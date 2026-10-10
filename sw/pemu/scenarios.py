"""Protocol scenarios. Each runs on any bench (Python model or RTL).

A scenario is `async def name(bench)`. It loads real firmware from
sw/programs, wires up device models and asserts on what the devices saw.
"""

import os

from .asm import assemble_file
from .host import EngineConfig, ctrl
from .model import (A_CTRL, A_FLAGS, A_ID, A_PIN_CLR, A_PIN_OE, A_PIN_SET,
                    CHIP_ID, R_STATUS, R_X, engine_base)
from .periph import I2cTarget, PulseRecorder, SpiSlave, SquareWave, UartSink, UartSource

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
EDGE_PIN = 8       # ui_in[0]

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
        pcs.append((await b.status(0)) & 31)
    assert pcs[0] == 1                  # pull done
    assert pcs[1:9] == [2] * 8          # start bit: 1 + 7 delay ticks
    assert pcs[9] == 3                  # out pins
    assert pcs[10:12] == [2, 2]         # jmp x-- taken, then a delay tick
    assert (await b.read(engine_base(0) + R_X)) == 6


SCENARIOS = [check_id, uart_tx, uart_tx_timing, uart_rx, uart_loopback, spi, sd_cmd,
             i2c, i2c_timeout, ws2812, edge_stamp, single_step]
