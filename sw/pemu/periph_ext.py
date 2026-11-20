"""Device models for the protocols added after the hardware was frozen
(see docs/new-protocols.md). Same conventions as periph.py: stepped once per
clock with the resolved wires; GPIO devices return the mask of pins they pull
low, ui_in devices return the level they drive.

Each model checks the timing rules of its protocol and records violations in
`errors`, so a scenario fails if the firmware is merely close.
"""

from .periph import bit


# ----- 1-Wire -----

def crc8_maxim(data):
    """Dallas/Maxim CRC-8 (x^8 + x^5 + x^4 + 1, reflected)."""
    crc = 0
    for b in data:
        for _ in range(8):
            mix = (crc ^ b) & 1
            crc >>= 1
            if mix:
                crc ^= 0x8C
            b >>= 1
    return crc


class OneWireDevice:
    """A DS18B20-style temperature sensor on a 1-Wire bus (a GPIO device).

    `us` is the number of clocks per microsecond of protocol time, so tests
    can run the bus faster than real time with the firmware's CLKDIV scaled
    to match. Supports READ ROM (0x33), MATCH ROM (0x55), SKIP ROM (0xCC),
    CONVERT T (0x44), READ SCRATCHPAD (0xBE) and WRITE SCRATCHPAD (0x4E).
    """

    def __init__(self, pin, us, rom_body=bytes([0x28, 0xA1, 0xB2, 0xC3, 0xD4, 0xE5, 0xF6]),
                 temp_c=25.0625):
        self.pin, self.us = pin, us
        self.rom = rom_body + bytes([crc8_maxim(rom_body)])
        self.temp_raw = int(round(temp_c * 16)) & 0xFFFF
        self.scratch = bytearray([0x50, 0x05, 0x4B, 0x46, 0x7F, 0xFF, 0x0C, 0x10])  # 85 C at power-up
        self.errors, self.log = [], []
        self.t = 0
        self.prev = 1
        self.fell = None         # time the line last fell
        self.rose = 0            # time the line last rose
        self.pull_until = -1     # we hold the line low until this time
        self.pull_from = -1
        self.sample_at = None    # when to sample a master write slot
        self.state = "idle"
        self.rx, self.nrx = 0, 0
        self.tx = []             # bits we send in the coming read slots

    def _start_tx(self, data):
        for byte in data:
            self.tx += [(byte >> i) & 1 for i in range(8)]

    def step(self, wires):
        us, t = self.us, self.t
        v = bit(wires, self.pin)
        if self.prev == 1 and v == 0 and not (self.pull_from <= t <= self.pull_until):
            if self.fell is None and t - self.rose < 1 * us and self.rose:
                self.errors.append(f"recovery {t - self.rose} clocks at {t}")
            self.fell, self.drove = t, False
            if self.state == "send":
                b = self.tx.pop(0) if self.tx else 1
                if b == 0:
                    self.pull_from, self.pull_until = t, t + 30 * us
                    self.drove = True
                if not self.tx:
                    self.state = "idle"
            elif self.state != "idle":
                self.sample_at = t + 30 * us
        if self.prev == 0 and v == 1 and self.fell is not None:
            low = t - self.fell
            self.fell, self.rose = None, t
            if low >= 480 * us:
                self.log.append("reset")
                self.state, self.rx, self.nrx, self.tx = "rom", 0, 0, []
                self.sample_at = None
                self.pull_from, self.pull_until = t + 30 * us, t + 150 * us
            elif self.drove:
                pass  # our own 0 bit held the line low
            elif low > 120 * us:
                self.errors.append(f"low for {low / us:.1f} us at {t}")
            elif 15 * us < low < 60 * us:
                self.errors.append(f"ambiguous slot, low for {low / us:.1f} us at {t}")
        if self.sample_at is not None and t == self.sample_at:
            self.sample_at = None
            self._bit(v)
        self.prev = v
        self.t += 1
        return int(self.pull_from <= t < self.pull_until) << self.pin

    def _bit(self, v):
        self.rx |= v << self.nrx
        self.nrx += 1
        need = 64 if self.state == "match" else 8
        if self.nrx < need:
            return
        value, self.rx, self.nrx = self.rx, 0, 0
        if self.state == "rom":
            self.log.append(("rom", value))
            if value == 0x33:
                self.state = "send"
                self._start_tx(self.rom)
            elif value == 0xCC:
                self.state = "func"
            elif value == 0x55:
                self.state = "match"
            else:
                self.state = "idle"
        elif self.state == "match":
            self.state = "func" if value == int.from_bytes(self.rom, "little") else "idle"
        elif self.state == "func":
            self.log.append(("func", value))
            if value == 0x44:
                self.scratch[0:2] = self.temp_raw.to_bytes(2, "little")
                self.state = "idle"
            elif value == 0xBE:
                self.state = "send"
                self._start_tx(bytes(self.scratch) + bytes([crc8_maxim(self.scratch)]))
            elif value == 0x4E:
                self.state, self.wbytes = "wscratch", []
            else:
                self.state = "idle"
        elif self.state == "wscratch":
            self.wbytes.append(value)
            if len(self.wbytes) == 3:
                self.scratch[2:5] = bytes(self.wbytes)
                self.state = "idle"


