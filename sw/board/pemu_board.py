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

from pemu_demos import CLK_HZ, DEMOS, ETH_RX_X

try:  # CPython (FPGA host): the package; MicroPython: copy sw/pemu/eth.py as pemu_eth.py
    from pemu import eth
except ImportError:
    try:
        import pemu_eth as eth
    except ImportError:
        eth = None
try:  # likewise sw/pemu/usb.py as pemu_usb.py
    from pemu import usb
except ImportError:
    try:
        import pemu_usb as usb
    except ImportError:
        usb = None

(A_CTRL, A_FLAGS, A_PIN_IN, A_PIN_OUT, A_PIN_OE, A_ID, A_PIN_SET, A_PIN_CLR,
 A_RAM_ADDR, A_RAM_DATA, A_DMA0, A_DMA1, A_FLAG_SET, A_FLAG_CLR, A_VERSION) = range(0x60, 0x6F)
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


    # ----- packet RAM and DMA -----

    def ram_write(self, addr, words):
        self.write(A_RAM_ADDR, addr)
        for i in range(0, len(words), 32):
            self.write(A_RAM_DATA, *words[i:i + 32])

    def ram_read(self, addr, n):
        self.write(A_RAM_ADDR, addr)
        out = []
        while len(out) < n:
            k = min(64, n - len(out))
            w = self.read(A_RAM_DATA, k)
            out += [w] if k == 1 else w
        return out

    def dma(self, channel, addr, count, rx=False):
        self.write(A_DMA0 + channel, (addr & 127) | (count & 255) << 7 | (0x8000 if rx else 0))

    # ----- 10BASE-T Ethernet (see sw/pemu/ethhost.py for the layout) -----

    ETH_RX_BASE, ETH_TX_BASE, ETH_WORDS = 0, 64, 64

    def eth_start(self, loopback=False):
        self.write(A_CTRL, 0)
        self.write(A_FLAG_CLR, 3)
        _desc, writes, enable, lb, _pins = DEMOS["ETH_LOOPBACK" if loopback else "ETHERNET"]
        for addr, value in writes:
            self.write(addr, value)
        self.dma(1, self.ETH_RX_BASE, self.ETH_WORDS, rx=True)
        self.write(A_CTRL, enable | (4 if lb else 0) | enable << 4)

    def eth_send(self, frame):
        """Send a frame (without FCS; it is padded and the FCS added)."""
        img = eth.tx_image(eth.with_fcs(frame))
        if len(img) > self.ETH_WORDS:
            raise ValueError("frame too long")
        while (self.read(A_DMA0) >> 7) & 255:
            pass  # previous frame still being fetched
        self.ram_write(self.ETH_TX_BASE, img)
        self.dma(0, self.ETH_TX_BASE, len(img))
        self.write(A_FLAG_SET, 1)

    def eth_recv(self, polls=1):
        """A received frame (with FCS) or None. Check eth.fcs_ok()."""
        for _ in range(polls):
            if self.read(A_FLAGS) & 2:
                break
        else:
            return None
        end = self.read(A_DMA1) & 127
        words = self.ram_read(self.ETH_RX_BASE, (end - self.ETH_RX_BASE) % 128)
        self.write(A_FLAG_CLR, 2)
        self.dma(1, self.ETH_RX_BASE, self.ETH_WORDS, rx=True)
        return eth.rx_decode(words, ETH_RX_X)[0]

    def eth_serve(self, mac=None, ip=None, frames=None, polls=None, log=print):
        """Answer ARP, ping and UDP echo (port 7) until `frames` good frames
        or `polls` polls (None = forever)."""
        mac = mac or eth.DEFAULT_MAC
        ip = ip or eth.DEFAULT_IP
        seen = n = 0
        while polls is None or n < polls:
            n += 1
            f = self.eth_recv()
            if f is None or not eth.fcs_ok(f):
                continue
            seen += 1
            reply = eth.respond(f[:-4], mac, ip)
            if log:
                log("rx %3d bytes %s -> %s" % (len(f), _ethdesc(f), "reply" if reply else "ignored"))
            if reply:
                self.eth_send(reply)
            if frames is not None and seen >= frames:
                break
        return seen


    # ----- USB low-speed sniffer -----

    def usb_sniff_start(self):
        """Start recording the USB pair on ui_in[1] (D+) and ui_in[2] (D-)."""
        _desc, writes, enable, lb, _pins = DEMOS["USB_SNIFF"]
        self.write(A_CTRL, 0)
        for addr, value in writes:
            self.write(addr, value)
        self.dma(1, 0, 255, rx=True)        # ring buffer over the whole RAM
        self.write(A_CTRL, enable | enable << 4)
        self._usb_rd = 0
        self._usb_states = []

    def usb_sniff_poll(self):
        """Packets captured since the last call: [(bytes or None, error)].
        Call often: the ring holds 128 words (about 1000 bit times)."""
        wr = self.read(A_DMA1) & 127
        n = (wr - self._usb_rd) % 128
        if n:
            start = self._usb_rd
            words = self.ram_read(start, min(n, 128 - start))
            if n > len(words):
                words += self.ram_read(0, n - len(words))
            self._usb_rd = wr
            self._usb_states += usb.capture_states(words)
        found, used = usb.split_stream(self._usb_states)
        self._usb_states = self._usb_states[used:]
        return found


def _ethdesc(f):
    etype = f[12] << 8 | f[13]
    if etype == 0x0806:
        return "ARP"
    if etype == 0x0800 and len(f) > 23:
        return {1: "ICMP", 6: "TCP", 17: "UDP"}.get(f[23], "IPv4")
    return "type %04x" % etype


def eth_selftest(link=None):
    """10BASE-T on-chip: engine 0 sends a UDP frame, engine 1 receives it."""
    chip = Pemu(link or TTBoardSpi())
    chip.check()
    chip.eth_start(loopback=True)
    frame = eth.udp_frame(b"PEMU Ethernet self-test")
    chip.eth_send(frame)
    got = chip.eth_recv(polls=2000)
    ok = got == eth.with_fcs(frame) and eth.fcs_ok(got)
    print("PEMU Ethernet self-test", "PASSED" if ok else "FAILED: got %r" % (got,))
    return ok


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
