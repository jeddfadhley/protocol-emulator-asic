"""Cycle-accurate reference model of the PEMU core.

This is the executable form of docs/isa.md. The RTL core (src/pe_core.v) is
checked against it clock by clock, so any behaviour change must be made in
both places.

A `Core` advances one clock per `clock()` call. The host bus is the parallel
register interface that sits behind the SPI slave. Per clock it carries at
most one of: a write (`we`), a read load (`re`: read data is captured) or a
read commit (`rc`: the word has been fully clocked out). A FIFO port read
pops the RX FIFO at commit, and only if the FIFO was non-empty at load, so
an aborted or speculative read never loses data.
"""

from dataclasses import dataclass, field

M16 = 0xFFFF
FIFO_DEPTH = 4

# Global register addresses
A_CTRL, A_FLAGS, A_PIN_IN, A_PIN_OUT, A_PIN_OE, A_ID, A_PIN_SET, A_PIN_CLR = range(0x60, 0x68)
CHIP_ID = 0x5045

# Engine register offsets
(R_CLKDIV, R_PROG, R_PINMAP, R_PINCFG, R_SHIFT, R_TIMEOUT, R_TRAP, R_CRCPOLY,
 R_CRC, R_X, R_Y, R_ISR, R_OSR, R_STATUS, R_TIME, R_FIFO) = range(16)

PINCFG_MASK = 0xF3FF
SHIFT_MASK = 0x3FFF


def engine_base(e):
    return 0x40 + 16 * e


def rotl(v, s):
    s &= 15
    return ((v << s) | (v >> (16 - s))) & M16


def rotr(v, s):
    s &= 15
    return ((v >> s) | (v << (16 - s))) & M16


def bitrev16(v):
    return int(f"{v & M16:016b}"[::-1], 2)


def mask_n(n):
    return (1 << n) - 1


def nfield(v):
    """Decode a 4-bit count field where 0 means 16."""
    v &= 15
    return 16 if v == 0 else v


def crc_step(crc, poly, bit):
    fb = ((crc >> 15) & 1) ^ (bit & 1)
    crc = (crc << 1) & M16
    return crc ^ poly if fb else crc


@dataclass
class Effects:
    """What one engine does to shared state during one clock."""
    out_mask: int = 0
    out_val: int = 0
    oe_mask: int = 0
    oe_val: int = 0
    flag_set: int = 0
    flag_clr: int = 0
    tx_pop: bool = False
    rx_push: int | None = None

    def write_pins(self, dirs, base, n, value):
        m = rotl(mask_n(n), base)
        v = rotl(value & mask_n(n), base)
        if dirs:
            self.oe_mask |= m
            self.oe_val = (self.oe_val & ~m) | v
        else:
            self.out_mask |= m
            self.out_val = (self.out_val & ~m) | v


