"""Lockstep check of real hardware against the Python model.

The board and the model get the same register writes: random programs (the
whole 16-bit instruction space, plus directed programs for the v4 features
skip, wake and add), random configurations, and words in the TX FIFOs. Both
engines are then single-stepped one tick at a time with CTRL.STEP, and after
every tick each engine's registers, the pins and the flags are read from both
and compared. Stepping makes the result independent of the host's speed, and
loopback makes the pins independent of whatever is plugged into the board.

This is the co-simulation of test/test_cosim.py, with the FPGA in place of
the RTL simulator. `chip` is anything with write(addr, *words) and
read(addr, n) (pemu_board.Pemu over the Basys 3 bridge, or a model).
"""

import random

from .asm import assemble
from .host import ModelHost, ctrl
from .model import (A_CTRL, A_FLAGS, A_IMEM_SEL, A_PIN_OE, A_PIN_OUT, IMEM_WORDS, R_CLKDIV,
                    R_CRC, R_CRCPOLY, R_PINCFG, R_PINMAP, R_PROG, R_SHIFT, R_TIME, R_TIMEOUT,
                    R_TRAP, R_X, R_Y, R_FIFO, Core, engine_base)

# Directed programs for the v4 instructions (as in test/test_cosim.py).
V4_PROGRAMS = [
    "wake pin\nwait 1 pin {k}\nin pins, 2\nwake !flag {k}\nwait 0 edge {k} timeout\n"
    "set flag, {k}\nwake txdata\nwait any edge {k}\nwake x<y [1]\nwait 1 gpio {k} timeout\n"
    "push noblock\nwake rxfull\nwait 0 flag {k}\npull noblock\nwake osre\nwait 1 pin 0\n"
    "clr flag, {k}\njmp y--, 0",
    "skip x<y\nmov x, x + y\nskip xodd\nskip !pin\nmov y, y + time\nskip x==y\n"
    "set x, {k}\nskip xneg [1]\nmov y, y + pins\nskip !flag {k}\nskip osre\nout x, 3\n"
    "skip txdata\nmov x, ~x\nskip rxfull\nin y, 4",
    "set x, {k}\nset y, {k}\nskip x<y\nmov x, ~x\nset x, {k}\nskip x>=y\nmov y, ~y\n"
    "set y, {k}\nskip x==y\nmov x, ~x\nmov x, ~x\nmov x, x + y\nskip x<y\nset y, 0\n"
    "mov y, x\nmov x, ~null\nmov x, x + y\nskip x<y\nmov y, ~y\nskip x!=y\nin x, 1",
    "mov x, x + y\nmov y, y + x\nmov x, x + time\nmov y, ~y\nmov y, y + isr\n"
    "mov x, x + osr\nmov y, y + crc\nmov x, x + pins\nmov y, y + null\nin x, 5\nout y, 3",
]

# What is compared after every tick: per engine CRC, X, Y, ISR, OSR, STATUS,
# TIME (0x48-0x4E and 0x58-0x5E), then FLAGS, PIN_IN, PIN_OUT, PIN_OE.
SNAPSHOT = [(engine_base(0) + R_CRC, 7), (engine_base(1) + R_CRC, 7), (A_FLAGS, 4)]
NAMES = ([f"e{e}.{n}" for e in range(2) for n in ("CRC", "X", "Y", "ISR", "OSR", "STATUS", "TIME")]
         + ["FLAGS", "PIN_IN", "PIN_OUT", "PIN_OE"])


class ModelChip:
    """The Python model behind the same write/read interface as the board."""

    def __init__(self):
        self.host = ModelHost(Core())

    def write(self, addr, *words):
        for i, w in enumerate(words):
            self.host.write(addr + i, w)
        self.host.idle(2)   # CTRL pulses act on the clock after the write

    def read(self, addr, n=1):
        words = [self.host.read(addr + i) for i in range(n)]
        return words[0] if n == 1 else words


def snapshot(chip):
    words = []
    for addr, n in SNAPSHOT:
        r = chip.read(addr, n)
        words += r if isinstance(r, list) else [r]
    return words


def random_word(rng):
    """A random instruction, biased toward ones that keep things moving
    (most waits can time out)."""
    op = rng.choices(range(8), weights=[8, 10, 12, 12, 8, 14, 8, 14])[0]
    sd = rng.choice([0, 0, 0, rng.randrange(32), rng.randrange(4)])
    operands = rng.randrange(256)
    if op in (2, 3) and rng.random() < 0.6:
        operands = (operands & 0xF0) | rng.choice([1, 2, 4, 8])
    if op == 1 and rng.random() < 0.7:
        operands |= 0x10
    return (op << 13) | (sd << 8) | operands


