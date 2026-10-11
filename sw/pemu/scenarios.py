"""Protocol scenarios. Each runs on any bench (Python model or RTL).

A scenario is `async def name(bench)`. It loads real firmware from
sw/programs, wires up device models and asserts on what the devices saw.
"""

import os

from .asm import assemble_file
from .host import EngineConfig, ctrl
from .model import (A_CTRL, A_FLAGS, A_ID, A_PIN_CLR, A_PIN_OE, A_PIN_SET,
                    CHIP_ID, R_STATUS, R_X, engine_base)
from . import eth, usb
from .periph import (EthMonitor, EthSource, I2cTarget, PulseRecorder, SpiSlave, SquareWave,
                     UartSink, UartSource, UsbLsMonitor)

PROGRAMS = os.path.join(os.path.dirname(__file__), "..", "programs")


def program(name):
    return assemble_file(os.path.join(PROGRAMS, name + ".pasm"))


def crc7(data):
    crc = 0
    for byte in data:
        for i in range(7, -1, -1):
            fb = ((crc >> 6) & 1) ^ ((byte >> i) & 1)
            crc = (crc << 1) & 0x7F
            if fb:
                crc ^= 0x09
    return crc


# Pin assignments used by the scenarios (pin numbers, see docs/isa.md)
UART_TX_PIN = 12   # uo_out[4], the Tiny Tapeout UART TX convention
UART_RX_PIN = 11   # ui_in[3], the Tiny Tapeout UART RX convention
SCK, MOSI, MISO, CS = 0, 1, 2, 3
SDA, SCL = 4, 5
LED_PIN = 13       # uo_out[5]
USB_DP, USB_DM = 6, 7
EDGE_PIN = 8       # ui_in[0]
ETH_TD = 0         # uio[0] = TD+, uio[1] = TD- (PINCFG.DIFF)
ETH_RD = 8         # ui_in[0]: receive pair, after a comparator

UART_DIV = 3                    # 4 clocks per tick
UART_CPB = 8 * (UART_DIV + 1)   # 32 clocks per bit


async def check_id(b):
    assert await b.read(A_ID) == CHIP_ID


async def uart_tx(b):
    msg = b"Hi PEMU\r\n"
    sink = b.monitor(UartSink(UART_TX_PIN, UART_CPB))
    prog = program("uart_tx")
    cfg = EngineConfig(clkdiv=UART_DIV, out_base=UART_TX_PIN, set_base=UART_TX_PIN,
                       set_count=1, out_right=True, x=7)
    await b.setup(0, prog, 0, cfg)
    await b.write(A_PIN_SET, 1 << UART_TX_PIN)
    await b.start(1)
    await b.send(0, list(msg))
    await b.run_until(lambda: len(sink.bytes) >= len(msg), 200_000)
    assert bytes(sink.bytes) == msg, sink.bytes
    assert not sink.errors, sink.errors


async def uart_tx_timing(b):
    """Bit periods are exact: every run length is a multiple of the bit time."""
    prog = program("uart_tx")
    cfg = EngineConfig(clkdiv=UART_DIV, out_base=UART_TX_PIN, set_base=UART_TX_PIN,
                       set_count=1, out_right=True, x=7)
    await b.setup(0, prog, 0, cfg)
    await b.write(A_PIN_SET, 1 << UART_TX_PIN)
    rec = b.monitor(PulseRecorder(UART_TX_PIN))
    await b.start(1)
    await b.push(0, 0x55)  # 0 1 0 1 0 1 0 1 0 1: ten single-bit runs
    await b.run_until(lambda: len(rec.runs) >= 10, 50_000)
    lows = [n for level, n in rec.runs if level == 0]
    assert lows == [UART_CPB] * 5, rec.runs
    highs = [n for level, n in rec.runs[1:] if level == 1]
    assert highs[:4] == [UART_CPB] * 4, rec.runs


async def uart_rx(b):
    data = [0x00, 0xFF, 0xA5, 0x3C, 0x81]
    src = b.ui(UartSource(UART_CPB, gap=3, bad_stop={2}), UART_RX_PIN - 8)
    prog = program("uart_rx")
    cfg = EngineConfig(clkdiv=UART_DIV, in_base=UART_RX_PIN, jmp_pin=UART_RX_PIN,
                       in_right=True)
    await b.setup(0, prog, 0, cfg)
    await b.start(1)
    src.queue.extend(data)
    got = await b.drain(0, len(data) - 1)
    assert got == [d for i, d in enumerate(data) if i != 2], got
    assert (await b.read(A_FLAGS)) & 1, "framing error flag not set"


