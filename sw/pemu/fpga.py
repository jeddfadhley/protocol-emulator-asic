"""Talk to PEMU running on a Digilent Basys 3 FPGA board over USB.

The FPGA image (fpga/basys3) puts a UART-to-SPI bridge in front of the
unmodified chip design. The board's USB cable carries a 1,000,000 baud serial
link, and this module speaks the bridge's byte protocol:

    'S' len_hi len_lo <len bytes>   one SPI transaction; returns len bytes
    'R'                             reset PEMU; returns 'R'
    'I'                             returns b"PEMU"

Command line (run from the sw/ directory):

    python3 -m pemu.fpga ports              list serial ports
    python3 -m pemu.fpga check              read the chip ID
    python3 -m pemu.fpga selftest           on-chip UART loopback test
    python3 -m pemu.fpga uart "Hello"       115200 8N1 text out of Pmod JB7
    python3 -m pemu.fpga peek 0x65 [count]  read registers
    python3 -m pemu.fpga poke 0x49 0x1234   write registers
    python3 -m pemu.fpga eth-selftest       10BASE-T on-chip loopback test
    python3 -m pemu.fpga eth-send "text"    broadcast a UDP packet (port 7777)
    python3 -m pemu.fpga eth-serve          answer ping/ARP/UDP echo as 192.168.7.2
    python3 -m pemu.fpga usb-sniff          decode USB low-speed traffic (e.g. a keyboard)

Add --port /dev/tty.usbserial-XXXX1 if auto-detection picks the wrong port.
pyserial (pip3 install pyserial) is only needed to talk to real hardware.
"""

import argparse
import os
import sys
import time

_BOARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "board")
if _BOARD not in sys.path:
    sys.path.insert(0, _BOARD)

import pemu_board  # noqa: E402  (sw/board: the shared register-level driver)

BAUD = 1_000_000
MAX_CHUNK = 65535            # bytes per 'S' command
FTDI_VID, FT2232_PID = 0x0403, 0x6010


class LinkError(RuntimeError):
    pass


def _serial():
    try:
        import serial
        import serial.tools.list_ports  # noqa: F401
    except ImportError:
        raise LinkError("pyserial is not installed: run  pip3 install pyserial") from None
    return serial


def list_ports():
    """[(device, description, is_basys3_uart)] for every serial port."""
    _serial()
    from serial.tools import list_ports as lp
    out = []
    for p in sorted(lp.comports(), key=lambda p: p.device):
        likely = _looks_like_basys3(p)
        out.append((p.device, p.description or "", likely))
    return out


def _looks_like_basys3(p):
    # The Basys 3's FT2232 has two channels: A is JTAG (programming), B is
    # the UART. On macOS the UART is the device whose name ends in 1.
    if p.vid == FTDI_VID and p.pid == FT2232_PID:
        if p.location and p.location.endswith(".1"):
            return True
        return p.device.endswith("1")
    return False


def find_port():
    _serial()
    candidates = [dev for dev, _desc, likely in list_ports() if likely]
    if not candidates:
        # macOS fallback: the second FTDI channel
        import glob
        candidates = sorted(glob.glob("/dev/tty.usbserial-*1")) or sorted(glob.glob("/dev/ttyUSB1"))
    if not candidates:
        raise LinkError("no Basys 3 serial port found; is the board plugged in and switched on? "
                        "(try: python3 -m pemu.fpga ports, then --port)")
    return candidates[0]