# ----- NEC infrared -----

class NecRemote:
    """An IR receiver module's output (a ui_in device): low during bursts.

    `unit` is the NEC unit (562.5 us) in clocks, and `scale` stretches every
    duration (1.03 = the remote's oscillator 3% slow). `events` is a list of
    ("frame", address, command), ("repeat",), ("noise", clocks low) and
    ("partial", address, command, bits), each followed by `gap` units of idle,
    after `lead` units of idle (time for the host to set the chip up).
    """

    def __init__(self, unit, events, gap=60, scale=1.0, lead=0):
        u = unit * scale
        self.segs = [(1, lead * u)] if lead else []   # (level, clocks); idle first
        for ev in events:
            if ev[0] == "frame" or ev[0] == "partial":
                addr, cmd = ev[1], ev[2]
                word = addr | (~addr & 0xFF) << 8 | cmd << 16 | (~cmd & 0xFF) << 24
                nbits = ev[3] if ev[0] == "partial" else 32
                self.segs += [(0, 16 * u), (1, 8 * u)]
                for i in range(nbits):
                    self.segs += [(0, u), (1, 3 * u if (word >> i) & 1 else u)]
                if ev[0] == "frame":
                    self.segs += [(0, u)]
            elif ev[0] == "repeat":
                self.segs += [(0, 16 * u), (1, 4 * u), (0, u)]
            elif ev[0] == "noise":
                self.segs += [(0, ev[1])]
            self.segs += [(1, gap * u)]
        self.t = 0.0
        self.i = 0
        self.edge = self.segs[0][1] if self.segs else 0
        self.done = not self.segs

    def step(self, wires):
        if self.done:
            return 1
        level = self.segs[self.i][0]
        self.t += 1
        if self.t >= self.edge:
            self.i += 1
            if self.i == len(self.segs):
                self.done = True
            else:
                self.edge += self.segs[self.i][1]
        return level


# ----- PS/2 -----

def odd_parity(byte):
    return 1 ^ (bin(byte).count("1") & 1)