async def uart_loopback(b):
    """Full-duplex self test: engine 0 transmits, engine 1 receives, on-chip."""
    tx, rx = program("uart_tx"), program("uart_rx")
    await b.setup(0, tx, 0, EngineConfig(clkdiv=UART_DIV, out_base=UART_TX_PIN,
                                         set_base=UART_TX_PIN, set_count=1,
                                         out_right=True, x=7))
    await b.setup(1, rx, len(tx), EngineConfig(clkdiv=UART_DIV, in_base=UART_TX_PIN,
                                               jmp_pin=UART_TX_PIN, in_right=True))
    await b.write(A_PIN_SET, 1 << UART_TX_PIN)
    await b.start(3, loopback=True)
    msg = [0x12, 0xAB, 0xEF, 0x00]  # fits the RX FIFO, however slow the host
    await b.send(0, msg)
    assert await b.drain(1, len(msg)) == msg


def spi_cfg(**kw):
    return EngineConfig(clkdiv=3, out_base=MOSI, in_base=MISO, side_base=SCK,
                        autopull=True, autopush=True, push_thresh=8, pull_thresh=8, **kw)


async def spi(b):
    dev = b.gpio(SpiSlave(SCK, MOSI, MISO, CS, responses=[0xFF, 0xEF, 0x40, 0x18]))
    await b.setup(0, program("spi_master"), 0, spi_cfg())
    await b.write(A_PIN_SET, 1 << CS)
    await b.write(A_PIN_OE, (1 << SCK) | (1 << MOSI) | (1 << CS))
    await b.start(1)
    await b.write(A_PIN_CLR, 1 << CS)
    sent = [0x9F, 0x00, 0x00, 0x00]
    await b.send(0, [x << 8 for x in sent])
    got = await b.drain(0, 4)
    await b.write(A_PIN_SET, 1 << CS)
    assert dev.received == sent, dev.received
    assert got == [0xFF, 0xEF, 0x40, 0x18], [hex(g) for g in got]


async def sd_cmd(b):
    """SD card commands with CRC7 computed by the CRC unit."""
    cfg = EngineConfig(clkdiv=1, out_base=MOSI, set_base=MOSI, set_count=1,
                       side_base=SCK, crc_out=True, crc_poly=0x09 << 9)
    await b.setup(0, program("sd_cmd"), 0, cfg)
    await b.write(A_PIN_OE, (1 << SCK) | (1 << MOSI))
    dev = b.gpio(SpiSlave(SCK, MOSI, None))  # after SCK is driven low
    await b.start(1)
    cmd0 = [0x40, 0x00, 0x00, 0x00, 0x00]
    cmd8 = [0x48, 0x00, 0x00, 0x01, 0xAA]
    await b.send(0, [x << 8 for x in cmd0 + cmd8])
    await b.run_until(lambda: len(dev.received) >= 12, 100_000)
    assert dev.received[:6] == cmd0 + [0x95], [hex(x) for x in dev.received]
    assert dev.received[6:12] == cmd8 + [0x87], [hex(x) for x in dev.received]
    assert crc7(cmd0) << 1 | 1 == 0x95


def i2c_word(data=0xFF, start=False, stop=False, ack=False):
    """Host word for i2c_master. data=0xFF with ack=True/False for reads."""
    return int(start) << 15 | (~data & 0xFF) << 7 | int(ack) << 6 | int(stop) << 5


def i2c_cfg():
    return EngineConfig(clkdiv=1, out_base=SDA, set_base=SDA, set_count=1,
                        side_base=SCL, in_base=SDA, timeout=400)


