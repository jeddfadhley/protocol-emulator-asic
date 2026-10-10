"""Pin-level reference models of external devices, stepped once per clock.

Each device sees the resolved wire values (a 16-bit int, see `Wires`) and
returns what it drives. Devices are independent of the PEMU implementation,
so the same models check the Python model and the RTL.

Wire resolution (see `resolve`): GPIO pins 0-7 are open-collector buses with
pull-ups. A pin reads 0 if the chip drives it low or any device pulls it low.
Pins 8-14 are the chip's dedicated outputs. Devices drive the dedicated
inputs (`ui_in`) through `ui_drive`.
"""


def resolve(pin_out, pin_oe, dev_low, ui_drive):
    """Return (wires, uio_in, ui_in).

    pin_out/pin_oe: the chip's 16-bit pin registers.
    dev_low: bitmask of GPIO pins (0-7) some device is pulling low.
    ui_drive: 8-bit value devices drive on ui_in.
    """
    chip = ((pin_out & pin_oe) | ~pin_oe) & 0xFF
    uio = chip & ~dev_low & 0xFF
    wires = uio | (pin_out & 0x7F00) | ((ui_drive & 0x80) << 8)
    return wires, uio, ui_drive & 0xFF


def bit(v, i):
    return (v >> i) & 1


class UartSink:
    """Decodes 8N1 frames on a wire. Checks start/stop bits and timing."""

    def __init__(self, pin, cycles_per_bit):
        self.pin = pin
        self.cpb = cycles_per_bit
        self.bytes = []
        self.errors = []
        self.t = 0
        self.state = "idle"
        self.count = 0
        self.prev = 0  # the line must idle high before a start bit counts

    def step(self, wires):
        v = bit(wires, self.pin)
        if self.state == "idle":
            if self.prev == 1 and v == 0:
                self.state, self.count, self.sample_at = "frame", 0, self.cpb // 2
                self.value, self.nbit = 0, 0
        else:
            self.count += 1
            if self.count == self.sample_at:
                if self.nbit == 0:
                    if v != 0:
                        self.errors.append(f"glitch at {self.t}")
                        self.state = "idle"
                elif self.nbit <= 8:
                    self.value |= v << (self.nbit - 1)
                else:
                    if v != 1:
                        self.errors.append(f"framing error at {self.t}")
                    self.bytes.append(self.value)
                    self.state = "idle"
                self.nbit += 1
                self.sample_at += self.cpb
        self.prev = v
        self.t += 1
        return 0


class UartSource:
    """Drives 8N1 frames from a byte queue. A ui_in device: returns the level."""

    def __init__(self, cycles_per_bit, data=(), gap=0, bad_stop=()):
        self.cpb = cycles_per_bit
        self.queue = list(data)
        self.gap = gap
        self.bad_stop = set(bad_stop)  # indices of bytes sent with a low stop bit
        self.bits = []
        self.count = 0
        self.level = 1
        self.sent = 0

    def step(self, wires):
        if self.count == 0:
            if not self.bits and self.queue:
                b = self.queue.pop(0)
                stop = 0 if self.sent in self.bad_stop else 1
                self.sent += 1
                self.bits = [0] + [(b >> i) & 1 for i in range(8)] + [stop] + [1] * self.gap
            if self.bits:
                self.level = self.bits.pop(0)
                self.count = self.cpb
            else:
                self.level = 1
        if self.count:
            self.count -= 1
        return self.level


class SpiSlave:
    """SPI mode 0 slave. Samples MOSI on SCK rising, shifts MISO on falling.

    Records whole bytes received while CS is low and answers with bytes from
    `responses` (0xFF when empty). A GPIO device: returns its pull-low mask
    for MISO (pass miso=None to leave MISO undriven).
    """

    def __init__(self, sck, mosi, miso, cs=None, responses=()):
        self.sck, self.mosi, self.miso, self.cs = sck, mosi, miso, cs
        self.responses = list(responses)
        self.received = []
        self.prev_sck = 0
        self.rx = 0
        self.nbits = 0
        self.tx = self._next()
        self.out = (self.tx >> 7) & 1

    def _next(self):
        return self.responses.pop(0) if self.responses else 0xFF

    def selected(self, wires):
        return self.cs is None or bit(wires, self.cs) == 0

    def step(self, wires):
        sck = bit(wires, self.sck)
        if self.selected(wires):
            if sck and not self.prev_sck:
                self.rx = ((self.rx << 1) | bit(wires, self.mosi)) & 0xFF
                self.nbits += 1
                if self.nbits == 8:
                    self.received.append(self.rx)
                    self.nbits = 0
            if not sck and self.prev_sck:
                if self.nbits == 0:
                    self.tx = self._next()
                    self.out = (self.tx >> 7) & 1
                else:
                    self.out = (self.tx >> (7 - self.nbits)) & 1
        self.prev_sck = sck
        if self.miso is None:
            return 0
        return (1 - self.out) << self.miso