class SerialLink:
    """PEMU's SPI host port, reached through the FPGA's UART bridge.

    transfer(data) runs one SPI transaction (CS low for the whole of data)
    and returns the bytes read back, so it plugs straight into
    pemu_board.Pemu. Pass `ser` to use an already open serial-like object
    (anything with write(), read(n) and reset_input_buffer()).
    """

    def __init__(self, port=None, baud=BAUD, ser=None, timeout=1.0):
        if ser is None:
            serial = _serial()
            port = port or find_port()
            ser = serial.Serial(port, baud, timeout=timeout)
        self.ser = ser
        self.port = port
        self._pending = 0    # reply bytes of posted writes not yet read
        self._sync()

    def _drain(self):
        """Collect the replies of posted writes (their content is unused)."""
        if self._pending:
            n, self._pending = self._pending, 0
            self._read(n)

    def _read(self, n):
        data = bytearray()
        while len(data) < n:
            chunk = self.ser.read(n - len(data))
            if not chunk:
                raise LinkError(f"timeout: expected {n} bytes from the FPGA, got {len(data)}")
            data += chunk
        return bytes(data)

    def _sync(self):
        """Make sure the bridge is idle and answering."""
        self.ser.reset_input_buffer()
        try:
            self.identify()
            return
        except LinkError:
            pass
        # The bridge may be half way through a transaction left by a crashed
        # program: it gives up after 0.5 s of silence.
        time.sleep(0.6)
        self.ser.reset_input_buffer()
        self.identify()

    def identify(self):
        """Check that the PEMU FPGA image is answering; returns b'PEMU'."""
        self._drain()
        self.ser.write(b"I")
        reply = self._read(4)
        if reply != b"PEMU":
            raise LinkError(f"unexpected reply {reply!r} to 'I': is the PEMU bitstream loaded?")
        return reply

    def reset(self):
        """Pulse PEMU's reset (all registers back to their reset values)."""
        self._drain()
        self.ser.write(b"R")
        if self._read(1) != b"R":
            raise LinkError("bad reply to reset")

    def transfer(self, data):
        data = bytes(data)
        if not 1 <= len(data) <= MAX_CHUNK:
            raise ValueError("an SPI transaction must be 1..65535 bytes")
        self.ser.write(b"S" + len(data).to_bytes(2, "big") + data)
        if not data[0] & 0x80:
            # A register write: its reply carries nothing, so don't wait for
            # it (each USB round trip costs milliseconds). The bridge keeps
            # the order, and the reply bytes are collected before the next
            # read.
            self._pending += len(data)
            if self._pending > 4096:
                self._drain()
            return bytearray(len(data))
        self._drain()
        return bytearray(self._read(len(data)))

    def close(self):
        self.ser.close()


# ---------------------------------------------------------------- CLI

def _int(s):
    return int(s, 0)


def cmd_ports(args, out):
    ports = list_ports()
    if not ports:
        print("no serial ports found", file=out)
    for dev, desc, likely in ports:
        print(f"{dev:40s} {desc}{'   <- Basys 3 UART' if likely else ''}", file=out)
    return 0


def cmd_check(args, out):
    link = args.open_link()
    chip = pemu_board.Pemu(link)
    chip.check()
    print(f"PEMU found: chip ID {chip.read(pemu_board.A_ID):#06x}", file=out)
    return 0


def cmd_selftest(args, out):
    link = args.open_link()
    link.reset()
    old, sys.stdout = sys.stdout, out
    try:
        ok = pemu_board.selftest(link)
    finally:
        sys.stdout = old
    return 0 if ok else 1


def cmd_uart(args, out):
    link = args.open_link()
    chip = pemu_board.Pemu(link)
    chip.check()
    text = args.text.encode().decode("unicode_escape")   # allow "\r\n"
    chip.uart_print(text)
    print(f"sent {len(text)} characters at 115200 8N1 on PEMU pin 12 "
          f"(uo_out[4], Pmod JB7)", file=out)
    return 0


def cmd_peek(args, out):
    chip = pemu_board.Pemu(args.open_link())
    words = chip.read(args.addr, args.count)
    if args.count == 1:
        words = [words]
    step = 0 if 0x40 <= args.addr < 0x60 and args.addr & 15 == 15 else 1
    for i, w in enumerate(words):
        print(f"{(args.addr + i * step) & 0x7F:#04x}: {w:#06x}", file=out)
    return 0


def cmd_poke(args, out):
    chip = pemu_board.Pemu(args.open_link())
    chip.write(args.addr, *args.values)
    print(f"wrote {len(args.values)} word(s) at {args.addr:#04x}", file=out)
    return 0


def cmd_eth_selftest(args, out):
    link = args.open_link()
    link.reset()
    old, sys.stdout = sys.stdout, out
    try:
        ok = pemu_board.eth_selftest(link)
    finally:
        sys.stdout = old
    return 0 if ok else 1


def cmd_eth_send(args, out):
    eth = pemu_board.eth
    chip = pemu_board.Pemu(args.open_link())
    chip.check()
    chip.eth_start()
    text = args.text.encode().decode("unicode_escape").encode()
    for i in range(args.count):
        chip.eth_send(eth.udp_frame(text, src_ip=_ip(args.ip)))
        if args.count > 1:
            time.sleep(args.interval)
    print(f"sent {args.count} UDP broadcast(s) to port 7777 from {args.ip} "
          f"({len(text)} bytes each)", file=out)
    return 0


def cmd_eth_serve(args, out):
    chip = pemu_board.Pemu(args.open_link())
    chip.check()
    chip.eth_start()
    print(f"PEMU is {args.ip} (MAC 02:50:45:4d:55:01). On the laptop: give the "
          f"Ethernet port a 192.168.7.x address, then  ping {args.ip}   (Ctrl-C to stop)",
          file=out)
    try:
        chip.eth_serve(ip=_ip(args.ip), frames=args.frames, polls=args.polls,
                       log=lambda m: print(m, file=out))
    except KeyboardInterrupt:
        pass
    return 0