async def i2c(b):
    dev = b.gpio(I2cTarget(SDA, SCL, address=0x50, stretch=7))
    await b.setup(0, program("i2c_master"), 0, i2c_cfg())
    await b.start(1)

    # Write three bytes at register 0x10
    words = [i2c_word(0xA0, start=True), i2c_word(0x10),
             i2c_word(0xDE), i2c_word(0xAD), i2c_word(0xBE, stop=True)]
    await b.send(0, words)
    res = await b.drain(0, 5)
    assert [r >> 1 for r in res] == [0xA0, 0x10, 0xDE, 0xAD, 0xBE]
    assert [r & 1 for r in res] == [0] * 5, "expected ACKs"
    assert dev.mem[0x10:0x13] == [0xDE, 0xAD, 0xBE]

    # Read them back: write pointer, repeated START, read 3 (ACK, ACK, NAK)
    words = [i2c_word(0xA0, start=True), i2c_word(0x10),
             i2c_word(0xA1, start=True), i2c_word(ack=True), i2c_word(ack=True),
             i2c_word(ack=False, stop=True)]
    await b.send(0, words)
    res = await b.drain(0, 6)
    assert [r >> 1 for r in res[3:]] == [0xDE, 0xAD, 0xBE], [hex(r) for r in res]
    assert [r & 1 for r in res] == [0, 0, 0, 0, 0, 1]
    await b.run(200)  # the STOP follows the last push
    assert dev.log.count("start") == 3 and dev.log.count("stop") == 2

    # A missing device NAKs its address
    await b.send(0, [i2c_word(0x42 << 1, start=True, stop=True)])
    res = await b.drain(0, 1)
    assert res[0] & 1 == 1, "expected NAK"
    assert (await b.read(A_FLAGS)) & 2 == 0


async def i2c_timeout(b):
    """A device holding SCL low trips the wait timeout; the engine recovers."""
    dev = b.gpio(I2cTarget(SDA, SCL, address=0x50))
    await b.setup(0, program("i2c_master"), 0, i2c_cfg())
    await b.start(1)
    dev.hang = True
    await b.send(0, [i2c_word(0xA0, start=True, stop=True)])
    await b.run(3000)
    assert (await b.read(A_FLAGS)) & 2, "bus error flag not set"
    st = await b.status(0)
    assert st & (1 << 12), "TIMED_OUT not set"
    await b.write(A_FLAGS, 0)
    await b.write(engine_base(0) + R_STATUS, 1 << 12)
    # Bus recovers once the device lets go
    dev.hang = False
    await b.send(0, [i2c_word(0xA0, start=True), i2c_word(0x00, stop=True)])
    res = await b.drain(0, 2)
    assert [r & 1 for r in res] == [0, 0]


async def ws2812(b):
    rec = b.monitor(PulseRecorder(LED_PIN))
    cfg = EngineConfig(clkdiv=0, side_base=LED_PIN, autopull=True, pull_thresh=8)
    await b.setup(0, program("ws2812"), 0, cfg)
    grb = [0xFF, 0x00, 0x5A]
    await b.send(0, [x << 8 for x in grb])  # queue first: no gaps between bytes
    await b.start(1)
    await b.run_until(lambda: len(rec.runs) >= 2 * 24, 100_000)
    await b.run(200)
    runs = rec.runs
    if runs and runs[0][0] == 0:
        runs = runs[1:]  # idle low before the first bit
    bits = []
    for i in range(24):
        (hl, hn), (ll, ln) = runs[2 * i], runs[2 * i + 1] if 2 * i + 1 < len(runs) else (0, 999)
        assert hl == 1 and ll == 0
        assert hn in (2, 7), f"bit {i}: high for {hn}"
        if i < 23:
            assert hn + ln == 10, f"bit {i}: period {hn + ln}"
        bits.append(int(hn == 7))
    value = int("".join(map(str, bits)), 2)
    assert value == (grb[0] << 16 | grb[1] << 8 | grb[2]), hex(value)


async def edge_stamp(b):
    segs = [(1, 37), (0, 53), (1, 11), (0, 99)]
    b.ui(SquareWave(segs), EDGE_PIN - 8)
    cfg = EngineConfig(clkdiv=0, in_base=EDGE_PIN, autopush=True)
    await b.setup(0, program("edge_stamp"), 0, cfg)
    await b.start(1)
    await b.run(800)  # the engine stalls once the RX FIFO holds 4 stamps
    stamps = await b.drain(0, 4)
    deltas = [(stamps[i + 1] - stamps[i]) & 0xFFFF for i in range(3)]
    # Successive edges alternate rising/falling; each interval is a segment.
    lengths = [n for _, n in segs]
    k = lengths.index(deltas[0])
    assert deltas == [lengths[(k + i) % 4] for i in range(3)], deltas