@dataclass
class Engine:
    # configuration
    clkdiv: int = 0
    prog: int = 0xF800
    pinmap: int = 0
    pincfg: int = 0
    shift: int = 0
    timeout: int = 0xFFFF
    trap: int = 0
    crcpoly: int = 0
    # architectural state
    crc: int = 0
    x: int = 0
    y: int = 0
    pc: int = 0
    delay: int = 0
    isr: int = 0
    isr_cnt: int = 0
    osr: int = 0
    osr_cnt: int = 16
    armed: int = 0
    to_cnt: int = 0
    time: int = 0
    divcnt: int = 0
    stalled: int = 0
    timed_out: int = 0
    rx_ovf: int = 0
    tx_ovf: int = 0
    pop_armed: int = 0
    now: int = 0
    txf: list = field(default_factory=list)
    rxf: list = field(default_factory=list)

    # decoded configuration fields
    @property
    def origin(self): return self.prog & 63
    @property
    def wrap_bottom(self): return (self.prog >> 6) & 31
    @property
    def wrap_top(self): return (self.prog >> 11) & 31
    @property
    def out_base(self): return self.pinmap & 15
    @property
    def set_base(self): return (self.pinmap >> 4) & 15
    @property
    def side_base(self): return (self.pinmap >> 8) & 15
    @property
    def in_base(self): return (self.pinmap >> 12) & 15
    @property
    def out_count(self): return nfield(self.pincfg)
    @property
    def set_count(self): return min((self.pincfg >> 4) & 7, 5)
    @property
    def side_count(self): return (self.pincfg >> 7) & 3
    @property
    def side_pindir(self): return (self.pincfg >> 9) & 1
    @property
    def jmp_pin(self): return (self.pincfg >> 12) & 15
    @property
    def in_right(self): return self.shift & 1
    @property
    def out_right(self): return (self.shift >> 1) & 1
    @property
    def autopush(self): return (self.shift >> 2) & 1
    @property
    def autopull(self): return (self.shift >> 3) & 1
    @property
    def push_thresh(self): return nfield(self.shift >> 4)
    @property
    def pull_thresh(self): return nfield(self.shift >> 8)
    @property
    def crc_in(self): return (self.shift >> 12) & 1
    @property
    def crc_out(self): return (self.shift >> 13) & 1

    def status(self):
        return (self.pc | len(self.txf) << 5 | len(self.rxf) << 8 | self.stalled << 11
                | self.timed_out << 12 | self.rx_ovf << 13 | self.tx_ovf << 14)

    def restart(self):
        self.pc = self.delay = self.isr = self.isr_cnt = self.osr = 0
        self.osr_cnt = 16
        self.armed = self.to_cnt = self.time = self.divcnt = self.stalled = 0

    def source(self, src, pin_in):
        return [rotr(pin_in, self.in_base), self.x, self.y, 0,
                self.crc, self.now, self.isr, self.osr][src]

    def execute(self, pin_in, flags, imem, fx):
        """Run one tick. Mutates self; side effects on shared state go in fx."""
        self.now = self.time  # TIME as read by this tick's instruction
        self.time = (self.time + 1) & M16
        if self.delay:
            self.delay -= 1
            self.stalled = 0
            return

        ins = imem[(self.origin + self.pc) & 63]
        op = ins >> 13
        sc = self.side_count
        sd = (ins >> 8) & 31
        dly = sd & mask_n(5 - sc)
        pin = lambda i: (pin_in >> (i & 15)) & 1
        rel = lambda i: pin(self.in_base + i)

        done = True
        trapped = False
        jump = None
        pins = Effects()  # instruction's own pin writes, merged under side-set

        if op == 0:  # JMP
            cond, addr = (ins >> 5) & 7, ins & 31
            take = [True, self.x == 0, self.x != 0, self.y == 0, self.y != 0,
                    self.x != self.y, pin(self.jmp_pin) == 1,
                    self.osr_cnt < self.pull_thresh][cond]
            if cond == 2:
                self.x = (self.x - 1) & M16
            if cond == 4:
                self.y = (self.y - 1) & M16
            if take:
                jump = addr

        elif op == 1:  # WAIT
            pol, src, tmo, idx = (ins >> 7) & 1, (ins >> 5) & 3, (ins >> 4) & 1, ins & 15
            if src == 0:
                done = pin(idx) == pol
            elif src == 1:
                done = rel(idx) == pol
            elif src == 2:
                done = ((flags >> (idx & 7)) & 1) == pol
                if done and pol:
                    fx.flag_clr |= 1 << (idx & 7)
            else:
                p = rel(idx)
                done = bool(self.armed) and p == pol
                if not done and p != pol:
                    self.armed = 1
            trapped = not done and tmo and self.to_cnt == self.timeout

        elif op == 2:  # IN
            src, n = (ins >> 5) & 7, nfield(ins)
            data = self.source(src, pin_in) & mask_n(n)
            if self.in_right:
                nisr = ((self.isr >> n) | (data << (16 - n))) & M16
            else:
                nisr = ((self.isr << n) | data) & M16
            ncnt = min(self.isr_cnt + n, 16)
            if self.autopush and ncnt >= self.push_thresh:
                if len(self.rxf) >= FIFO_DEPTH:
                    done = False
                else:
                    fx.rx_push = nisr
                    self.isr, self.isr_cnt = 0, 0
            else:
                self.isr, self.isr_cnt = nisr, ncnt
            if done and self.crc_in and n == 1:
                self.crc = crc_step(self.crc, self.crcpoly, data)

        elif op == 3:  # OUT
            dst, n = (ins >> 5) & 7, nfield(ins)
            if self.autopull and self.osr_cnt >= self.pull_thresh:
                if not self.txf:
                    done = False
                else:
                    src, base = self.txf[0], 0
                    fx.tx_pop = True
            else:
                src, base = self.osr, self.osr_cnt
            if done:
                if self.out_right:
                    data = src & mask_n(n)
                    self.osr = src >> n
                else:
                    data = src >> (16 - n)
                    self.osr = (src << n) & M16
                self.osr_cnt = min(base + n, 16)
                if dst == 0:
                    pins.write_pins(0, self.out_base, n, data)
                elif dst == 1:
                    self.x = data
                elif dst == 2:
                    self.y = data
                elif dst == 4:
                    pins.write_pins(1, self.out_base, n, data)
                elif dst == 5:
                    jump = data & 31
                elif dst == 6:
                    self.isr, self.isr_cnt = data, n
                if self.crc_out and n == 1:
                    self.crc = crc_step(self.crc, self.crcpoly, data)

        elif op == 4:  # PUSH / PULL
            is_pull, cond, block = (ins >> 7) & 1, (ins >> 6) & 1, (ins >> 5) & 1
            if not is_pull:
                if cond and self.isr_cnt < self.push_thresh:
                    pass
                elif len(self.rxf) >= FIFO_DEPTH:
                    if block:
                        done = False
                    else:
                        self.isr, self.isr_cnt = 0, 0
                        self.rx_ovf = 1
                else:
                    fx.rx_push = self.isr
                    self.isr, self.isr_cnt = 0, 0
            else:
                if cond and self.osr_cnt < self.pull_thresh:
                    pass
                elif not self.txf:
                    if block:
                        done = False
                    else:
                        self.osr, self.osr_cnt = self.x, 0
                else:
                    self.osr, self.osr_cnt = self.txf[0], 0
                    fx.tx_pop = True

        elif op == 5:  # MOV
            dst, mop, src = (ins >> 5) & 7, (ins >> 3) & 3, ins & 7
            v = self.source(src, pin_in)
            if mop == 1:
                v = ~v & M16
            elif mop == 2:
                v = bitrev16(v)
            if dst == 0:
                pins.write_pins(0, self.out_base, self.out_count, v)
            elif dst == 1:
                self.x = v
            elif dst == 2:
                self.y = v
            elif dst == 3:
                self.crc = v
            elif dst == 4:
                pins.write_pins(1, self.out_base, self.out_count, v)
            elif dst == 5:
                jump = v & 31
            elif dst == 6:
                self.isr, self.isr_cnt = v, 0
            elif dst == 7:
                self.osr, self.osr_cnt = v, 0

        elif op == 7:  # SET
            dst, imm = (ins >> 5) & 7, ins & 31
            if dst == 0:
                pins.write_pins(0, self.set_base, self.set_count, imm)
            elif dst == 1:
                self.x = imm
            elif dst == 2:
                self.y = imm
            elif dst == 3:
                if imm & 16:
                    fx.flag_clr |= 1 << (imm & 7)
                else:
                    fx.flag_set |= 1 << (imm & 7)
            elif dst == 4:
                pins.write_pins(1, self.set_base, self.set_count, imm)

        # op 6 is reserved and completes as a NOP

        # Instruction pin writes, then side-set on top (side-set wins).
        fx.out_mask, fx.out_val = pins.out_mask, pins.out_val
        fx.oe_mask, fx.oe_val = pins.oe_mask, pins.oe_val
        if sc:
            fx.write_pins(self.side_pindir, self.side_base, sc, sd >> (5 - sc))

        if done:
            if jump is not None:
                self.pc = jump
            elif self.pc == self.wrap_top:
                self.pc = self.wrap_bottom
            else:
                self.pc = (self.pc + 1) & 31
            self.delay = dly
            self.to_cnt = 0
            self.armed = 0
            self.stalled = 0
        elif trapped:
            self.pc = self.trap
            self.delay = 0
            self.to_cnt = 0
            self.armed = 0
            self.timed_out = 1
            self.stalled = 1
        else:
            self.to_cnt = (self.to_cnt + 1) & M16
            self.stalled = 1


