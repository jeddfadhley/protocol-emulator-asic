"""The Basys 3 host software (pemu.fpga), run against the reference model.

FakeBridge stands in for the serial port: it parses the FPGA bridge's byte
protocol ('S'/'R'/'I') exactly as fpga/rtl/uart_spi_bridge.v does and runs
each SPI transaction on the Python model, so SerialLink's framing and every
CLI command are checked without hardware or pyserial.
"""

import io

import pytest

from pemu import fpga
from pemu import eth
from pemu.model import Core, is_port
from pemu.periph import EthMonitor, resolve


class ModelSpi:
    """One SPI transaction on the model (same decoding as test_board_driver)."""

    def __init__(self):
        self.core = Core()
        self.tx_trace = []           # PEMU pin 12 (UART TX demo) after each idle clock
        self.eth = EthMonitor(0, 1)  # a link partner on the TD+/TD- pair

    def idle(self, clocks):
        for _ in range(clocks):
            c = self.core
            wires, uio, _ = resolve(c.pin_out, c.pin_oe, 0, 0xFF)
            early, _, _ = resolve(c.pad_early(), c.pin_oe, 0, 0xFF)
            self.eth.step2(early, wires)
            c.clock(ui_in=0xFF, uio_in=uio)
            self.tx_trace.append(c.pin_out >> 12 & 1)

    def transaction(self, data):
        cmd, payload = data[0], data[1:]
        addr, rd = cmd & 0x7F, cmd & 0x80
        rx = bytearray(1)
        for i in range(0, len(payload) - 1, 2):
            if rd:
                w = self.core.clock(re=True, addr=addr)
                self.core.clock(rc=True, addr=addr)
                rx += bytes((w >> 8, w & 0xFF))
            else:
                self.core.clock(we=True, addr=addr, wdata=payload[i] << 8 | payload[i + 1])
            if not is_port(addr):
                addr = (addr + 1) & 0x7F
        rx += bytes(len(data) - len(rx))          # odd trailing byte reads 0
        self.idle(400)
        return rx


class FakeBridge:
    """A serial port with the PEMU FPGA image on the other end."""

    def __init__(self):
        self.spi = ModelSpi()
        self.inbuf = bytearray()     # PC -> FPGA, not yet consumed
        self.outbuf = bytearray()    # FPGA -> PC
        self.transactions = 0

    def write(self, data):
        self.inbuf += data
        self._run()
        return len(data)

    def _run(self):
        buf = self.inbuf
        while buf:
            c = buf[0]
            if c == ord("I"):
                del buf[0]
                self.outbuf += b"PEMU"
            elif c == ord("R"):
                del buf[0]
                self.spi = ModelSpi()
                self.outbuf += b"R"
            elif c == ord("S"):
                if len(buf) < 3:
                    return
                n = buf[1] << 8 | buf[2]
                if len(buf) < 3 + n:
                    return
                data = bytes(buf[3:3 + n])
                del buf[:3 + n]
                if n:
                    self.outbuf += self.spi.transaction(data)
                    self.transactions += 1
            else:
                del buf[0]           # ignored

    def read(self, n):
        out = bytes(self.outbuf[:n])
        del self.outbuf[:n]
        return out

    def reset_input_buffer(self):
        self.outbuf.clear()

    def close(self):
        pass


@pytest.fixture
def link():
    return fpga.SerialLink(ser=FakeBridge())


def run(argv, link):
    out = io.StringIO()
    rc = fpga.main(argv, link=link, out=out)
    return rc, out.getvalue()


def test_identify_and_reset(link):
    assert link.identify() == b"PEMU"
    link.transfer(bytes([0x49, 0x12, 0x34]))                 # X0 = 0x1234
    assert link.transfer(bytes([0xC9, 0, 0]))[1:] == b"\x12\x34"
    link.reset()
    assert link.transfer(bytes([0xC9, 0, 0]))[1:] == b"\x00\x00"


def test_transfer_framing(link):
    rx = link.transfer(bytes([0x80 | 0x65, 0, 0]))
    assert rx == bytearray(b"\x00\x50\x45")
    with pytest.raises(ValueError):
        link.transfer(b"")


def test_sync_recovers_from_stale_bytes():
    ser = FakeBridge()
    ser.outbuf += b"junk"                    # left over from an earlier session
    link = fpga.SerialLink(ser=ser)
    assert link.identify() == b"PEMU"


def test_cli_check(link):
    rc, out = run(["check"], link)
    assert rc == 0 and "0x5045" in out


def test_cli_selftest(link):
    rc, out = run(["selftest"], link)
    assert rc == 0 and "PASSED" in out


def test_cli_peek_poke(link):
    assert run(["poke", "0x49", "0xbeef"], link)[0] == 0
    rc, out = run(["peek", "0x49"], link)
    assert rc == 0 and "0x49: 0xbeef" in out
    assert run(["poke", "0", "1", "2", "3"], link)[0] == 0
    rc, out = run(["peek", "0", "3"], link)
    assert out.split() == ["0x00:", "0x0001", "0x01:", "0x0002", "0x02:", "0x0003"]


def test_cli_uart_runs_firmware(link):
    rc, out = run(["uart", "Hi\\r\\n"], link)
    assert rc == 0 and "JB7" in out
    spi = link.ser.spi
    spi.idle(4 * 10 * 432)                        # let the TX FIFO drain
    assert decode_uart(spi.tx_trace, 432) == b"Hi\r\n"


def decode_uart(trace, bit):
    """Decode 8N1 frames from a per-clock trace of a pin."""
    out, i = bytearray(), trace.index(1)      # skip until the line idles high
    while True:
        while i < len(trace) and trace[i] == 1:
            i += 1
        if i + 10 * bit > len(trace):
            return bytes(out)
        mid = i + bit // 2
        assert trace[mid] == 0
        out.append(sum(trace[mid + (k + 1) * bit] << k for k in range(8)))
        assert trace[mid + 9 * bit] == 1, "stop bit"
        i = mid + 9 * bit


def test_cli_reports_missing_chip():
    class Dead:
        def transfer(self, data):
            return bytearray(len(data))
    rc, _ = run(["check"], Dead())
    assert rc == 2


def test_pyserial_is_optional(monkeypatch):
    import builtins
    real = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "serial" or name.startswith("serial."):
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(fpga.LinkError, match="pyserial"):
        fpga.SerialLink()


def test_cli_eth_selftest(link):
    rc, out = run(["eth-selftest"], link)
    assert rc == 0 and "PASSED" in out, out


def test_cli_eth_send_puts_a_frame_on_the_wire(link):
    rc, out = run(["eth-send", "hello wireshark"], link)
    assert rc == 0, out
    mon = link.ser.spi.eth
    link.ser.spi.idle(8000)
    assert not mon.errors and len(mon.frames) == 1, mon.errors
    f = mon.frames[0]
    assert eth.fcs_ok(f) and eth.parse_udp(f[:-4])[-1] == b"hello wireshark"