async def single_step(b):
    """Debug: a disabled engine runs one tick per STEP, and PC is observable."""
    prog = program("uart_tx")
    cfg = EngineConfig(out_base=UART_TX_PIN, set_base=UART_TX_PIN, set_count=1,
                       out_right=True, x=7)
    await b.setup(0, prog, 0, cfg)
    await b.write(A_CTRL, ctrl(restart=1))
    await b.push(0, 0x01)
    pcs = []
    for _ in range(12):
        await b.write(A_CTRL, ctrl(step=1))
        await b.run(1)                  # the pulse acts on the next clock
        pcs.append((await b.status(0)) & 31)
    assert pcs[0] == 1                  # pull done
    assert pcs[1:9] == [2] * 8          # start bit: 1 + 7 delay ticks
    assert pcs[9] == 3                  # out pins
    assert pcs[10:12] == [2, 2]         # jmp x-- taken, then a delay tick
    assert (await b.read(engine_base(0) + R_X)) == 6


async def usb_ls_tx(b):
    """USB low-speed packets at exactly 1.5 Mbit/s (48 MHz clock, 32 clocks/bit)."""
    mon = b.monitor(UsbLsMonitor(USB_DP, USB_DM, 32))
    cfg = EngineConfig(clkdiv=7, out_base=USB_DP, set_base=USB_DP, out_count=2,
                       set_count=2, autopull=True, y=3)
    await b.setup(0, program("usb_ls_tx"), 0, cfg)
    get_descriptor = bytes([0x80, 0x06, 0x00, 0x01, 0x00, 0x00, 0x40, 0x00])
    packets = [usb.token(usb.PID_SETUP, 0, 0),
               usb.data_packet(usb.PID_DATA0, get_descriptor),
               usb.handshake(usb.PID_ACK),
               usb.data_packet(usb.PID_DATA1, b"\xff" * 4)]  # needs bit stuffing
    words = []
    for p in packets:
        words += usb.to_words(usb.encode(p))
    for w in words[:3]:
        await b.push(0, w)
    await b.start(1)
    await b.stream(0, words[3:], 8 * 32 + 8)  # slightly slower than the line rate
    await b.run_until(lambda: len(mon.packets) + len(mon.errors) >= len(packets), 50_000)
    assert not mon.errors, mon.errors
    assert mon.packets == packets, [p.hex() for p in mon.packets]
    assert not (await b.status(0)) & (1 << 14), "TX FIFO overflowed"


# ----- 10BASE-T Ethernet (50 MHz clock) -----

ETH_TX_ORIGIN, ETH_RX_ORIGIN = 0, 16


def eth_tx_cfg(timeout=0xFFFF, nlp_div=11):
    """eth_tx config. Link pulses are (nlp_div + 1) * (timeout + 2) + 8 ticks
    apart: 12 x 65537 + 8 ticks = 15.7 ms at 50 MHz with the defaults."""
    return EngineConfig(out_base=ETH_TD, set_base=ETH_TD, side_base=ETH_TD, out_count=1,
                        set_count=1, diff=True, out_right=True, autopull=True,
                        timeout=timeout, crc_init=nlp_div, y=nlp_div)


def eth_rx_cfg(rd_pin, max_bytes):
    _, x = eth.rx_words(max_bytes)
    return EngineConfig(in_base=rd_pin, jmp_pin=rd_pin, in_right=True, autopush=True,
                        timeout=4, x=x, y=x)


async def eth_send(b, frame, origin=0):
    """Queue a frame (without FCS) for eth_tx: RAM image, DMA, flag 0."""
    img = eth.tx_image(eth.with_fcs(frame))
    await b.ram_write(origin, img)
    await b.dma(0, origin, len(img))
    await b.flag_set(1)
    return img


async def eth_wait_frame(b, limit=200_000):
    """Wait for eth_rx to finish a frame (flag 1), clear the flag and return
    the RX DMA address after it."""
    for _ in range(limit):
        if (await b.flags()) & 2:
            break
    else:
        raise TimeoutError("no frame received")
    await b.flag_clr(2)
    return (await b.dma_state(1))[0]


def eth_hello(n=0):
    return eth.udp_frame(b"Hello from PEMU over 10BASE-T! #%d" % n)


async def eth_loopback(b):
    """Engine 0 sends a frame, engine 1 receives it, on-chip."""
    frame = eth.with_fcs(eth_hello())
    _, x = eth.rx_words(len(frame))
    await b.setup(0, program("eth_tx"), ETH_TX_ORIGIN, eth_tx_cfg(timeout=300, nlp_div=1))
    await b.setup(1, program("eth_rx"), ETH_RX_ORIGIN, eth_rx_cfg(ETH_TD, len(frame)))
    await b.dma(1, 64, 64, rx=True)
    await b.start(3, loopback=True)
    await eth_send(b, eth_hello())
    end = await eth_wait_frame(b)
    data, nbits = eth.rx_decode(await b.ram_read(64, end - 64), x)
    assert nbits == 8 * len(frame), nbits
    assert data == frame, (data.hex(), frame.hex())
    assert eth.fcs_ok(data)