class Ps2Keyboard:
    """A PS/2 keyboard (a GPIO device on DATA and CLK, both open drain).

    `half` is half a clock period and `us` one microsecond, in clocks. Sends
    the bytes in `keys` (scan codes) and answers host commands: 0xED (set
    LEDs, then the LED byte), 0xF2 (identify) and 0xFF (reset), each with
    0xFA (ACK). A send is aborted and retried if the host inhibits the bus
    (holds CLK low) before the 10th clock, as the protocol requires.
    """

    def __init__(self, data, clk, half, us, keys=()):
        self.data, self.clk, self.half, self.us = data, clk, half, us
        self.queue = list(keys)
        self.sent, self.received, self.errors = [], [], []
        self.leds = None
        self.aborts = 0
        self.pull = 0
        self.w = 0xFFFF
        self.gen = self._run()

    def step(self, wires):
        self.w = wires
        next(self.gen)
        return self.pull

    def _line(self, pin):
        return bit(self.w, pin)

    def _set(self, pin, low):
        self.pull = (self.pull & ~(1 << pin)) | (int(low) << pin)

    def _wait(self, n):
        for _ in range(int(n)):
            yield

    def _run(self):
        idle = 0
        while True:
            if not self._line(self.clk):
                yield from self._host_request()
                idle = 0
            elif self.queue and idle >= 50 * self.us:
                yield from self._send()
                idle = 0
            else:
                idle = idle + 1 if self._line(self.data) else 0
                yield

    def _send(self):
        byte = self.queue[0]
        bits = [0] + [(byte >> i) & 1 for i in range(8)] + [odd_parity(byte), 1]
        for i, b in enumerate(bits):
            self._set(self.data, not b)
            yield from self._wait(self.half // 2)
            if not self._line(self.clk):       # the host is inhibiting: give up
                self._set(self.data, False)
                if i < 10:
                    self.aborts += 1
                    return
            self._set(self.clk, True)
            yield from self._wait(self.half)
            self._set(self.clk, False)
            yield from self._wait(self.half - self.half // 2)
        self._set(self.data, False)
        self.queue.pop(0)
        self.sent.append(byte)

    def _host_request(self):
        low = 0
        while not self._line(self.clk):
            low += 1
            yield
        if self._line(self.data):
            return                              # an inhibit with no request
        if low < 100 * self.us:
            self.errors.append(f"CLK held low {low / self.us:.0f} us before the request")
        yield from self._wait(20 * self.us)
        bits = []
        for _ in range(10):                     # data, parity, stop
            self._set(self.clk, True)
            yield from self._wait(self.half)
            self._set(self.clk, False)
            yield from self._wait(2)
            bits.append(self._line(self.data))  # sampled just after the rising edge
            yield from self._wait(self.half - 2)
        byte = sum(b << i for i, b in enumerate(bits[:8]))
        if bits[8] != odd_parity(byte):
            self.errors.append(f"parity error on {byte:#04x}")
        if bits[9] != 1:
            self.errors.append(f"stop bit 0 after {byte:#04x}")
        self._set(self.data, True)              # ACK: DATA low during an 11th clock
        yield from self._wait(self.half // 2)
        self._set(self.clk, True)
        yield from self._wait(self.half)
        self._set(self.clk, False)
        yield from self._wait(self.half // 2)
        self._set(self.data, False)
        self.received.append(byte)
        self._command(byte)

    def _command(self, byte):
        if getattr(self, "_expect_leds", False):
            self.leds, self._expect_leds = byte, False
            self.queue.insert(0, 0xFA)
        elif byte == 0xED:
            self._expect_leds = True
            self.queue.insert(0, 0xFA)
        elif byte == 0xF2:
            self.queue[0:0] = [0xFA, 0xAB, 0x83]
        elif byte == 0xFF:
            self.queue[0:0] = [0xFA, 0xAA]
        else:
            self.queue.insert(0, 0xFE)          # resend: unknown command


# ----- ARM Serial Wire Debug -----

SWD_OK, SWD_WAIT, SWD_FAULT = 0b001, 0b010, 0b100


def parity(value):
    return bin(value).count("1") & 1


class SwdTarget:
    """An ARM SW-DP with a MEM-AP in front of a small RAM (a GPIO device).

    Samples SWDIO on SWCLK rising edges and changes it just after them. It
    pulls SWDIO low for 0 bits and otherwise leaves it to the pull-up.
    Implements line reset (50+ ones), the rule that IDCODE must be read
    first after a reset, IDCODE, CTRL/STAT (power-up handshake, WDATAERR,
    STICKYERR), ABORT, SELECT, RDBUFF, and the AP registers CSW, TAR, DRW
    and IDR, with posted AP reads and TAR auto-increment.
    """

    IDCODE = 0x2BA01477
    AP_IDR = 0x24770011

    def __init__(self, swclk, swdio):
        self.swclk, self.swdio = swclk, swdio
        self.prev_clk = 0
        self.ones = 0
        self.locked = True       # until IDCODE is read after a line reset
        self.state = "idle"
        self.out = 1
        self.errors, self.log = [], []
        self.ctrl = 0
        self.select = 0
        self.csw = 0x03000052
        self.tar = 0
        self.rdbuff = 0
        self.mem = {}

    def step(self, wires):
        clk, dio = bit(wires, self.swclk), bit(wires, self.swdio)
        if clk and not self.prev_clk:
            self._rise(dio)
        self.prev_clk = clk
        return int(self.out == 0) << self.swdio

    def _rise(self, dio):
        st = self.state
        self.ones = self.ones + 1 if dio and st in ("idle", "req") else 0
        if self.ones >= 50:
            if st != "reset":
                self.log.append("line reset")
            self.state, self.locked, self.out = "reset", True, 1
            return
        if st in ("idle", "reset"):
            if st == "reset" and dio:
                return                  # still in the reset sequence
            if dio:
                self.state, self.req = "req", [1]
            else:
                self.state = "idle"
            return
        if st == "req":
            self.req.append(dio)
            if len(self.req) == 8:
                self._request()
            return
        if st == "turn":                # the turnaround edge: nobody drives
            self.state = "ack"
            st = "ack"
        if st == "ack":                 # after the turnaround edge: drive the ACK
            self.out = (self.ack >> self.n) & 1
            self.n += 1
            if self.n == 3:
                if self.ack != SWD_OK:
                    self.state, self.n = "release", 0
                elif self.rnw:
                    self.state, self.n = "rdata", 0
                    self.bits = [(self.data >> i) & 1 for i in range(32)] + [parity(self.data)]
                else:
                    self.state, self.n = "wtrn", 0
            return
        if st == "release":             # a turnaround before the host drives again
            self.out, self.state = 1, "trn_end"
            return
        if st == "trn_end":             # the turnaround's closing edge carries no bit
            self.state = "idle"
            return
        if st == "rdata":
            self.out = self.bits[self.n]
            self.n += 1
            if self.n == 33:
                self.state = "release"
            return
        if st == "wtrn":
            self.out, self.state, self.bits = 1, "wtrn_end", []
            return
        if st == "wtrn_end":
            self.state = "wdata"
            return
        if st == "wdata":
            self.bits.append(dio)
            if len(self.bits) == 33:
                self.state = "idle"
                value = sum(b << i for i, b in enumerate(self.bits[:32]))
                if self.bits[32] != parity(value):
                    self.ctrl |= 1 << 7     # WDATAERR
                    self.log.append(("parity error", value))
                else:
                    self._write(value)

    def _request(self):
        start, apndp, rnw, a2, a3, par, stop, park = self.req
        self.state, self.n = "ack", -1
        if par != parity(apndp | rnw << 1 | a2 << 2 | a3 << 3) or stop or not park:
            self.state = "idle"         # a protocol error: no response
            self.log.append(("bad request", self.req))
            return
        self.apndp, self.rnw, self.addr = apndp, rnw, a2 << 2 | a3 << 3
        if self.locked and not (not apndp and rnw and self.addr == 0):
            self.state = "idle"         # only IDCODE answers after a line reset
            self.log.append(("locked", self.req))
            return
        self.n = 0
        self.state = "turn"
        sticky = self.ctrl & (1 << 7 | 1 << 5)
        abort = not apndp and not rnw and self.addr == 0   # ABORT is never refused
        self.ack = SWD_FAULT if sticky and not abort else SWD_OK
        if self.ack == SWD_OK and rnw:
            self.data = self._read()
        self.log.append(("ap" if apndp else "dp", "r" if rnw else "w", self.addr,
                         {SWD_OK: "OK", SWD_FAULT: "FAULT"}[self.ack]))

    def _read(self):
        if not self.apndp:
            if self.addr == 0:
                self.locked = False
                return self.IDCODE
            if self.addr == 4:
                return self.ctrl
            if self.addr == 0xC:
                return self.rdbuff
            return 0
        ap_addr = (self.select & 0xF0) | self.addr
        if ap_addr == 0x0C:
            new = self.mem.get(self.tar, 0)
            self._inc()
        elif ap_addr == 0x04:
            new = self.tar
        elif ap_addr == 0x00:
            new = self.csw
        elif ap_addr == 0xFC:
            new = self.AP_IDR
        else:
            new = 0
        old, self.rdbuff = self.rdbuff, new   # posted: the result arrives next time
        return old

    def _write(self, value):
        if not self.apndp:
            if self.addr == 0:          # ABORT
                if value & (1 << 3):
                    self.ctrl &= ~(1 << 7)
                if value & (1 << 2):
                    self.ctrl &= ~(1 << 5)
            elif self.addr == 4:        # CTRL/STAT: power-up requests are acknowledged
                req = value & (5 << 28)
                self.ctrl = (self.ctrl & 0x0FFFFFFF & ~(5 << 28)) | req | req << 1
            elif self.addr == 8:
                self.select = value
            return
        ap_addr = (self.select & 0xF0) | self.addr
        if ap_addr == 0x04:
            self.tar = value
        elif ap_addr == 0x0C:
            self.mem[self.tar] = value
            self._inc()
        elif ap_addr == 0x00:
            self.csw = value

    def _inc(self):
        if (self.csw >> 4) & 3 == 1:    # AddrInc single
            self.tar = (self.tar + 4) & 0xFFFFFFFF


# ----- CAN -----

class CanNode:
    """A CAN 2.0A controller on the bus (see CanBus). Hard-syncs on SOF,
    re-syncs on every recessive-to-dominant edge and samples at 75% of the
    bit, and records an error if an edge lands more than 2 clocks off the
    bit grid (the transmitter's bit time is wrong). As a receiver it destuffs, checks the CRC and drives the ACK slot.
    With frames queued it transmits once the bus has been idle for 11 bits,
    or joins a frame another node starts (as CAN allows), arbitrating bit by
    bit. Decoded frames go to `frames` as (ident, data); `acked` records
    whether each of our own frames was acknowledged.
    """

    def __init__(self, bit_clocks, tx_queue=(), ack=True, name="node", join_only=False):
        from . import can
        self.can = can
        self.B = bit_clocks
        self.sp = (3 * bit_clocks) // 4
        self.queue = list(tx_queue)
        self.do_ack = ack
        self.name = name
        self.join_only = join_only    # only send by joining a frame someone else starts
        self.frames, self.errors, self.acked, self.lost = [], [], [], 0
        self.tx = 1
        self.prev = 1
        self.idle_bits = 11       # start as if the bus had been idle
        self.phase = None         # clocks since the start of the current bit
        self._reset_frame()

    def _reset_frame(self):
        self.bits = []
        self.raw, self.run, self.last, self.need = [], 0, None, None
        self.after_crc = None     # bits sampled after the CRC (delimiter, ACK, ...)
        self.sending = None       # (levels, arbitration length) while transmitting
        self.crc_ok = False
        self.ack_state = None     # "pending": drive the next bit (the ACK slot)

    def step(self, bus):
        if self.phase is None:
            if self.prev == 1 and bus == 0:                 # SOF: hard sync
                self.phase = 0
                if self.queue:                              # join with our own frame
                    self._start_tx()
            elif self.queue and self.idle_bits >= 11 and bus == 1 and not self.join_only:
                self.phase = 0
                self._start_tx()
        else:
            if self.prev == 1 and bus == 0 and self.tx == 1:  # re-sync on others' edges
                err = self.phase if self.phase < self.B // 2 else self.phase - self.B
                if abs(err) > 2:                            # well beyond CAN's ~1.5% clock tolerance
                    self.errors.append(f"{self.name}: edge {err} clocks off the bit grid")
                self.phase = 0
            if self.phase == 0 and self.sending:
                lv, _ = self.sending
                n = len(self.bits)
                self.tx = lv[n] if n < len(lv) else 1
            elif self.phase == 0 and self.ack_state == "pending":
                self.tx, self.ack_state = 0, "on"           # the whole ACK slot
            elif self.phase == 0 and self.ack_state == "on":
                self.tx, self.ack_state = 1, None
            if self.phase == self.sp:
                self._sample(bus)
        if self.phase is None:
            if bus == 1:
                self.idle_bits = min(self.idle_bits + (1 / self.B), 99)
            else:
                self.idle_bits = 0
        else:
            self.phase += 1
            if self.phase == self.B:
                self.phase = 0
        self.prev = bus
        return self.tx

    def _start_tx(self):
        levels, _ = self.can.frame_bits(*self.queue[0])
        arb = len(self.can.stuff(self.can.frame_fields(*self.queue[0])[:13]))
        self.sending = (levels, arb)
        self.tx = levels[0]

    def _sample(self, bus):
        self.bits.append(bus)
        if self.sending:
            lv, arb = self.sending
            i = len(self.bits) - 1
            sent = lv[i] if i < len(lv) else 1
            if sent == 1 and bus == 0 and i != self._ack_index():
                if i < arb:
                    self.lost += 1                          # lost arbitration: listen
                    self.sending, self.tx = None, 1
                else:
                    self.errors.append(f"{self.name}: bit error at {i}")
                    return self._abort()
        if self.after_crc is None:
            self._destuff(bus)
            return
        self.after_crc.append(bus)
        k = len(self.after_crc)
        if k == 1 and self.do_ack and self.crc_ok and not self.sending:
            self.ack_state = "pending"                      # CRC delimiter seen: ACK next
        if k == 2:
            if self.sending:
                self.acked.append(bus == 0)
        if k == 10:
            try:
                ident, data, rtr, ack, _ = self.can.decode(self.bits)
                self.frames.append((ident, data))
                if self.sending:
                    self.queue.pop(0)
            except self.can.CanError as exc:
                self.errors.append(f"{self.name}: {exc}")
            self._end()

    def _ack_index(self):
        if self.after_crc is None:
            return None
        return len(self.bits) - len(self.after_crc)

    def _destuff(self, b):
        if self.run == 5:
            if b == self.last:
                self.errors.append(f"{self.name}: stuff error")
                return self._abort()
            self.run, self.last = 1, b
            if self.need is not None and len(self.raw) == self.need:
                self.after_crc = []
            return
        self.run = self.run + 1 if b == self.last else 1
        self.last = b
        self.raw.append(b)
        if len(self.raw) == 19:
            dlc = min(int("".join(map(str, self.raw[15:19])), 2), 8)
            self.need = 19 + (0 if self.raw[12] else 8 * dlc) + 15
        if self.need is not None and len(self.raw) == self.need:
            fields = self.raw[:-15]
            self.crc_ok = self.can.crc15(fields) == int("".join(map(str, self.raw[-15:])), 2)
            if self.run < 5:
                self.after_crc = []

    def _abort(self):
        self._end()

    def _end(self):
        self.tx = 1
        self.phase = None
        self.idle_bits = 0
        self._reset_frame()


class CanBus:
    """The bus seen through a transceiver: a ui_in device that returns the
    RXD level, the wired-AND of the chip's TXD and every node."""

    def __init__(self, txd_pin, nodes):
        self.txd = txd_pin
        self.nodes = list(nodes)
        self.node_tx = [1] * len(self.nodes)
        self.level = 1

    def step(self, wires):
        level = bit(wires, self.txd)
        for t in self.node_tx:
            level &= t
        self.level = level
        self.node_tx = [n.step(level) for n in self.nodes]
        return level


class SpiController:
    """An SPI controller (mode 0, MSB first) driving a chip that acts as the
    device. `frames` is a list of byte lists, each sent with one CS_N low
    period; a frame given as (bytes, n_bits) stops after n_bits (an aborted
    transfer). Received MISO bytes go to `received`, one list per frame.
    Connect with ports(): (CS_N, SCK, MOSI) devices for bench.ui(). It
    holds CS_N high until `go` is set, so the chip can be set up first."""

    def __init__(self, miso_pin, frames, half=16, gap=40):
        self.miso = miso_pin
        self.half, self.gap = half, gap
        self.frames = list(frames)
        self.received = []
        self.cs, self.sck, self.mosi = 1, 0, 0
        self.t = 0
        self._plan = self._run()
        self.done = False
        self.go = False

    def _run(self):
        while not self.go:
            yield
        for frame in self.frames:
            if isinstance(frame, tuple):
                data, nbits = frame
            else:
                data, nbits = frame, 8 * len(frame)
            yield from self._wait(self.gap)
            self.cs = 0
            got, cur = [], 0
            bits = [(b >> (7 - i)) & 1 for b in data for i in range(8)][:nbits]
            for k, bit in enumerate(bits):
                self.mosi = bit
                yield from self._wait(self.half)
                self.sck = 1
                wires = yield
                cur = cur << 1 | ((wires >> self.miso) & 1)
                if k % 8 == 7:
                    got.append(cur)
                    cur = 0
                yield from self._wait(self.half - 1)
                self.sck = 0
            yield from self._wait(self.half)
            self.cs = 1
            self.received.append(got)
        yield from self._wait(self.gap)
        self.done = True
        while True:
            yield

    def _wait(self, n):
        for _ in range(n):
            yield

    def step(self, wires):
        self._plan.send(wires) if self.t else next(self._plan)
        self.t += 1

    def ports(self):
        ctl = self

        class Port:
            def __init__(self, attr, lead):
                self.attr, self.lead = attr, lead

            def step(self, wires):
                if self.lead:
                    ctl.step(wires)
                return getattr(ctl, self.attr)

        return Port("cs", True), Port("sck", False), Port("mosi", False)
