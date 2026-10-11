"""PEMU host driver for MicroPython on the Tiny Tapeout demo board (RP2040).

Copy pemu_board.py and pemu_demos.py to the board, then:

    >>> import pemu_board
    >>> pemu_board.selftest()          # on-chip UART loopback, no wiring needed
    >>> chip = pemu_board.Pemu(pemu_board.TTBoardSpi())
    >>> chip.uart_print("Hello from PEMU\\r\\n")

The host link is SPI mode 0 on ui_in[4] (CS_N), ui_in[5] (SCK), ui_in[6]
(MOSI) and uo_out[7] (MISO), bit-banged from MicroPython. The chip only
requires SCK <= clk / 8, which bit-banging never approaches.

NOTE: the TTBoardSpi adapter targets the Tiny Tapeout MicroPython SDK
(ttboard). It has not been run on silicon yet; the Pemu class above it is
tested against the reference model (sw/tests/test_board_driver.py).
"""

from pemu_demos import CLK_HZ, DEMOS

A_CTRL, A_FLAGS, A_PIN_IN, A_PIN_OUT, A_PIN_OE, A_ID, A_PIN_SET, A_PIN_CLR = range(0x60, 0x68)
CHIP_ID = 0x5045
R_STATUS, R_FIFO = 0xD, 0xF


def engine_base(e):
    return 0x40 + 16 * e


class TTBoardSpi:
    """Bit-banged SPI over the demo board's ui_in/uo_out pins."""

    CS, SCK, MOSI, MISO = 4, 5, 6, 7

    def __init__(self, project="tt_um_jeddfadhley_protocol_emu", clock_hz=CLK_HZ):
        from ttboard.demoboard import DemoBoard
        tt = DemoBoard.get()
        getattr(tt.shuttle, project).enable()
        tt.clock_project_PWM(clock_hz)
        tt.ui_in.value = 1 << self.CS
        tt.reset_project(True)
        tt.reset_project(False)
        self.tt = tt

    def transfer(self, data):
        ui, uo = self.tt.ui_in, self.tt.uo_out
        rx = bytearray(len(data))
        ui[self.CS] = 0
        for i, byte in enumerate(data):
            r = 0
            for b in range(7, -1, -1):
                ui[self.MOSI] = (byte >> b) & 1
                ui[self.SCK] = 0
                ui[self.SCK] = 1
                r = (r << 1) | uo[self.MISO]
            rx[i] = r
        ui[self.SCK] = 0
        ui[self.CS] = 1
        return rx


class Pemu:
    """Register-level access to the chip over any transport with transfer()."""

    def __init__(self, link):
        self.link = link

    def write(self, addr, *words):
        buf = bytearray([addr & 0x7F])
        for w in words:
            buf += bytes(((w >> 8) & 0xFF, w & 0xFF))
        self.link.transfer(buf)

    def read(self, addr, n=1):
        rx = self.link.transfer(bytearray([0x80 | (addr & 0x7F)]) + bytearray(2 * n))
        words = [rx[1 + 2 * i] << 8 | rx[2 + 2 * i] for i in range(n)]
        return words[0] if n == 1 else words

    def check(self):
        ident = self.read(A_ID)
        if ident != CHIP_ID:
            raise RuntimeError("PEMU not found (ID read %#06x)" % ident)

    def load_demo(self, name):
        _desc, writes, enable, loopback, pin_set = DEMOS[name]
        self.write(A_CTRL, 0)
        for addr, value in writes:
            self.write(addr, value)
        if pin_set:
            self.write(A_PIN_SET, pin_set)
        # Enable + restart the engines in one write so they start together
        self.write(A_CTRL, enable | (4 if loopback else 0) | enable << 4)

    def status(self, engine):
        return self.read(engine_base(engine) + R_STATUS)

    def push(self, engine, word):
        """Push one word, waiting for TX FIFO space."""
        while (self.status(engine) >> 5) & 7 >= 4:
            pass
        self.write(engine_base(engine) + R_FIFO, word)

    def pop(self, engine, timeout=1000):
        """Pop one word from the RX FIFO, or None after `timeout` polls."""
        for _ in range(timeout):
            if (self.status(engine) >> 8) & 7:
                return self.read(engine_base(engine) + R_FIFO)
        return None

    # ----- demos -----

    def uart_print(self, text):
        self.load_demo("UART_TX")
        for ch in text:
            self.push(0, ord(ch))

    def ws2812(self, grb_bytes):
        self.load_demo("WS2812")
        for b in grb_bytes:
            self.push(0, b << 8)

    def i2c_transfer(self, words, n_results):
        """Raw i2c_master words (see sw/programs/i2c_master.pasm)."""
        for w in words:
            self.push(0, w)
        return [self.pop(0) for _ in range(n_results)]

    def i2c_scan(self):
        self.load_demo("I2C")
        found = []
        for addr in range(0x08, 0x78):
            # START, address + W, STOP: a 9-bit result with ACK in bit 0
            word = 1 << 15 | (~(addr << 1) & 0xFF) << 7 | 1 << 5
            r = self.i2c_transfer([word], 1)[0]
            if r is not None and r & 1 == 0:
                found.append(addr)
        return found


def selftest(link=None):
    """UART loopback between the two engines, entirely on-chip."""
    chip = Pemu(link or TTBoardSpi())
    chip.check()
    chip.load_demo("UART_LOOPBACK")
    sent = [0x55, 0xAA, 0x00, 0xFF]
    for b in sent:
        chip.push(0, b)
    got = [chip.pop(1) for _ in sent]
    ok = got == sent
    print("PEMU self-test", "PASSED" if ok else "FAILED: sent %r got %r" % (sent, got))
    return ok