class I2cTarget:
    """I2C target with an 8-bit register pointer (24C02-style EEPROM).

    Write: [addr+W] [ptr] [data...]. Read: [addr+R] then data from ptr.
    Optionally stretches SCL for `stretch` clocks after each ACK bit, or
    holds SCL low forever when `hang` is set (to test the bus timeout).
    Returns a bitmask of the GPIO pins it pulls low.
    """

    def __init__(self, sda, scl, address=0x50, size=256, stretch=0):
        self.sda, self.scl = sda, scl
        self.address = address
        self.mem = [0] * size
        self.ptr = 0
        self.stretch = stretch
        self.hang = False
        self.prev_sda = 1
        self.prev_scl = 1
        self.state = "idle"
        self.drive_sda = 0
        self.stretching = 0
        self.log = []  # ("start" | "stop" | ("byte", v, acked))

    def _begin_byte(self):
        self.shift = 0
        self.nbit = 0

    def step(self, wires):
        sda, scl = bit(wires, self.sda), bit(wires, self.scl)
        if scl and self.prev_scl and self.prev_sda and not sda:
            self.log.append("start")
            self.state = "addr"
            self._begin_byte()
            self.drive_sda = 0
        elif scl and self.prev_scl and not self.prev_sda and sda:
            self.log.append("stop")
            self.state = "idle"
            self.drive_sda = 0
        elif scl and not self.prev_scl:
            self._rising(sda)
        elif not scl and self.prev_scl:
            self._falling()
        self.prev_sda, self.prev_scl = sda, scl
        hold_scl = self.hang or self.stretching > 0
        if self.stretching:
            self.stretching -= 1
        return (self.drive_sda << self.sda) | (hold_scl << self.scl)

    def _rising(self, sda):
        if self.state in ("addr", "write", "read"):
            if self.nbit < 8:
                self.shift = (self.shift << 1) | sda
            self.nbit += 1
            if self.nbit == 9:
                if self.state == "read":
                    acked = sda == 0
                    self.log.append(("byte", self.last_read, acked))
                    self.state = "read" if acked else "done"
                else:
                    self.log.append(("byte", self.shift & 0xFF, self.acking))

    def _falling(self):
        if self.state not in ("addr", "write", "read"):
            self.drive_sda = 0
            return
        if self.nbit == 8:
            # Drive the ACK bit (for addr/write) or release for the master's ACK.
            if self.state == "addr":
                b = self.shift & 0xFF
                self.acking = (b >> 1) == self.address
                self.rw = b & 1
            elif self.state == "write":
                self.acking = True
                if self.first_write:
                    self.ptr = b = self.shift & 0xFF
                    self.first_write = False
                else:
                    self.mem[self.ptr % len(self.mem)] = self.shift & 0xFF
                    self.ptr += 1
            else:
                self.acking = False
            self.drive_sda = int(self.state != "read" and self.acking)
        elif self.nbit == 9:
            self.stretching = self.stretch
            if self.state == "addr":
                if not self.acking:
                    self.state = "done"
                elif self.rw:
                    self.state = "read"
                else:
                    self.state = "write"
                    self.first_write = True
            if self.state == "done":
                self.drive_sda = 0
                return
            self._begin_byte()
            if self.state == "read":
                self.last_read = self.mem[self.ptr % len(self.mem)]
                self.ptr += 1
            self._drive_read_bit()
        else:
            self._drive_read_bit()

    def _drive_read_bit(self):
        if self.state == "read" and self.nbit < 8:
            self.drive_sda = 1 - ((self.last_read >> (7 - self.nbit)) & 1)
        else:
            self.drive_sda = 0


class SquareWave:
    """Drives a ui_in bit with a repeating list of (level, cycles) segments."""

    def __init__(self, segments):
        self.segments = list(segments)
        self.i = 0
        self.left = self.segments[0][1]

    def step(self, wires):
        level, _ = self.segments[self.i]
        self.left -= 1
        if self.left == 0:
            self.i = (self.i + 1) % len(self.segments)
            self.left = self.segments[self.i][1]
        return level


class PulseRecorder:
    """Records (level, length) runs on a wire."""

    def __init__(self, pin):
        self.pin = pin
        self.runs = []
        self.level = None
        self.count = 0

    def step(self, wires):
        v = bit(wires, self.pin)
        if v == self.level:
            self.count += 1
        else:
            if self.level is not None:
                self.runs.append((self.level, self.count))
            self.level, self.count = v, 1
        return 0