def random_program(rng):
    return [random_word(rng) for _ in range(rng.randrange(4, 20))], 0


def directed_program(rng, src):
    prog = assemble(src.format(k=rng.randrange(8)))
    return list(prog.code), prog.side_count | int(prog.side_pindir) << 2


def setup_writes(rng, programs):
    """Register writes that load and configure both engines (left disabled)."""
    writes = [(A_CTRL, ctrl(loopback=True)), (A_PIN_OUT, 0), (A_PIN_OE, 0), (A_FLAGS, 0)]
    for e, (code, side) in enumerate(programs):
        assert len(code) <= IMEM_WORDS
        b = engine_base(e)
        n = len(code)
        # All 64 words are written: jumps can leave the program, and what
        # they find must not depend on earlier runs. (Reset clears the
        # chip's latch memories but not the FPGA's LUT RAM, see pe_core.v.)
        writes.append((A_IMEM_SEL, 1 << e))
        writes += list(enumerate(code + [random_word(rng) for _ in range(IMEM_WORDS - n)]))
        pincfg = (rng.choice([1, 2, 4, 8]) | rng.randrange(6) << 4 | (side & 3) << 7
                  | (side >> 2) << 9 | rng.randrange(2) << 10 | rng.randrange(16) << 12)
        writes += [
            (b + R_CLKDIV, 0),
            (b + R_PROG, rng.randrange(n) << 4 | (n - 1) << 10),
            (b + R_PINMAP, rng.randrange(1 << 16)),
            (b + R_PINCFG, pincfg),
            (b + R_SHIFT, rng.randrange(1 << 14)),
            (b + R_TIMEOUT, rng.randrange(1, 12)),
            (b + R_TRAP, rng.randrange(n)),
            (b + R_CRCPOLY, rng.randrange(1 << 16)),
            (b + R_CRC, rng.randrange(1 << 16)),
            (b + R_X, rng.choice([0, 1, rng.randrange(1 << 16)])),
            (b + R_Y, rng.choice([0, 1, rng.randrange(1 << 16)])),
        ]
        writes += [(b + R_FIFO, rng.randrange(1 << 16)) for _ in range(rng.randrange(5))]
    writes += [(A_FLAGS, rng.randrange(256)), (A_PIN_OE, rng.randrange(1 << 16)),
               (A_PIN_OUT, rng.randrange(1 << 16)), (A_CTRL, ctrl(loopback=True, restart=3))]
    return writes


def run_pair(chip, model, writes, ticks, where):
    for a, v in writes:
        chip.write(a, v)
        model.write(a, v)
    for t in range(ticks):
        hw, sw = snapshot(chip), snapshot(model)
        if hw != sw:
            bad = [f"{NAMES[i]} board {h:#06x} model {s:#06x}"
                   for i, (h, s) in enumerate(zip(hw, sw)) if h != s]
            raise AssertionError(f"{where}, tick {t}: " + "; ".join(bad))
        chip.write(A_CTRL, ctrl(loopback=True, step=3))
        model.write(A_CTRL, ctrl(loopback=True, step=3))


def run(chip, seed=1, programs=20, ticks=150, log=print, reset=None, link_errors=()):
    """Compare `chip` against the model for `programs` program pairs of
    `ticks` ticks each. Returns (ticks compared, retries); raises
    AssertionError, naming the register, at the first difference.

    With `reset` (a function that resets the chip), every program pair starts
    from reset on both sides, and a pair interrupted by one of `link_errors`
    (a lost USB reply) is run again from reset, up to 3 times. A difference
    in the registers is never retried."""
    rng = random.Random(seed)
    compared = retries = 0
    for p in range(programs):
        if p < 2 * len(V4_PROGRAMS):
            kind = "v4"
            pair = [directed_program(rng, V4_PROGRAMS[(2 * p + e) % len(V4_PROGRAMS)])
                    for e in range(2)]
        else:
            kind = "random"
            pair = [random_program(rng) for _ in range(2)]
        writes = setup_writes(rng, pair)
        where = f"program {p + 1} ({kind})"
        for attempt in range(4):
            if reset:
                reset()
                model = ModelChip()
            elif p == 0:
                model = ModelChip()
            try:
                run_pair(chip, model, writes, ticks, where)
                break
            except link_errors as e:
                if not reset or attempt == 3:
                    raise
                retries += 1
                log(f"{where}: link error ({e}), running it again from reset")
        compared += ticks
        log(f"program {p + 1}/{programs} ({kind}): {ticks} ticks match")
    chip.write(A_CTRL, 0)
    return compared, retries
