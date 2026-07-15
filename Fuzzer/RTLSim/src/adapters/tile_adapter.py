import re
import sys
import os
import cocotb

from cocotb.decorators import coroutine
from cocotb.triggers import RisingEdge

from adapters.tilelink.adapter import tlAdapter
from adapters.tilelink.definitions import *

INT_MEIP = 0x4
INT_SEIP = 0x8
INT_MTIP = 0x1
INT_MSIP = 0x2

TL_PORT_RE = re.compile(r"_[abcde]_(?:ready|valid|bits(?:_|$))")


def is_interrupt_port(name):
    return (
        name.startswith("auto_int_") or
        "_int_in_" in name or
        "_int_local_" in name
    )


def _get_handle(parent, name):
    try:
        return getattr(parent, name)
    except AttributeError:
        pass
    try:
        return parent._id(name, extended=False)
    except Exception:
        pass
    try:
        return parent._id(name, extended=True)
    except Exception:
        return None


def _verilator_escaped_name(name):
    return name.replace("/", "__02f").replace(".", "__02e")


def resolve_dut_handle(dut, name):
    """Resolve a DUT handle across hierarchical and Yosys-flattened Verilog."""
    top = getattr(dut, "_name", "")
    flat_slash = name.replace(".", "/")
    flat_dot = name.replace("/", ".")
    candidates = [
        name,
        flat_slash,
        flat_dot,
        "\\{} ".format(flat_slash),
        name.replace(".", "__DOT__").replace("/", "__DOT__"),
        _verilator_escaped_name(flat_slash),
    ]
    if top:
        candidates.append("{}__DOT__{}".format(
            top, _verilator_escaped_name(flat_slash)))

    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        handle = _get_handle(dut, candidate)
        if handle is not None:
            return handle

    for sep in (".", "/"):
        if sep not in name:
            continue
        handle = dut
        ok = True
        for part in name.split(sep):
            handle = _get_handle(handle, part)
            if handle is None:
                ok = False
                break
        if ok:
            return handle

    raise AttributeError("{} contains no object named {}".format(
        getattr(dut, "_name", dut), name))


class intPorts():
    __slots__ = ('seip', 'meip', 'msip', 'mtip')

    def __init__(self):
        for attr in self.__slots__:
            setattr(self, attr, None)

class tileAdapter():
    def __init__(self, dut, port_names, monitor, debug=False,
                 monitor_dut=None):
        self.dut = dut
        monitor_dut = dut if monitor_dut is None else monitor_dut
        self.debug = debug
        self.drive = False

        tl_port_names = []
        int_port_names = []
        others = []
        protocol = TL_UL
        reset_vector_port = None

        for name in port_names:
            if TL_PORT_RE.search(name):
                tl_port_names.append(name)
            elif is_interrupt_port(name):
                int_port_names.append(name)
            elif 'reset_vector' in name:
                reset_vector_port = name
            else:
                others.append(name)

        pc_name = monitor[0]
        valid_name = monitor[1]

        for name in tl_port_names:
            if '_b_' in name:
                protocol = TL_C

        self.tl_adapter = tlAdapter(dut, tl_port_names, protocol, 64, debug)

        self.int_ports = intPorts()
        self.int_handles = []
        for name in int_port_names:
            handle = getattr(self.dut, name)
            self.int_handles.append(handle)
            if re.search(r'in_2_(?:sync_)?0$', name):
                setattr(self.int_ports, 'seip', handle)
            if re.search(r'in_1_(?:sync_)?0$', name):
                setattr(self.int_ports, 'meip', handle)
            if re.search(r'in_0_(?:sync_)?0$', name):
                setattr(self.int_ports, 'msip', handle)
            if re.search(r'in_0_(?:sync_)?1$', name):
                setattr(self.int_ports, 'mtip', handle)

        if self.int_ports.mtip is None:
            for name in int_port_names:
                if re.search(r'in_1_(?:sync_)?1$', name):
                    setattr(self.int_ports, 'mtip', getattr(self.dut, name))
                    break

        self.reset_vector_port = resolve_dut_handle(self.dut, reset_vector_port)

        self.reset_vector = 0x10000
        self.reset_vector_port <= self.reset_vector
        self.clear_interrupts()

        self.monitor_pc = resolve_dut_handle(monitor_dut, pc_name)
        self.monitor_valid = resolve_dut_handle(monitor_dut, valid_name)

        self.intr = 0
        self.stop_forced = False

    def debug_print(self, message):
        if self.debug:
            print(message)

    def clear_interrupts(self):
        for handle in self.int_handles:
            handle <= 0

    def assert_intr(self, intr):
        if intr == self.intr:
            return

        missing = [
            name for name in self.int_ports.__slots__
            if getattr(self.int_ports, name) is None
        ]
        if missing:
            raise Exception('Cannot assert interrupts; missing ports: {}'.format(
                ', '.join(missing)))

        self.intr = intr
        meip = int((intr & INT_MEIP) == INT_MEIP)
        seip = int((intr & INT_SEIP) == INT_SEIP)
        mtip = int((intr & INT_MTIP) == INT_MTIP)
        msip = int((intr & INT_MSIP) == INT_MSIP)

        self.int_ports.seip <= seip
        self.int_ports.meip <= meip
        self.int_ports.msip <= msip
        self.int_ports.mtip <= mtip

    def pc_valid(self):
        return self.monitor_valid.value

    @coroutine
    def interrupt_handler(self, ints):
        if not ints:
            return

        while self.drive:
            if self.pc_valid():
                pc = self.monitor_pc.value & ((1 << len(self.monitor_pc.value)) - 1)
                if pc in ints.keys():
                    self.debug_print('[RTLHost] interrupt_handler, pc: {:016x}, INT: {:01x}'.
                                     format(pc, ints[pc]))
                    self.assert_intr(ints[pc])
            yield RisingEdge(self.dut.clock)


    def probe_tohost(self, tohost_addr):
        self.tl_adapter.probe_block(tohost_addr)

    def check_assert(self):
        return self.dut.metaAssert.value

    def start(self, memory, ints):
        if memory.__class__.__name__ != 'dict':
            raise Exception('Tile adapter must receive address map to drive DUT')

        self.drive = True
        self.stop_forced = False
        self.tl_adapter.start(memory)
        self.intr_handler = cocotb.fork(self.interrupt_handler(ints))

    @coroutine
    def stop(self):
        self.drive = False
        max_wait = max(int(os.getenv('DIFUZZRTL_ADAPTER_STOP_MAX_CYCLES', '1000')), 0)
        waited = 0
        while self.tl_adapter.onGoing() and waited < max_wait:
            yield RisingEdge(self.dut.clock)
            waited += 1
        self.tl_adapter.stop()
        waited = 0
        while self.tl_adapter.isRunning() and waited < max_wait:
            yield RisingEdge(self.dut.clock)
            waited += 1
        if self.tl_adapter.isRunning():
            self.stop_forced = True
            self.tl_adapter.force_stop()

        self.clear_interrupts()

        self.intr = 0
