# SPDX-License-Identifier: Apache-2.0
"""Lockstep co-simulation: RTL core vs the Python reference model.

Random programs, random configuration, random pin activity and random host
bus traffic. Every clock, the RTL's outputs, bus read data and architectural
state are compared against sw/pemu/model.py.
"""

import os
import random
from collections import Counter

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge

from pemu.model import Core, engine_base

SEEDS = int(os.environ.get("COSIM_SEEDS", "40"))
CYCLES = int(os.environ.get("COSIM_CYCLES", "3000"))
SEED0 = int(os.environ.get("COSIM_SEED0", "1"))

ENGINE_FIELDS = [
    # (model attribute, RTL signal)
    ("pc", "pc"), ("delay", "delay"), ("x", "x"), ("y", "y"), ("isr", "isr"),
    ("isr_cnt", "isr_cnt"), ("osr", "osr"), ("osr_cnt", "osr_cnt"),
    ("crc", "crc"), ("armed", "armed"), ("to_cnt", "to_cnt"),
    ("time", "time_cnt"), ("divcnt", "divcnt"), ("stalled", "stalled"),
    ("timed_out", "timed_out"), ("rx_ovf", "rx_ovf"), ("tx_ovf", "tx_ovf"),
    ("pop_armed", "pop_armed"),
]


def random_instr(rng):
    """A random instruction, biased toward ones that keep things moving."""
    op = rng.choices(range(8), weights=[14, 10, 12, 12, 8, 14, 2, 14])[0]
    sd = rng.choice([0, 0, 0, rng.randrange(32), rng.randrange(4)])
    operands = rng.randrange(256)
    if op == 1 and rng.random() < 0.7:
        operands |= 0x10  # most waits can time out, so engines don't hang forever
    return (op << 13) | (sd << 8) | operands


def random_engine_config(rng, e):
    b = engine_base(e)
    regs = {
        0: rng.choice([0, 0, 0, 1, 2, 3, rng.randrange(8)]),         # CLKDIV
        1: rng.randrange(1 << 16),                                    # PROG
        2: rng.randrange(1 << 16),                                    # PINMAP
        3: rng.randrange(1 << 16),                                    # PINCFG
        4: rng.randrange(1 << 16) & rng.choice([0xFFFF, 0xFFF3, 0xFFF3]),  # SHIFT
        5: rng.choice([0, 1, 3, 7, 20, rng.randrange(64)]),           # TIMEOUT
        6: rng.randrange(32),                                         # TRAP
        7: rng.randrange(1 << 16),                                    # CRCPOLY
        8: rng.randrange(1 << 16),                                    # CRC
        9: rng.choice([0, 1, 5, rng.randrange(1 << 16)]),             # X
        10: rng.choice([0, 1, 5, rng.randrange(1 << 16)]),            # Y
    }
    return [(b + k, v) for k, v in regs.items()]


def random_bus_op(rng):
    """(we, re, rc, addr, wdata) for one clock. Loads and commits are issued
    independently, so aborted reads and FIFO changes in between are covered."""
    r = rng.random()
    if r < 0.66:
        return 0, 0, 0, rng.randrange(128), 0
    if r < 0.88:  # read load or commit, often at a FIFO port or status
        addr = rng.choice([0x4F, 0x5F, 0x4F, 0x5F, 0x4D, 0x5D, 0x62, rng.randrange(128)])
        load = rng.random() < 0.5
        return 0, int(load), int(not load), addr, 0
    addr = rng.choice([0x4F, 0x5F, 0x4F, 0x5F, 0x60, 0x61, 0x63, 0x64, 0x66, 0x67,
                       0x4D, 0x5D, 0x49, 0x4A, 0x59, 0x5A, rng.randrange(128)])
    wdata = rng.randrange(1 << 16)
    if addr == 0x60:
        # Mostly keep both engines enabled; occasionally pulse restart/step/flush
        wdata = rng.choice([0x3, 0x7, 0x3, 0x7, wdata & 0x3FF])
    return 1, 0, 0, addr, wdata


def sig(dut, path):
    obj = dut
    for p in path.split("."):
        obj = getattr(obj, p)
    return int(obj.value)