def cmd_usb_sniff(args, out):
    usb = pemu_board.usb
    chip = pemu_board.Pemu(args.open_link())
    chip.check()
    chip.usb_sniff_start()
    print("Recording USB low speed: D+ on Pmod JC1, D- on JC2, GND to JC5. "
          "Ctrl-C to stop.", file=out)
    last_report, typed, shown = b"", "", 0
    polls = 0
    try:
        while args.polls is None or polls < args.polls:
            polls += 1
            for pkt, err in chip.usb_sniff_poll():
                if err:
                    print(f"  ! {err}", file=out)
                    continue
                pid = pkt[0] & 15
                is_data = pid in (usb.PID_DATA0, usb.PID_DATA1)
                if args.all or not (pid in (usb.PID_IN, usb.PID_NAK, usb.PID_ACK, 0x5)):
                    print(f"  {usb.describe(pkt)}", file=out)
                    shown += 1
                if is_data and len(pkt) == 11:          # 8-byte HID boot report
                    keys = usb.hid_keys(pkt[1:9], last_report)
                    last_report = pkt[1:9]
                    if keys:
                        typed += keys
                        print(f"  keys: {keys!r}   typed so far: {typed!r}", file=out)
            if args.packets is not None and shown >= args.packets:
                break
    except KeyboardInterrupt:
        pass
    return 0


def _ip(s):
    parts = tuple(int(p) for p in s.split("."))
    if len(parts) != 4 or not all(0 <= p < 256 for p in parts):
        raise argparse.ArgumentTypeError(f"bad IPv4 address {s!r}")
    return parts


def build_parser():
    p = argparse.ArgumentParser(prog="python3 -m pemu.fpga",
                                description="Control PEMU on a Basys 3 FPGA board.")
    p.add_argument("--port", help="serial port (default: auto-detect the Basys 3)")
    p.add_argument("--baud", type=int, default=BAUD)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ports", help="list serial ports").set_defaults(func=cmd_ports)
    sub.add_parser("check", help="read the chip ID").set_defaults(func=cmd_check)
    sub.add_parser("selftest", help="on-chip UART loopback self-test").set_defaults(func=cmd_selftest)
    u = sub.add_parser("uart", help="send text at 115200 8N1 out of Pmod JB7 (PEMU pin 12)")
    u.add_argument("text")
    u.set_defaults(func=cmd_uart)
    pk = sub.add_parser("peek", help="read register(s): peek ADDR [COUNT]")
    pk.add_argument("addr", type=_int)
    pk.add_argument("count", type=_int, nargs="?", default=1)
    pk.set_defaults(func=cmd_peek)
    po = sub.add_parser("poke", help="write register(s): poke ADDR VALUE [VALUE...]")
    po.add_argument("addr", type=_int)
    po.add_argument("values", type=_int, nargs="+")
    po.set_defaults(func=cmd_poke)
    sub.add_parser("eth-selftest", help="10BASE-T on-chip loopback self-test").set_defaults(
        func=cmd_eth_selftest)
    es = sub.add_parser("eth-send", help="broadcast UDP packets with this text (port 7777)")
    es.add_argument("text")
    es.add_argument("--count", type=int, default=1)
    es.add_argument("--interval", type=float, default=1.0)
    es.add_argument("--ip", default="192.168.7.2")
    es.set_defaults(func=cmd_eth_send)
    sv = sub.add_parser("eth-serve", help="answer ARP, ping and UDP echo (port 7)")
    sv.add_argument("--ip", default="192.168.7.2")
    sv.add_argument("--frames", type=int, default=None, help="stop after this many frames")
    sv.add_argument("--polls", type=int, default=None, help="stop after this many polls")
    sv.set_defaults(func=cmd_eth_serve)
    us = sub.add_parser("usb-sniff", help="record and decode USB low-speed traffic")
    us.add_argument("--all", action="store_true", help="also show IN, NAK, ACK and SOF")
    us.add_argument("--packets", type=int, default=None, help="stop after this many shown")
    us.add_argument("--polls", type=int, default=None, help="stop after this many polls")
    us.set_defaults(func=cmd_usb_sniff)
    return p


def main(argv=None, link=None, out=None):
    """Run the CLI. Tests pass `link` (anything with transfer/reset/identify)."""
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    args.open_link = (lambda: link) if link is not None else \
        (lambda: SerialLink(args.port, args.baud))
    try:
        return args.func(args, out)
    except (LinkError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