async def eth_tx(b):
    """A link partner checks link pulses and every bit's timing."""
    mon = b.monitor(EthMonitor(ETH_TD, ETH_TD + 1))
    await b.setup(0, program("eth_tx"), ETH_TX_ORIGIN, eth_tx_cfg(timeout=400, nlp_div=2))
    await b.start(1)
    await b.run_until(lambda: len(mon.nlps) >= 2, 20_000)
    frames = [eth_hello(1), eth.udp_frame(bytes(range(100)))]
    for f in frames:
        await eth_send(b, f)
        n = len(mon.frames)
        await b.run_until(lambda: len(mon.frames) > n or mon.errors, 60_000)
    await b.run_until(lambda: len(mon.nlps) >= 4, 20_000)
    assert not mon.errors, mon.errors
    assert mon.frames == [eth.with_fcs(f) for f in frames]
    quiet = [(t0, t1) for t0, t1 in zip(mon.nlps, mon.nlps[1:])
             if not any(t0 < f < t1 for f in mon.frame_times)]
    assert len(quiet) >= 2 and {t1 - t0 for t0, t1 in quiet} == {3 * 402 + 8}, mon.nlps


async def _eth_rx(b, **line):
    frames = [eth.with_fcs(eth_hello(2)), eth.with_fcs(eth.udp_frame(b"short")),
              eth.with_fcs(eth.udp_frame(bytes(range(256))[:90]))]
    max_bytes = 100
    _, x = eth.rx_words(max_bytes)
    src = EthSource(frames, gap_ns=120_000, **line)
    await b.setup(1, program("eth_rx"), ETH_RX_ORIGIN, eth_rx_cfg(ETH_RD, max_bytes))
    await b.dma(1, 0, 128, rx=True)
    await b.start(2)
    b.ui(src, ETH_RD - 8)
    ends = [await eth_wait_frame(b) for _ in frames]
    words = await b.ram_read(0, ends[-1])
    start = 0
    for f, end in zip(frames, ends):
        data, nbits = eth.rx_decode(words[start:end], x)
        if len(f) <= max_bytes:
            assert data == f, (data.hex(), f.hex())
            assert eth.fcs_ok(data)
        else:  # longer than the buffer: the first max_bytes, the rest skipped
            assert data == f[:max_bytes] and nbits == 8 * max_bytes
        start = end


async def eth_rx(b):
    await _eth_rx(b, link_pulses=True)


async def eth_rx_drift(b):
    """Receive with the sender's clock 100 ppm fast, phase offset and jitter."""
    await _eth_rx(b, phase_ns=13.0, ppm=100.0, jitter_ns=3.0)


async def eth_ping(b):
    """A laptop (device models) sends ARP and ping; the chip answers both."""
    from .ethhost import Station
    laptop_mac, laptop_ip = b"\xaa\xbb\xcc\x00\x00\x01", (192, 168, 7, 1)
    requests = [eth.arp_request(laptop_mac, laptop_ip, eth.DEFAULT_IP),
                eth.icmp_echo_request(laptop_mac, eth.DEFAULT_MAC, laptop_ip, eth.DEFAULT_IP,
                                      0x1234, 7, bytes(range(56)))]
    mon = b.monitor(EthMonitor(ETH_TD, ETH_TD + 1))
    st = Station(b, tx_timeout=4000, nlp_div=1)
    await st.start()
    b.ui(EthSource([eth.with_fcs(r) for r in requests], gap_ns=600_000), ETH_RD - 8)
    await st.serve(frames=2, polls=100_000)
    await b.run_until(lambda: len(mon.frames) >= 2 or mon.errors, 100_000)
    assert not mon.errors, mon.errors
    arp, pong = (f[:-4] for f in mon.frames)
    assert all(eth.fcs_ok(f) for f in mon.frames)
    assert arp == eth.with_fcs(eth.respond(requests[0]))[:-4]  # padded to 60 bytes
    assert eth.parse_icmp_echo(pong) == (0, 0x1234, 7, bytes(range(56)))


SCENARIOS = [check_id, uart_tx, uart_tx_timing, uart_rx, uart_loopback, spi, sd_cmd,
             i2c, i2c_timeout, ws2812, edge_stamp, single_step, usb_ls_tx,
             eth_loopback, eth_tx, eth_rx, eth_rx_drift, eth_ping]
