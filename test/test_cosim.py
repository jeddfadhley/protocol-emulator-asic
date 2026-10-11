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


PROFILES = {
    # opcode weights:  JMP WAIT IN OUT P/P MOV rsv SET
    "mixed":          [14, 10, 12, 12, 8, 14, 2, 14],
    "datapath":       [8, 2, 22, 22, 18, 14, 1, 8],
    "control":        [24, 16, 6, 6, 4, 14, 1, 20],
}


def random_instr(rng, profile="mixed"):
    """A random instruction, biased toward ones that keep things moving."""
    op = rng.choices(range(8), weights=PROFILES[profile])[0]
    sd = rng.choice([0, 0, 0, rng.randrange(32), rng.randrange(4)])
    operands = rng.randrange(256)
    if op in (2, 3) and rng.random() < 0.6:
        # small bit counts make shift-count/threshold boundaries common
        operands = (operands & 0xF0) | rng.choice([1, 1, 2, 2, 4, 8])
    if op == 1 and rng.random() < 0.7:
        operands |= 0x10  # most waits can time out, so engines don't hang forever
    return (op << 13) | (sd << 8) | operands


def random_shift(rng):
    """SHIFT register: thresholds are usually small powers of two."""
    v = rng.randrange(1 << 16) & rng.choice([0xFFFF, 0xFFF3, 0xFFF3])
    if rng.random() < 0.7:
        v = (v & ~0xFF0) | rng.choice([1, 2, 4, 8, 0]) << 4 | rng.choice([1, 2, 4, 8, 0]) << 8
    return v


def random_engine_config(rng, e):
    b = engine_base(e)
    regs = {
        0: rng.choice([0, 0, 0, 1, 2, 3, rng.randrange(8)]),         # CLKDIV
        1: rng.randrange(1 << 16),                                    # PROG
        2: rng.randrange(1 << 16),                                    # PINMAP
        3: rng.randrange(1 << 16),                                    # PINCFG
        4: random_shift(rng),                                         # SHIFT
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
    addr = rng.choice([0x4F, 0x5F] * 6 + [0x60, 0x61, 0x63, 0x64, 0x66, 0x67,
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
    # Bring-up sequence: program, configure, start; then random traffic.
    script = [(0x60, 0)]
    profile = rng.choice(list(PROFILES))
    coverage[f"profile_{profile}"] += 1
    script += [(a, random_instr(rng, profile)) for a in range(64)]
    script += random_engine_config(rng, 0) + random_engine_config(rng, 1)
    script += [(0x63, rng.randrange(1 << 16)), (0x64, rng.randrange(1 << 16))]
    script += [(0x60, rng.choice([0x33, 0x37]))]  # restart + enable both
    await run_lockstep(dut, rng, script, CYCLES, coverage, f"seed {seed}")


async def run_lockstep(dut, rng, script, cycles, coverage, name):
    """Apply `script` (register writes), then random traffic, comparing the
    RTL with the model every clock."""
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

    pins = rng.randrange(1 << 16)
    toggle_p = rng.choice([0.01, 0.05, 0.2])

    for cyc in range(len(script) + cycles):
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
        ctx = f"{name} cycle {cyc}"
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


# Directed programs: small loops that sweep every bit count against every
# threshold, so shift-count and FIFO boundary cases are hit exhaustively.
DIRECTED = [
    ("in pins, {n}\npush iffull noblock", "push_thresh"),
    ("in pins, {n}\npush iffull block", "push_thresh"),
    ("in x, {n}", "push_thresh"),                      # autopush
    ("out x, {n}\npull ifempty noblock", "pull_thresh"),
    ("out y, {n}\njmp !osre, 0\npull noblock", "pull_thresh"),
    ("out isr, {n}\npush iffull noblock", "push_thresh"),
    ("out pins, {n}", "pull_thresh"),                  # autopull
]


@cocotb.test()
async def test_lockstep_directed(dut):
    """Every IN/OUT bit count against every push/pull threshold."""
    from pemu.asm import assemble
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    coverage = Counter()
    rng = random.Random(1234)
    counts = (1, 2, 3, 5, 8, 15, 16)
    for k, (src, which) in enumerate(DIRECTED):
        auto = (0x4 if "push" in which else 0x8) if k in (2, 6) else 0
        for n in counts:
            code = assemble(src.format(n=n)).code
            for t in counts:
                # Engine 0 uses threshold t; engine 1 uses threshold n, so the
                # count == threshold case is always covered.
                script = [(0x60, 0)] + list(enumerate(code))
                for e, thr in ((0, t), (1, n)):
                    b = 0x40 + 16 * e
                    f = thr & 15
                    script += [(b + 1, (len(code) - 1) << 11),
                               (b + 4, f << 4 | f << 8 | auto | rng.randrange(4)),
                               (b + 9, rng.randrange(1 << 16))]
                script += [(0x60, 0x33)]
                await run_lockstep(dut, rng, script, 300, coverage,
                                   f"directed {k} n={n} t={t}")
    dut._log.info(f"directed coverage: {dict(sorted(coverage.items()))}")
