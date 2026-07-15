import os

import cocotb

from cocotb.decorators import coroutine
from cocotb.triggers import Timer, RisingEdge
from reader.tile_reader import infer_top_port_names
from adapters.tile_adapter import tileAdapter
from fuzzer.rtl_coverage import DutCoverageObserver, DutDtGroupObserver
from fuzzer.tile_info import read_tile_info

SUCCESS = 0
ASSERTION_FAIL = 1
TIME_OUT = 2
ILL_MEM = -1

DRAM_BASE = 0x80000000

class rtlInput():
    def __init__(self, hexfile, intrfile, data, symbols, max_cycles):
        self.hexfile = hexfile
        self.intrfile = intrfile
        self.data = data
        self.symbols = symbols
        self.max_cycles = max_cycles

class rvRTLhost():
    def __init__(self, dut, toplevel, rtl_sig_file, debug=False,
                 dt_group_json=None, dt_group_pair_id=None,
                 dt_group_feedback_io="auto", dt_group_feedback_bits=False,
                 dt_group_internal_weight=1, dt_group_object_weights=""):
        source_info = os.getenv(
            "DIFUZZRTL_TILE_INFO",
            'infos/' + toplevel + '_info.txt')
        paths = read_tile_info(source_info)

        port_names = infer_top_port_names(toplevel) or paths['port_names']
        monitor_pc = paths['monitor_pc']
        monitor_valid = paths['monitor_valid']
        monitor = (monitor_pc[0], monitor_valid[0])

        self.rtl_sig_file = rtl_sig_file
        self.debug = debug

        self.dut = dut
        self.adapter = tileAdapter(dut, port_names, monitor, self.debug)
        self.toplevel = toplevel
        self.coverage = DutCoverageObserver(getattr(dut, toplevel, dut), toplevel)
        self.dt_group = DutDtGroupObserver(
            self.coverage,
            dt_group_json,
            pair_id=dt_group_pair_id,
            feedback_io=dt_group_feedback_io,
            feedback_bits=bool(dt_group_feedback_bits),
            internal_weight=int(dt_group_internal_weight or 1),
            object_weights=dt_group_object_weights,
        )
        self.module_cov_names = self.coverage.module_cov_names
        self.last_bitmap_target_hits = set()
        self.last_dt_group_feedback_hits = set()
        self.last_target_cov_hits = set()
        self.last_target_cov_module = None
        self.last_target_handle_count = 0
        self.last_dt_group_observation = None
        self.last_dt_group_handle_count = 0
        self.last_cycles = 0

    def debug_print(self, message):
        if self.debug:
            print(message)

    def set_bootrom(self):
        bootrom_addrs = []
        memory = {}
        bootrom = [ 0x00000297, # auipc t0, 0x0
                    0x02028593, # addi a1, t0, 32
                    0xf1402573, # csrr a0, mhartid
                    0x0182b283, # ld t0, 24(t0)
                    0x00028067, # jr t0
                    0x00000000, # no data
                    0x80000000, # Jump address
                    0x00000000,
                    0x00000000,
                    0x00000000,
                    0x00000000,
                    0x00000000,
                    0x00000000,
                    0x00000000,
                    0x00000000,
                    0x00000000 ] # no data

        for i in range(0, len(bootrom), 2):
            bootrom_addrs.append(0x10000 + i * 4)
            memory[0x10000 + i * 4] = (bootrom[i+1] << 32) | bootrom[i]

        return (bootrom_addrs, memory)

    @coroutine
    def clock_gen(self, clock, period=2):
        while True:
            clock <= 1
            yield Timer(period / 2)
            clock <= 0
            yield Timer(period / 2)

    @coroutine
    def reset(self, clock, metaReset, reset, timer=5):
        clkedge = RisingEdge(clock)

        metaReset <= 1
        for i in range(timer):
            yield clkedge
        metaReset <= 0
        reset <= 1
        for i in range(timer):
            yield clkedge
        reset <= 0

    def save_signature(self, memory, sig_start, sig_end, data_addrs, sig_file):
        fd = open(sig_file, 'w')
        for i in range(sig_start, sig_end, 16):
            dump = '{:016x}{:016x}\n'.format(memory[i+8], memory[i])
            fd.write(dump)

        for (data_start, data_end) in data_addrs:
            for i in range(data_start, data_end, 16):
                dump = '{:016x}{:016x}\n'.format(memory[i+8], memory[i])
                fd.write(dump)

        fd.close()

    def get_covsum(self):
        return self.coverage.total_cov()

    def get_cov_stats(self):
        return self.coverage.cov_stats()

    @coroutine
    def run_test(self, rtl_input: rtlInput, assert_intr: bool,
                 target_bitmap_module=None, target_bitmap_sample_period=1):

        self.debug_print('[RTLHost] Start RTL simulation')
        self.last_bitmap_target_hits = set()
        self.last_dt_group_feedback_hits = set()
        self.last_target_cov_hits = set()
        self.last_target_cov_module = target_bitmap_module
        self.last_target_handle_count = 0
        self.last_dt_group_observation = None
        self.last_dt_group_handle_count = 0
        self.last_cycles = 0

        fd = open(rtl_input.hexfile, 'r')
        lines = fd.readlines()
        fd.close()

        max_cycles = rtl_input.max_cycles

        symbols = rtl_input.symbols
        _start = symbols['_start']
        _end = symbols['_end_main']

        (bootrom_addrs, memory) = self.set_bootrom()
        for (i, addr) in enumerate(range(_start, _end + 36, 8)):
            memory[addr] = int(lines[i], 16)

        tohost_addr = symbols['tohost']
        sig_start = symbols['begin_signature']
        sig_end = symbols['end_signature']

        memory[tohost_addr] = 0
        for addr in range(sig_start // 8 * 8, sig_end, 8):
            memory[addr] = 0

        data = rtl_input.data
        data_addrs = []
        offset = 0
        for n in range(6):
            data_start = symbols['_random_data{}'.format(n)]
            data_end = symbols['_end_data{}'.format(n)]
            data_addrs.append((data_start, data_end))

            for i, addr in enumerate(range(data_start // 8 * 8, data_end // 8 * 8, 8)):
                word = data[i + offset]
                memory[addr] = word

            offset += (data_end - data_start) // 8

        self.debug_print('[RTLHost] Prepared memory image')

        ints = {}
        if assert_intr:
            fd = open(rtl_input.intrfile, 'r')
            intr_pairs = [ line.split(':') for line in fd.readlines() ]
            fd.close()

            for pair in intr_pairs:
                ints[int(pair[0], 16)] = int(pair[1], 2)

        clk = self.dut.clock
        clk_driver = cocotb.fork(self.clock_gen(clk))
        clkedge = RisingEdge(clk)
        target_cov_trace_handles = self.coverage.target_trace_handles(
            target_bitmap_module)
        self.last_target_handle_count = len(target_cov_trace_handles)
        target_bitmap_sample_period = max(
            int(target_bitmap_sample_period or 1), 1)
        self.dt_group.reset()
        self.last_dt_group_handle_count = self.dt_group.handle_count

        self.debug_print('[RTLHost] Reset begin')
        yield self.reset(clk, self.dut.metaReset, self.dut.reset)
        self.debug_print('[RTLHost] Reset complete')

        self.adapter.start(memory, ints)
        self.debug_print('[RTLHost] Execution begin')
        for i in range(max_cycles):
            yield clkedge
            if target_cov_trace_handles and \
               i % target_bitmap_sample_period == 0:
                self.coverage.sample_tagged_handles(
                    target_cov_trace_handles, self.last_bitmap_target_hits)
            if self.dt_group.enabled and i % target_bitmap_sample_period == 0:
                self.dt_group.sample()

            if i % 100 == 0:
                tohost = memory[tohost_addr]
                if tohost:
                    break
                else:
                    self.adapter.probe_tohost(tohost_addr)
        self.last_cycles = i + 1
        self.debug_print('[RTLHost] Execution complete cycles={}'.format(
            self.last_cycles))

        yield self.adapter.stop()
        adapter_stop_forced = bool(getattr(self.adapter, 'stop_forced', False))
        self.debug_print('[RTLHost] Adapter stopped forced={}'.format(
            adapter_stop_forced))
        clk_driver.kill()
        self.last_dt_group_observation = self.dt_group.observe()
        if self.last_dt_group_observation is not None:
            self.last_dt_group_feedback_hits = set(
                self.last_dt_group_observation.feedback_targets)
        self.last_target_cov_hits = (
            self.last_bitmap_target_hits | self.last_dt_group_feedback_hits)

        # Check all the CPU's memory access operations occurs in DRAM
        mem_check = True
        for addr in memory.keys():
            if addr not in bootrom_addrs and addr < DRAM_BASE:
                mem_check = False

        cov_total, module_covs = self.get_cov_stats()

        if not mem_check:
            return (ILL_MEM, cov_total, module_covs)

        if i == max_cycles - 1 or adapter_stop_forced:
            self.debug_print('[RTLHost] Timeout, max_cycle={}'.format(max_cycles))
            return (TIME_OUT, cov_total, module_covs)

        if self.adapter.check_assert():
            self.debug_print('[RTLHost] Assertion Failure')
            return (ASSERTION_FAIL, cov_total, module_covs)

        self.save_signature(memory, sig_start, sig_end, data_addrs, self.rtl_sig_file)
        self.debug_print('[RTLHost] Stop RTL simulation')

        return (SUCCESS, cov_total, module_covs)
