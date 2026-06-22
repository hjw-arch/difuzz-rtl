import sys
import os
import subprocess
import re

class isaInput():
    def __init__(self, binary, intrfile, symbols=None, data=None):
        self.binary = binary
        self.intrfile = intrfile
        self.symbols = symbols
        self.data = data

class rvISAhost():
    STORE_SIZES = {
        'sb': 1, 'sh': 2, 'sw': 4, 'sd': 8,
        'fsb': 1, 'fsh': 2, 'fsw': 4, 'fsd': 8, 'fsq': 16,
        'c_sw': 4, 'c_swsp': 4, 'c_fsw': 4, 'c_fswsp': 4,
        'c_sd': 8, 'c_sdsp': 8, 'c_fsd': 8, 'c_fsdsp': 8,
    }
    STORE_RE = re.compile(
        r'\([0-9a-fA-Fx]+\)\s+([A-Za-z0-9_.]+)\s+.*'
        r'\bmem\s+0x([0-9a-fA-F]+)\s+0x([0-9a-fA-F]+)')
    OP_RE = re.compile(r'\([0-9a-fA-Fx]+\)\s+([A-Za-z0-9_.]+)\b')
    MEM_RE = re.compile(r'\bmem\s+0x([0-9a-fA-F]+)\s+0x([0-9a-fA-F]+)')

    def __init__(self, spike, spike_args, isa_sigfile, debug=False):
        self.spike = spike
        self.spike_args = spike_args
        self.isa_sigfile = isa_sigfile

        self.debug= debug

    def debug_print(self, message):
        if self.debug:
            print(message)

    def run_test(self, isa_input: isaInput, assert_intr=False):
        binary = isa_input.binary
        if assert_intr: intr = [ '--intr={}'.format(isa_input.intrfile) ]
        else: intr = []

        args = [ self.spike ] + self.spike_args + intr + \
            [ '+signature={}'.format(self.isa_sigfile), binary ]

        self.debug_print('[ISAHost] Start ISA simulation')
        if '-l' not in args:
            args.insert(1 + len(self.spike_args), '-l')

        result = subprocess.run(args, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        if self.debug:
            print(result.stdout, end='')
        if result.returncode == 0:
            self.append_data_signature(isa_input, result.stdout)
        return result.returncode

    def append_data_signature(self, isa_input, spike_log):
        symbols = getattr(isa_input, 'symbols', None)
        initial_data = getattr(isa_input, 'data', None)
        if not symbols or not initial_data:
            return
        if not os.path.isfile(self.isa_sigfile):
            return

        signature_lines = (
            symbols['end_signature'] - symbols['begin_signature']) // 16
        data_ranges = [
            (symbols['_random_data{}'.format(i)],
             symbols['_end_data{}'.format(i)])
            for i in range(6)
        ]
        data_lines = sum((end - start) // 16 for start, end in data_ranges)
        with open(self.isa_sigfile, 'r') as fd:
            lines = fd.readlines()
        if len(lines) >= signature_lines + data_lines:
            return
        if len(lines) != signature_lines:
            return

        memory = self.initial_data_memory(initial_data, data_ranges)
        self.apply_store_log(memory, data_ranges, spike_log)

        with open(self.isa_sigfile, 'a') as fd:
            for start, end in data_ranges:
                for addr in range(start, end, 16):
                    lo = self.load_word(memory, addr)
                    hi = self.load_word(memory, addr + 8)
                    fd.write('{:016x}{:016x}\n'.format(hi, lo))

    def initial_data_memory(self, initial_data, data_ranges):
        memory = {}
        offset = 0
        for start, end in data_ranges:
            for idx, addr in enumerate(range(start, end, 8)):
                value = int(initial_data[offset + idx])
                self.store_bytes(memory, addr, value, 8)
            offset += (end - start) // 8
        return memory

    def apply_store_log(self, memory, data_ranges, spike_log):
        pending_op = None
        for line in spike_log.splitlines():
            op_match = self.OP_RE.search(line)
            if op_match:
                pending_op = op_match.group(1).replace('.', '_')

            match = self.STORE_RE.search(line)
            if match:
                op = match.group(1).replace('.', '_')
                mem_matches = [(match.group(2), match.group(3))]
            else:
                op = pending_op
                mem_matches = self.MEM_RE.findall(line)
            if not op or not mem_matches:
                continue
            size = self.store_size(op)
            if not size:
                continue
            addr_text, value_text = mem_matches[-1]
            addr = int(addr_text, 16)
            if not self.addr_in_ranges(addr, data_ranges):
                continue
            value = int(value_text, 16)
            self.store_bytes(memory, addr, value, size)

    def store_size(self, op):
        if op in self.STORE_SIZES:
            return self.STORE_SIZES[op]
        if op.startswith('sc_w') or op.startswith('amo') and op.endswith('_w'):
            return 4
        if op.startswith('sc_d') or op.startswith('amo') and op.endswith('_d'):
            return 8
        return None

    def addr_in_ranges(self, addr, ranges):
        for start, end in ranges:
            if start <= addr < end:
                return True
        return False

    def store_bytes(self, memory, addr, value, size):
        for byte_idx in range(size):
            memory[addr + byte_idx] = (value >> (8 * byte_idx)) & 0xff

    def load_word(self, memory, addr):
        value = 0
        for byte_idx in range(8):
            value |= int(memory.get(addr + byte_idx, 0)) << (8 * byte_idx)
        return value & ((1 << 64) - 1)