async def run_seed(dut, seed, coverage):
    rng = random.Random(seed)
    model = Core()

    dut.rst_n.value = 0
    dut.bus_we.value = 0
    dut.bus_re.value = 0
    dut.bus_rc.value = 0
    dut.bus_addr.value = 0
    dut.bus_wdata.value = 0
    dut.pins_ext.value = 0
    await ClockCycles(dut.clk, 2)
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1

    # Bring-up sequence: program, configure, start; then random traffic.
    script = [(0x60, 0)]
    script += [(a, random_instr(rng)) for a in range(64)]
    script += random_engine_config(rng, 0) + random_engine_config(rng, 1)
    script += [(0x63, rng.randrange(1 << 16)), (0x64, rng.randrange(1 << 16))]
    script += [(0x60, rng.choice([0x33, 0x37]))]  # restart + enable both
    pins = rng.randrange(1 << 16)
    toggle_p = rng.choice([0.01, 0.05, 0.2])

    for cyc in range(len(script) + CYCLES):
        # Inputs for this clock
        if cyc < len(script):
            we, re, rc, addr, wdata = 1, 0, 0, *script[cyc]
        else:
            we, re, rc, addr, wdata = random_bus_op(rng)
        if rng.random() < toggle_p:
            pins ^= 1 << rng.randrange(16)
        dut.bus_we.value = we
        dut.bus_re.value = re
        dut.bus_rc.value = rc
        dut.bus_addr.value = addr
        dut.bus_wdata.value = wdata
        dut.pins_ext.value = pins
        await cocotb.triggers.ReadOnly()

        # Compare the state the clock edge will act on
        ctx = f"seed {seed} cycle {cyc}"
        exp_rdata = model.read(addr)
        assert int(dut.bus_rdata.value) == exp_rdata, \
            f"{ctx}: rdata[{addr:#x}] {int(dut.bus_rdata.value):#06x} != {exp_rdata:#06x}"
        assert int(dut.pin_out.value) == model.pin_out, f"{ctx}: pin_out"
        assert int(dut.pin_oe.value) == model.pin_oe, f"{ctx}: pin_oe"
        assert sig(dut, "dut.flags") == model.flags, f"{ctx}: flags"
        for i, e in enumerate(model.engines):
            rtl = getattr(dut.dut, f"u_e{i}")
            for attr, name in ENGINE_FIELDS:
                got = int(getattr(rtl, name).value)
                assert got == getattr(e, attr), \
                    f"{ctx}: engine {i} {attr} rtl={got:#x} model={getattr(e, attr):#x}"
            assert int(rtl.tx_count.value) == len(e.txf), f"{ctx}: engine {i} tx level"
            assert int(rtl.rx_count.value) == len(e.rxf), f"{ctx}: engine {i} rx level"
            if (model.ctrl >> i) & 1 and e.delay == 0 and e.divcnt >= e.clkdiv:
                ins = model.imem[(e.origin + e.pc) & 63]
                coverage[f"op{ins >> 13}"] += 1

        before = [(e.pc, e.stalled, e.timed_out) for e in model.engines]
        model.clock(pins >> 8, pins & 0xFF, we=bool(we), re=bool(re), addr=addr,
                    wdata=wdata, rc=bool(rc))
        for (pc, st, to), e in zip(before, model.engines):
            coverage["stall_ticks"] += e.stalled and not st
            coverage["traps"] += e.timed_out and not to
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)

    for e in model.engines:
        coverage["rx_ovf"] += e.rx_ovf
        coverage["tx_ovf"] += e.tx_ovf


@cocotb.test()
async def test_lockstep_random(dut):
    """RTL == model, every clock, over many random programs."""
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    coverage = Counter()
    for k in range(SEEDS):
        await run_seed(dut, SEED0 + k, coverage)
    dut._log.info(f"{SEEDS} seeds x {CYCLES} cycles, coverage: {dict(sorted(coverage.items()))}")
    for op in (0, 1, 2, 3, 4, 5, 7):
        assert coverage[f"op{op}"] > 0, f"opcode {op} never executed"
