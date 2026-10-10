"""Host-side helpers: engine configuration and register write sequences.

These functions only build lists of (address, value) register writes, so the
same code drives the Python model, the cocotb testbenches (over SPI) and real
hardware.
"""

from dataclasses import dataclass

from .model import (A_CTRL, R_CLKDIV, R_CRC, R_CRCPOLY, R_PINCFG, R_PINMAP,
                    R_PROG, R_SHIFT, R_TIMEOUT, R_TRAP, R_X, R_Y, engine_base)


@dataclass
class EngineConfig:
    clkdiv: int = 0
    out_base: int = 0
    out_count: int = 1
    set_base: int = 0
    set_count: int = 0
    side_base: int = 0
    in_base: int = 0
    jmp_pin: int = 0
    in_right: bool = False
    out_right: bool = False
    autopush: bool = False
    autopull: bool = False
    push_thresh: int = 16
    pull_thresh: int = 16
    crc_in: bool = False
    crc_out: bool = False
    crc_poly: int = 0
    crc_init: int = 0
    timeout: int = 0xFFFF
    x: int = 0
    y: int = 0

    def pinmap(self):
        return (self.out_base & 15) | (self.set_base & 15) << 4 \
            | (self.side_base & 15) << 8 | (self.in_base & 15) << 12

    def pincfg(self, program):
        return (self.out_count & 15) | (self.set_count & 7) << 4 \
            | (program.side_count & 3) << 7 | int(program.side_pindir) << 9 \
            | (self.jmp_pin & 15) << 12

    def shift(self):
        return (int(self.in_right) | int(self.out_right) << 1 | int(self.autopush) << 2
                | int(self.autopull) << 3 | (self.push_thresh & 15) << 4
                | (self.pull_thresh & 15) << 8 | int(self.crc_in) << 12
                | int(self.crc_out) << 13)


def load_writes(program, origin):
    """Register writes that place a program in instruction memory."""
    if origin + len(program) > 64:
        raise ValueError("program does not fit at this origin")
    return [(origin + i, w) for i, w in enumerate(program.code)]


def config_writes(engine, program, origin, cfg):
    """Register writes that configure an engine to run `program` at `origin`."""
    b = engine_base(engine)
    return [
        (b + R_CLKDIV, cfg.clkdiv),
        (b + R_PROG, program.prog_reg(origin)),
        (b + R_PINMAP, cfg.pinmap()),
        (b + R_PINCFG, cfg.pincfg(program)),
        (b + R_SHIFT, cfg.shift()),
        (b + R_TIMEOUT, cfg.timeout),
        (b + R_TRAP, program.trap),
        (b + R_CRCPOLY, cfg.crc_poly),
        (b + R_CRC, cfg.crc_init),
        (b + R_X, cfg.x),
        (b + R_Y, cfg.y),
    ]


def ctrl(enable=0, loopback=False, restart=0, step=0, flush=0):
    """CTRL register value. enable/restart/step/flush are engine bitmasks."""
    return (enable & 3) | int(loopback) << 2 | (restart & 3) << 4 \
        | (step & 3) << 6 | (flush & 3) << 8


def setup_writes(engine, program, origin, cfg):
    """Load + configure + restart (engine left disabled)."""
    return (load_writes(program, origin) + config_writes(engine, program, origin, cfg))


class ModelHost:
    """Synchronous host driver for the Python model (one bus op per clock)."""

    def __init__(self, core, ui_in=0, uio_in=0):
        self.core = core
        self.ui_in = ui_in
        self.uio_in = uio_in
        self.ctrl_state = 0  # en + loopback bits we last wrote

    def write(self, addr, value):
        self.core.clock(self.ui_in, self.uio_in, we=True, addr=addr, wdata=value)

    def read(self, addr, pop=True):
        rdata = self.core.clock(self.ui_in, self.uio_in, re=True, addr=addr)
        self.core.clock(self.ui_in, self.uio_in, rc=pop, addr=addr)
        return rdata

    def apply(self, writes):
        for a, v in writes:
            self.write(a, v)

    def idle(self, n=1):
        for _ in range(n):
            self.core.clock(self.ui_in, self.uio_in)