class Core:
    """The PEMU core: instruction memory, two engines, pins and flags."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.imem = [0] * 64
        self.engines = [Engine(), Engine()]
        self.ctrl = 0
        self.flags = 0
        self.pin_out = 0
        self.pin_oe = 0
        self.sync1 = 0
        self.sync2 = 0

    # ----- combinational views -----

    @property
    def loopback(self):
        return (self.ctrl >> 2) & 1

    def pin_in(self):
        if self.loopback:
            low = ((self.pin_out & self.pin_oe) | ~self.pin_oe) & 0xFF
            return (self.pin_out & 0xFF00) | low
        return self.sync2

    def outputs(self):
        """(uio_out, uio_oe, uo_out[6:0]) as driven by the core."""
        return self.pin_out & 0xFF, self.pin_oe & 0xFF, (self.pin_out >> 8) & 0x7F

    def read(self, addr):
        addr &= 0x7F
        if addr < 0x40:
            return self.imem[addr]
        if addr < 0x60:
            e = self.engines[(addr >> 4) & 1]
            off = addr & 15
            return {
                R_CLKDIV: e.clkdiv, R_PROG: e.prog, R_PINMAP: e.pinmap,
                R_PINCFG: e.pincfg, R_SHIFT: e.shift, R_TIMEOUT: e.timeout,
                R_TRAP: e.trap, R_CRCPOLY: e.crcpoly, R_CRC: e.crc, R_X: e.x,
                R_Y: e.y, R_ISR: e.isr, R_OSR: e.osr, R_STATUS: e.status(),
                R_TIME: e.time, R_FIFO: e.rxf[0] if e.rxf else 0,
            }[off]
        return {
            A_CTRL: self.ctrl & 7, A_FLAGS: self.flags, A_PIN_IN: self.pin_in(),
            A_PIN_OUT: self.pin_out, A_PIN_OE: self.pin_oe, A_ID: CHIP_ID,
        }.get(addr, 0)

    # ----- one clock -----

    def clock(self, ui_in=0, uio_in=0, we=False, re=False, addr=0, wdata=0, rc=False):
        """Advance one clock. Returns the read data for this cycle's bus address."""
        addr &= 0x7F
        wdata &= M16
        rdata = self.read(addr)
        pin_in = self.pin_in()
        ctrl_w = we and addr == A_CTRL
        restart = [ctrl_w and (wdata >> (4 + i)) & 1 for i in range(2)]
        step = [ctrl_w and (wdata >> (6 + i)) & 1 for i in range(2)]
        flush = [ctrl_w and (wdata >> (8 + i)) & 1 for i in range(2)]

        fxs = []
        for i, e in enumerate(self.engines):
            fx = Effects()
            fxs.append(fx)
            en = (self.ctrl >> i) & 1
            if restart[i]:
                e.restart()
                continue
            if en:
                tick = e.divcnt >= e.clkdiv
                e.divcnt = 0 if tick else (e.divcnt + 1) & M16
            else:
                e.divcnt = 0
                tick = step[i]
            if tick:
                e.execute(pin_in, self.flags, self.imem, fx)

        # Shared state: engine 0, then engine 1, then the host.
        pin_out, pin_oe, flags = self.pin_out, self.pin_oe, self.flags
        for fx in fxs:
            pin_out = (pin_out & ~fx.out_mask) | (fx.out_val & fx.out_mask)
            pin_oe = (pin_oe & ~fx.oe_mask) | (fx.oe_val & fx.oe_mask)
            flags = (flags & ~fx.flag_clr) | fx.flag_set

        # FIFOs
        for i, (e, fx) in enumerate(zip(self.engines, fxs)):
            base = engine_base(i)
            tx_push = we and addr == base + R_FIFO
            rx_load = re and addr == base + R_FIFO
            rx_commit = rc and addr == base + R_FIFO
            txf, rxf = e.txf, e.rxf
            rx_pop = rx_commit and e.pop_armed and len(rxf) > 0
            if rx_load:
                e.pop_armed = int(len(rxf) > 0)
            elif rx_commit:
                e.pop_armed = 0
            ntx = txf[1:] if fx.tx_pop else list(txf)
            if tx_push:
                if len(txf) < FIFO_DEPTH:
                    ntx.append(wdata)
                else:
                    e.tx_ovf = 1
            nrx = rxf[1:] if (rx_pop and rxf) else list(rxf)
            if fx.rx_push is not None:
                nrx.append(fx.rx_push)
            if flush[i]:
                ntx, nrx = [], []
            e.txf, e.rxf = ntx, nrx

        # Host writes override everything else this cycle.
        if we:
            if addr < 0x40:
                self.imem[addr] = wdata
            elif addr < 0x60:
                e = self.engines[(addr >> 4) & 1]
                off = addr & 15
                if off == R_CLKDIV: e.clkdiv = wdata
                elif off == R_PROG: e.prog = wdata
                elif off == R_PINMAP: e.pinmap = wdata
                elif off == R_PINCFG: e.pincfg = wdata & PINCFG_MASK
                elif off == R_SHIFT: e.shift = wdata & SHIFT_MASK
                elif off == R_TIMEOUT: e.timeout = wdata
                elif off == R_TRAP: e.trap = wdata & 31
                elif off == R_CRCPOLY: e.crcpoly = wdata
                elif off == R_CRC: e.crc = wdata
                elif off == R_X: e.x = wdata
                elif off == R_Y: e.y = wdata
                elif off == R_STATUS:
                    if wdata >> 12 & 1: e.timed_out = 0
                    if wdata >> 13 & 1: e.rx_ovf = 0
                    if wdata >> 14 & 1: e.tx_ovf = 0
            elif addr == A_CTRL:
                self.ctrl = wdata & 7
            elif addr == A_FLAGS:
                flags = wdata & 0xFF
            elif addr == A_PIN_OUT:
                pin_out = wdata
            elif addr == A_PIN_OE:
                pin_oe = wdata
            elif addr == A_PIN_SET:
                pin_out |= wdata
            elif addr == A_PIN_CLR:
                pin_out &= ~wdata & M16

        self.pin_out, self.pin_oe, self.flags = pin_out, pin_oe, flags
        self.sync2 = self.sync1
        self.sync1 = ((ui_in & 0xFF) << 8) | (uio_in & 0xFF)
        return rdata
