"""Scenarios for the protocols added after the hardware was frozen
(docs/new-protocols.md). They run on the Python model and on the RTL, like
scenarios.py, and are appended to its SCENARIOS list.
"""

import os

from .asm import assemble_file
from .host import EngineConfig
from .model import A_PIN_OE, A_PIN_SET
from .periph_ext import (CanBus, CanNode, Dht22, DmxController, NecRemote, OneWireDevice,
                         Ps2Keyboard, QuadratureEncoder, SpiController, SwdTarget, crc8_maxim,
                         odd_parity)
from .swdhost import SwdError, SwdHost
from . import can

PROGRAMS = os.path.join(os.path.dirname(__file__), "..", "programs")


def program(name):
    return assemble_file(os.path.join(PROGRAMS, name + ".pasm"))


# ----- 1-Wire -----

OW_PIN = 0          # uio[0], with a pull-up
OW_DIV = 3          # 1 tick = 1 us of protocol time = 4 clocks (real silicon: 49)
OW_US = OW_DIV + 1


def ow_word(byte=0xFF, reset=False):
    """onewire TX word: [0] RESET first, [8:1] ~data. byte=0xFF reads."""
    return int(reset) | (~byte & 0xFF) << 1


def ow_cfg():
    return EngineConfig(clkdiv=OW_DIV, out_base=OW_PIN, set_base=OW_PIN, in_base=OW_PIN,
                        set_count=1, out_right=True, in_right=True)


async def ow_transaction(b, writes, nread=0):
    """RESET, write bytes, read nread bytes. Returns (presence, bytes read)."""
    words = [ow_word(writes[0], reset=True)] + [ow_word(x) for x in writes[1:]]
    words += [ow_word()] * nread
    res = []
    for i in range(0, len(words), 4):        # at most 4 in flight: the RX FIFO never fills
        await b.send(0, words[i:i + 4])
        res += await b.drain(0, len(words[i:i + 4]))
    present = not (res[0] >> 7) & 1
    return present, bytes(r >> 8 for r in res[len(writes):])


async def onewire(b):
    """Read a DS18B20's ROM code, convert, and read the temperature."""
    dev = b.gpio(OneWireDevice(OW_PIN, OW_US, temp_c=25.0625))
    await b.setup(0, program("onewire"), 0, ow_cfg())
    await b.start(1)

    present, rom = await ow_transaction(b, [0x33], 8)          # READ ROM
    assert present
    assert rom == dev.rom and crc8_maxim(rom) == 0, rom.hex()

    present, _ = await ow_transaction(b, [0xCC, 0x44])         # SKIP ROM, CONVERT T
    assert present
    present, sp = await ow_transaction(b, [0x55] + list(rom) + [0xBE], 9)  # MATCH ROM, READ SCRATCHPAD
    assert present
    assert crc8_maxim(sp) == 0, sp.hex()
    raw = int.from_bytes(sp[0:2], "little", signed=True)
    assert raw / 16 == 25.0625, raw

    # A ROM code that matches no device: nobody answers the read
    other = bytes([0x28] + [0] * 6)
    other += bytes([crc8_maxim(other)])
    _, sp = await ow_transaction(b, [0x55] + list(other) + [0xBE], 2)
    assert sp == b"\xff\xff"
    assert dev.log.count("reset") == 4
    assert not dev.errors, dev.errors


async def onewire_absent(b):
    """An empty bus: the presence bit says so."""
    await b.setup(0, program("onewire"), 0, ow_cfg())
    await b.start(1)
    present, _ = await ow_transaction(b, [0xCC])
    assert not present


# ----- NEC infrared -----

IR_PIN = 10         # ui_in[2]
IR_DIV = 3          # 4 clocks per tick, 32 clocks per NEC unit (real silicon: 3514)
IR_UNIT = 8 * (IR_DIV + 1)


async def _nec(b, scale):
    events = [("frame", 0x04, 0x08), ("repeat",), ("noise", 3 * IR_UNIT),
              ("partial", 0x10, 0x20, 11),       # cut short: the trap drops it
              ("frame", 0xEF, 0x5A), ("repeat",), ("repeat",)]
    # Idle for 300 units first: over SPI the setup below takes ~6,500 clocks
    remote = b.ui(NecRemote(IR_UNIT, events, scale=scale, lead=300), IR_PIN - 8)
    cfg = EngineConfig(clkdiv=IR_DIV, in_base=IR_PIN, jmp_pin=IR_PIN, in_right=True,
                       autopush=True, timeout=80)
    await b.setup(0, program("nec_ir"), 0, cfg)
    await b.start(1)
    got = []
    while len(got) < 10:
        got += await b.drain(0, 2)
    pairs = [(w0, w1) for w0, w1 in zip(got[::2], got[1::2])]
    decoded = [("repeat",) if w1 == 0 else ("frame", w0 & 0xFF, w1 & 0xFF) for w0, w1 in pairs]
    assert decoded == [("frame", 0x04, 0x08), ("repeat",), ("frame", 0xEF, 0x5A),
                       ("repeat",), ("repeat",)], decoded
    for w0, w1 in pairs:
        assert w1 == 0 or (w0 >> 8 == ~w0 & 0xFF and w1 >> 8 == ~w1 & 0xFF)
    assert (await b.status(0)) & (1 << 12), "the partial frame should have timed out"
    await b.run_until(lambda: remote.done, 100_000)


async def nec_ir(b):
    """Decode NEC remote frames and repeat codes; drop noise and a cut-off frame."""
    await _nec(b, 1.0)


async def nec_ir_tolerance(b):
    """The same with the remote's clock 8% slow, then 8% fast."""
    await _nec(b, 1.08)


async def nec_ir_fast(b):
    await _nec(b, 0.92)


async def nec_ir_midframe(b):
    """The receiver starts in the middle of a frame: it must ignore the rest
    of that frame (its data bursts are not leaders) and decode the next."""
    events = [("frame", 0x11, 0x22), ("frame", 0x04, 0x08)]
    cfg = EngineConfig(clkdiv=IR_DIV, in_base=IR_PIN, jmp_pin=IR_PIN, in_right=True,
                       autopush=True, timeout=80)
    await b.setup(0, program("nec_ir"), 0, cfg)
    remote = b.ui(NecRemote(IR_UNIT, events, lead=0), IR_PIN - 8)
    await b.run(30 * IR_UNIT)                            # well into the first frame's data
    await b.start(1)                                     # one register write: quick on any bench
    await b.run_until(lambda: remote.done, 200_000)
    words = await b.drain(0, 2)
    assert words == [0x04 | 0xFB << 8, 0x08 | 0xF7 << 8], [hex(w) for w in words]
    assert ((await b.status(0)) >> 8) & 7 == 0, "nothing else should have been decoded"


# ----- PS/2 -----

PS2_DATA, PS2_CLK = 2, 3    # uio[2], uio[3], with pull-ups
PS2_DIV = 1                 # 1 tick = 1 us of protocol time = 2 clocks (real silicon: 49)
PS2_US = PS2_DIV + 1


def ps2_tx_word(byte):
    """ps2_host word for a byte sent to the device."""
    return (~(byte | odd_parity(byte) << 8 | 1 << 9) & 0x3FF) | 0x8000


def ps2_decode(word):
    """A received frame -> byte (checks start, parity and stop), or 'ACK'/'NAK'."""
    if word & 0x7FE0 == 0:
        return "NAK" if word & 0x8000 else "ACK"
    assert (word >> 5) & 1 == 0 and word >> 15 == 1, hex(word)
    byte = (word >> 6) & 0xFF
    assert (word >> 14) & 1 == odd_parity(byte), hex(word)
    return byte


async def ps2(b):
    """Read scan codes from a keyboard, then set its LEDs (host to device)."""
    cfg = EngineConfig(clkdiv=PS2_DIV, in_base=PS2_DATA, out_base=PS2_DATA,
                       set_base=PS2_DATA, set_count=2, jmp_pin=PS2_CLK,
                       out_right=True, in_right=True, timeout=15000)
    await b.setup(0, program("ps2_host"), 0, cfg)
    await b.start(1)
    kb = b.gpio(Ps2Keyboard(PS2_DATA, PS2_CLK, half=40 * PS2_US, us=PS2_US,
                            keys=[0x1C, 0xF0, 0x1C]))       # 'A' pressed, released
    got = [ps2_decode(w) for w in await b.drain(0, 3)]
    assert got == [0x1C, 0xF0, 0x1C], got

    await b.push(0, ps2_tx_word(0xED))                      # set LEDs ...
    assert [ps2_decode(w) for w in await b.drain(0, 2)] == ["ACK", 0xFA]
    await b.push(0, ps2_tx_word(0x07))                      # ... all three on
    assert [ps2_decode(w) for w in await b.drain(0, 2)] == ["ACK", 0xFA]
    assert kb.leds == 0x07
    await b.push(0, ps2_tx_word(0xF2))                      # identify
    assert [ps2_decode(w) for w in await b.drain(0, 4)] == ["ACK", 0xFA, 0xAB, 0x83]
    assert kb.received == [0xED, 0x07, 0xF2]
    assert not kb.errors, kb.errors


async def ps2_busy(b):
    """The host queues a command while the keyboard is mid-frame. The
    firmware only starts a send between frames, so it finishes receiving
    first: nothing is lost and the keyboard is never interrupted."""
    cfg = EngineConfig(clkdiv=PS2_DIV, in_base=PS2_DATA, out_base=PS2_DATA,
                       set_base=PS2_DATA, set_count=2, jmp_pin=PS2_CLK,
                       out_right=True, in_right=True, timeout=1000)
    await b.setup(0, program("ps2_host"), 0, cfg)
    await b.start(1)
    kb = b.gpio(Ps2Keyboard(PS2_DATA, PS2_CLK, half=40 * PS2_US, us=PS2_US, keys=[0x5A]))
    await b.run(50 * PS2_US + 4 * 80 * PS2_US)               # the keyboard is on its 4th bit
    await b.push(0, ps2_tx_word(0xFF))                       # reset
    got = [ps2_decode(w) for w in await b.drain(0, 4)]
    assert got == [0x5A, "ACK", 0xFA, 0xAA], got
    assert kb.aborts == 0 and kb.received == [0xFF]
    assert not (await b.status(0)) & (1 << 12), "nothing should have timed out"
    assert not kb.errors, kb.errors


async def ps2_glitch(b):
    """A frame that stops halfway (keyboard unplugged mid-byte) times out and
    is dropped; the next frame is received normally."""
    cfg = EngineConfig(clkdiv=PS2_DIV, in_base=PS2_DATA, out_base=PS2_DATA,
                       set_base=PS2_DATA, set_count=2, jmp_pin=PS2_CLK,
                       out_right=True, in_right=True, timeout=1000)
    await b.setup(0, program("ps2_host"), 0, cfg)
    await b.start(1)
    kb = b.gpio(Ps2Keyboard(PS2_DATA, PS2_CLK, half=40 * PS2_US, us=PS2_US, keys=[0x1C]))
    await b.run(50 * PS2_US + 5 * 80 * PS2_US)
    kb.gen = iter(lambda: None, 1)                           # the keyboard goes silent
    kb.pull = 0
    await b.run(3000 * PS2_US)
    assert (await b.status(0)) & (1 << 12), "the half frame should have timed out"
    assert ((await b.status(0)) >> 8) & 7 == 0, "nothing should have been pushed"
    kb.queue = [0x32]                                        # plugged back in
    kb.gen = kb._run()
    assert [ps2_decode(w) for w in await b.drain(0, 1)] == [0x32]


# ----- ARM Serial Wire Debug -----

SWDIO = 1           # uio[1]
SWCLK = 8           # uo_out[0]


async def swd_setup(b):
    target = b.gpio(SwdTarget(SWCLK, SWDIO))
    cfg = EngineConfig(clkdiv=0, out_base=SWDIO, set_base=SWDIO, in_base=SWDIO, set_count=1,
                       side_base=SWCLK, out_right=True, in_right=True,
                       autopull=True, autopush=True)
    await b.setup(0, program("swd"), 0, cfg)
    await b.start(1)
    return target, SwdHost(b)


async def swd(b):
    """Connect to an ARM debug port, power it up and use its memory port."""
    target, host = await swd_setup(b)
    await host.line_reset()
    assert await host.dp_read(0x0) == SwdTarget.IDCODE
    await host.dp_write(0x4, 0x50000000)             # CSYSPWRUPREQ | CDBGPWRUPREQ
    assert await host.dp_read(0x4) == 0xF0000000     # ... and their ACKs
    await host.dp_write(0x8, 0xF0)                   # SELECT AP bank 0xF
    await host.read(1, 0xC)                          # IDR (posted)
    assert await host.dp_read(0xC) == SwdTarget.AP_IDR
    await host.dp_write(0x8, 0x00)
    await host.mem_write(0x20000000, 0xCAFEF00D)
    await host.mem_write(0x20000004, 0x12345678)
    assert await host.mem_read(0x20000000) == 0xCAFEF00D
    assert await host.mem_read(0x20000004) == 0x12345678
    assert target.mem == {0x20000000: 0xCAFEF00D, 0x20000004: 0x12345678}
    assert not target.errors, target.errors


async def swd_errors(b):
    """The rules a debugger must follow: IDCODE first after a reset, and a
    write with bad parity makes later requests FAULT until ABORT clears it."""
    target, host = await swd_setup(b)
    await host.line_reset()
    try:
        await host.dp_read(0x4)                      # not IDCODE: no response
        raise AssertionError("expected no response")
    except SwdError as exc:
        assert "no response" in str(exc), exc
    await host.line_reset()
    assert await host.dp_read(0x0) == SwdTarget.IDCODE
    await host.write(0, 0x8, 0x00, bad_parity=True)
    try:
        await host.dp_read(0x4)
        raise AssertionError("expected FAULT")
    except SwdError as exc:
        assert str(exc) == "FAULT", exc
    await host.dp_write(0x0, 1 << 3)                 # ABORT.WDERRCLR (ABORT is never refused)
    assert await host.dp_read(0x4) & (1 << 7) == 0


# ----- CAN -----

CAN_TXD = 4         # uio[4], to the transceiver's TXD. Not a uo_out pin: those
                    # reset to 0, which would hold the bus dominant until configured.
CAN_RXD = 11        # ui_in[3], from its RXD
CAN_DIV = 1         # 1 Mbit/s at 50 MHz
CAN_BIT = 25 * (CAN_DIV + 1)
CAN_LOST = 1 << 2   # event flag 2


def can_cfg():
    return EngineConfig(clkdiv=CAN_DIV, out_base=CAN_TXD, set_base=CAN_TXD, set_count=1,
                        in_base=CAN_RXD, jmp_pin=CAN_RXD, out_right=True, in_right=True,
                        autopull=True, timeout=275)


async def can_setup(b, nodes):
    bus = b.ui(CanBus(CAN_TXD, nodes), CAN_RXD - 8)
    await b.write(A_PIN_SET, 1 << CAN_TXD)               # TXD idles recessive ...
    await b.write(A_PIN_OE, 1 << CAN_TXD)                # ... before it is driven
    await b.setup(0, program("can_tx"), 0, can_cfg())
    await b.start(1)
    return bus


async def can_send(b, ident, data=b"", addr=0):
    """Put a frame in the packet RAM and let DMA feed the engine. Returns the
    pushed word (the last 16 bus levels)."""
    words = can.tx_words(ident, data)
    await b.ram_write(addr, words)
    await b.dma(0, addr, len(words))
    return (await b.drain(0, 1))[0]


async def can_tx(b):
    """Send frames at 1 Mbit/s; another node checks them and acknowledges."""
    rx = CanNode(CAN_BIT, name="rx")
    await can_setup(b, [rx])
    frames = [(0x123, b"PEMU"), (0x000, b"\x00\x00\xff\xff\x00"), (0x7FF, b"")]
    for ident, data in frames:
        tail = await can_send(b, ident, data)
        assert (tail >> 7) & 1 == 0, f"no ACK: {tail:016b}"   # the ACK slot was dominant
        assert tail >> 8 == 0xFF                             # delimiter and EOF recessive
        await b.run(12 * CAN_BIT)                            # intermission
    assert rx.frames == frames, rx.frames
    assert not (await b.flags()) & CAN_LOST
    assert not rx.errors, rx.errors


async def can_arbitration(b):
    """Another node starts a higher-priority frame at the same moment. The
    chip loses arbitration, stops driving, and the winner's frame arrives
    intact. Then the chip retries and gets through."""
    rival = CanNode(CAN_BIT, tx_queue=[(0x100, b"\x55")], name="rival", join_only=True)
    listener = CanNode(CAN_BIT, name="listener")
    await can_setup(b, [rival, listener])
    await can_send(b, 0x123, b"PEMU")                    # 0x100 < 0x123 wins
    assert (await b.flags()) & CAN_LOST, "the chip should have lost arbitration"
    assert rival.lost == 0 and rival.acked == [True]
    assert listener.frames == [(0x100, b"\x55")], listener.frames
    await b.flag_clr(CAN_LOST)
    await b.run(12 * CAN_BIT)
    tail = await can_send(b, 0x123, b"PEMU")             # retry on a quiet bus
    assert (tail >> 7) & 1 == 0, f"no ACK: {tail:016b}"
    assert not (await b.flags()) & CAN_LOST
    assert listener.frames == [(0x100, b"\x55"), (0x123, b"PEMU")], listener.frames
    assert not rival.errors and not listener.errors, rival.errors + listener.errors


async def can_busy(b):
    """The bus is busy when the chip gets a frame: it waits for the other
    node's frame and 11 idle bits, then sends. Neither frame is damaged."""
    other = CanNode(CAN_BIT, tx_queue=[(0x050, bytes(range(8)))], name="other")  # sends at once
    listener = CanNode(CAN_BIT, name="listener")
    await can_setup(b, [other, listener])
    tail = await can_send(b, 0x123, b"PEMU")
    assert (tail >> 7) & 1 == 0, f"no ACK: {tail:016b}"
    assert not (await b.flags()) & CAN_LOST
    assert listener.frames == [(0x050, bytes(range(8))), (0x123, b"PEMU")], listener.frames
    assert other.acked == [True]
    assert not other.errors and not listener.errors, other.errors + listener.errors


# ----- CAN receiver (v4) -----

CAN_RX_BIT = 50     # can_rx: CLKDIV = 0, 50 ticks per bit: 1 Mbit/s


def can_rx_cfg():
    return EngineConfig(clkdiv=0, set_base=CAN_TXD, set_count=1, in_base=CAN_RXD,
                        jmp_pin=CAN_RXD, push_thresh=14, pull_thresh=4, crc_in=True,
                        crc_poly=can.CRC15_POLY << 1, timeout=19)


async def can_rx_run(b, node, give_up_after_error=False):
    """Start can_rx (its words go to the packet RAM by DMA), connect `node`
    to the bus, let it send everything, and return the frames the chip
    reported. A CAN sender repeats a frame that failed; with
    give_up_after_error the node drops it after the first try instead."""
    await b.write(A_PIN_SET, 1 << CAN_TXD)               # TXD idles recessive ...
    await b.write(A_PIN_OE, 1 << CAN_TXD)                # ... before it is driven
    await b.setup(0, program("can_rx"), 0, can_rx_cfg())
    await b.dma(0, 0, 127, rx=True)
    await b.start(1)
    node.idle_bits = 0                                   # it must see 11 idle bits first
    b.ui(CanBus(CAN_TXD, [node]), CAN_RXD - 8)
    sent = len(node.queue)
    if give_up_after_error:
        await b.run_until(lambda: node.errors, limit=200 * CAN_RX_BIT)
        node.queue.pop(0)
    await b.run_until(lambda: not node.queue and node.phase is None, limit=sent * 200 * CAN_RX_BIT)
    await b.run(4 * CAN_RX_BIT)
    addr, _ = await b.dma_state(0)
    return can.rx_split(await b.ram_read(0, addr))


async def can_rx(b):
    """v4: the chip receives CAN frames and acknowledges them itself. The
    sender sees every frame acknowledged; the host gets every bit."""
    frames = [(0x123, b"PEMU"), (0x000, b"\x00\x00\xff\xff\x00"), (0x7FF, b""),
              (0x555, bytes(range(8))), (0x0F0, b"\x7f"), (0x3E0, b"\xff" * 8)]
    tx = CanNode(CAN_RX_BIT, tx_queue=frames, ack=False, name="tx")
    got = await can_rx_run(b, tx)
    assert tx.acked == [True] * len(frames), tx.acked
    assert [can.rx_decode(f)[:2] for f in got] == frames, got
    assert not tx.errors, tx.errors


async def can_rx_bad_crc(b):
    """v4: a frame with a wrong CRC is reported as an error and not
    acknowledged; the next good frame is received normally."""
    frames = [(0x2A5, b"\x01\x02", False, True), (0x2A5, b"\x01\x02")]
    tx = CanNode(CAN_RX_BIT, tx_queue=frames, ack=False, name="tx")
    got = await can_rx_run(b, tx, give_up_after_error=True)
    assert tx.acked == [False, True], tx.acked
    assert got[0][-1] == can.RX_ERROR, got
    assert can.rx_decode(got[1])[:2] == (0x2A5, b"\x01\x02"), got
    assert tx.errors == ["tx: CRC error"], tx.errors     # the sender's own check of frame 1


# ----- SPI device (v4) -----

SPID_MISO = 0       # uio[0]
SPID_MOSI = 8       # ui_in[0]; SCK = ui_in[1], CS_N = ui_in[2]


async def spi_device_run(b, frames, replies):
    cfg = EngineConfig(clkdiv=0, out_base=SPID_MISO, set_base=SPID_MISO, set_count=1,
                       in_base=SPID_MOSI, jmp_pin=SPID_MOSI + 2, autopush=True,
                       push_thresh=8, x=0xFF00)
    await b.setup(0, program("spi_device"), 0, cfg)
    ctl = SpiController(SPID_MISO, frames)              # connected (CS_N high) before
    for k, port in enumerate(ctl.ports()):              # the engine starts: CS_N, SCK, MOSI
        b.ui(port, (SPID_MOSI - 8) + (2, 1, 0)[k])
    for r in replies:
        await b.push(0, r << 8)
    await b.dma(0, 0, 64, rx=True)                      # received bytes go to the packet RAM
    await b.start(1)
    ctl.go = True
    await b.run_until(lambda: ctl.done, limit=200_000)
    addr, _ = await b.dma_state(0)
    return ctl, await b.ram_read(0, addr)


async def spi_device(b):
    """v4: the chip as an SPI device. It answers from the TX FIFO (then 0xFF
    when that runs dry) and pushes every byte it receives."""
    ctl, got = await spi_device_run(b, [[0x9F, 0x11, 0x22, 0x33], [0x05, 0x00]], [0xA5, 0x3C, 0x0F])
    assert ctl.received == [[0xA5, 0x3C, 0x0F, 0xFF], [0xFF, 0xFF]], ctl.received
    assert got == [0x9F, 0x11, 0x22, 0x33, 0x05, 0x00], got


async def spi_device_abort(b):
    """v4: the controller deselects in the middle of a byte. `wake pin` ends
    the clock wait, the partial byte is dropped, and the next transfer is
    received aligned."""
    ctl, got = await spi_device_run(b, [([0xC3, 0xFF], 12), [0x5A, 0x81]], [0x12, 0x34, 0x56])
    assert ctl.received == [[0x12], [0x56, 0xFF]], ctl.received   # 0x34 was cut off
    assert got == [0xC3, 0x5A, 0x81], got               # the half byte was dropped


# ----- rotary encoder (v4) -----

ENC_A = 8           # ui_in[0]; B = ui_in[1]


async def encoder_run(b, moves, period):
    enc = QuadratureEncoder(moves, period)
    for k, port in enumerate(enc.ports()):              # A, B
        b.ui(port, ENC_A - 8 + k)
    cfg = EngineConfig(in_base=ENC_A, jmp_pin=ENC_A + 1)
    await b.setup(0, program("quadrature"), 0, cfg)
    await b.start(1)
    await b.run(100)
    enc.go = True
    await b.run_until(lambda: enc.done, limit=200_000)
    await b.run(40)
    x = await b.read(0x40 + 9, pop=False)               # engine 0's X: the position
    return enc, x - 0x10000 if x & 0x8000 else x


async def encoder(b):
    """v4: a rotary encoder turned both ways. The chip's count (X) matches
    the knob: add counts, wake catches whichever input changes."""
    enc, pos = await encoder_run(b, [37, -50, 5, -1, 1], period=24)
    assert pos == enc.position == -8, (pos, enc.position)


async def encoder_fast(b):
    """v4: edges every 12 clocks (4 MHz of edges at 50 MHz): none missed."""
    enc, pos = await encoder_run(b, [64, -3, 20], period=12)
    assert pos == enc.position == 81, (pos, enc.position)


# ----- DHT22 sensor (v4) -----

DHT_PIN = 2         # uio[2], with a pull-up
DHT_US = 5          # tests run 10x faster than real time: CLKDIV = 4


def dht_cfg():
    return EngineConfig(clkdiv=DHT_US - 1, in_base=DHT_PIN, set_base=DHT_PIN, set_count=1,
                        autopush=True, push_thresh=8, y=48, timeout=200)


async def dht22(b):
    """v4: read a DHT22: 40 pulses classified by length with add and skip."""
    sensor = b.gpio(Dht22(DHT_PIN, DHT_US, humidity=652, temperature=-101))
    await b.setup(0, program("dht22"), 0, dht_cfg())
    await b.start(1)
    for _ in range(2):                                  # two readings
        await b.push(0, 1)
        data = await b.drain(0, 5)
        assert data == sensor.bytes, (data, sensor.bytes)
        assert sum(data[:4]) & 255 == data[4]
        humidity = (data[0] << 8 | data[1]) / 10
        raw = data[2] << 8 | data[3]
        temp = (-(raw & 0x7FFF) if raw & 0x8000 else raw) / 10
        assert (humidity, temp) == (65.2, -10.1), (humidity, temp)
        await b.run(3000 * DHT_US)
    assert sensor.readings == 2 and not sensor.errors, sensor.errors


async def dht22_absent(b):
    """v4: no sensor: the wait timeout ends the attempt, nothing is pushed,
    and the next request is handled."""
    sensor = b.gpio(Dht22(DHT_PIN, DHT_US, present=False))
    await b.setup(0, program("dht22"), 0, dht_cfg())
    await b.start(1)
    await b.push(0, 1)
    await b.run(2000 * DHT_US)
    st = await b.status(0)
    assert st & (1 << 12), "the attempt should have timed out"
    assert (st >> 8) & 7 == 0, "nothing should have been pushed"
    sensor.present = True
    await b.push(0, 1)
    assert await b.drain(0, 5) == sensor.bytes


# ----- DMX512 receiver -----

DMX_PIN = 10        # ui_in[2]
DMX_DIV = 3         # tests run 6.25x faster than real time (32 clocks per bit)


async def dmx_rx(b):
    """A DMX512 fixture at address 5 with 4 channels: from two frames of
    24 slots it keeps exactly channels 5-8, and finds each break."""
    frames = [[0] + [(17 * k + 3) & 255 for k in range(1, 24)],
              [0] + [(255 - 9 * k) & 255 for k in range(1, 24)]]
    dmx = b.ui(DmxController(frames, bit=8 * (DMX_DIV + 1), gap_bits=1), DMX_PIN - 8)
    cfg = EngineConfig(clkdiv=DMX_DIV, in_base=DMX_PIN, jmp_pin=DMX_PIN, in_right=True,
                       timeout=100, crc_init=4)
    await b.setup(0, program("dmx_rx"), 0, cfg)
    await b.push(0, 5)                                   # start address N
    await b.dma(0, 0, 8, rx=True)
    await b.start(1)
    dmx.go = True
    await b.run_until(lambda: dmx.done, limit=400_000)
    got = [w >> 8 for w in await b.ram_read(0, 8)]
    assert got == frames[0][5:9] + frames[1][5:9], (got, frames[0][5:9], frames[1][5:9])




# ----- VGA (sw/pemu/vga.py) -----

async def vga(b):
    """640x480 VGA from both engines: the first three picture rows (45 lines)
    have exact sync timing and every pixel where a monitor expects it. The
    full-frame check (vsync, 525 lines) is in sw/tests/test_vga.py."""
    from . import vga as v
    img = v.demo_picture()
    await v.setup(b, v.picture(img), program("vga_sync"), program("vga_pixels"))
    mon = b.monitor(v.VgaMonitor())   # from here, line 1 has the first HSYNC edge
    await b.run(46 * v.LINE_CLOCKS)
    rows, problems = v.first_lines(mon.samples, 44)
    assert not problems, problems[:5]
    want = v.expected_frame(img)
    for y, row in enumerate(rows, start=1):
        assert row == want[y], f"line {y}: " + str(
            [x for x in range(640) if row[x] != want[y][x]][:10])


# ----- Music box and MIDI synth (sw/pemu/music.py) -----
# Fast settings for simulation: 1-clock ticks, so a "note" lasts microseconds.

def _high_runs(rec, start=0):
    """Lengths of the high pulses recorded since run index `start`."""
    return [n for level, n in rec.runs[start:] if level == 1]


async def music_box(b):
    """Two engines play a song from the packet RAM with no host: every note
    has the right pitch and length, repeated notes are separated by a gap,
    a rest is silent, and the speaker stops after the last note."""
    from . import music
    from .periph import PulseRecorder
    notes = [(20, 2), (0, 1), (33, 1), (33, 1), (47, 3)]     # (x, steps); x 0 = rest
    words = [(steps - 1) << 12 | x for x, steps in notes] + [0]
    step, gap = 300, 1                                         # conductor CRC, tone CRC
    await music.music_box(b, words, program("tone"), program("conductor"),
                          tone_cfg=music.tone_config(clkdiv=0, gap=gap),
                          conductor_cfg=music.conductor_config(clkdiv=0, step=step))
    rec = b.monitor(PulseRecorder(music.SPEAKER_PIN))
    total = sum(steps * (step + 3) + 3 for _, steps in notes)
    await b.run(total + 400)
    # Split the recording into notes at the gaps (low runs longer than a half wave).
    played, current = [], []
    for level, n in rec.runs:
        if level == 1:
            current.append(n)
        elif current and n > current[-1] + 1:
            played.append(current)
            current = []
    if current:
        played.append(current)
    want = [(x, steps) for x, steps in notes if x]
    assert [p[0] for p in played] == [x + 4 for x, _ in want], [p[:2] for p in played]
    for p, (x, steps) in zip(played, want):
        assert set(p) == {x + 4}, p
        length = steps * (step + 3) + 3 - 16 * (gap + 1) - 6
        assert abs(len(p) - length / (2 * (x + 4))) <= 1.5, (x, len(p), length / (2 * (x + 4)))
    assert rec.level == 0 and rec.count > 200, "silent after the last note"


async def midi_synth(b):
    """MIDI in, sound out: note on, a second key (last-note priority),
    releasing it falls back to the first, and note-on with velocity 0
    silences the voice. Real-time clock bytes in between are ignored."""
    from . import music
    from .periph import PulseRecorder, UartSource
    from .scenarios import UART_CPB, UART_DIV
    tick_s = 1 / (2 * music.frequency(60) * 44)      # note 60 -> 44-tick half waves
    rec = b.monitor(PulseRecorder(music.SPEAKER_PIN))
    src = b.ui(UartSource(UART_CPB), music.MIDI_PIN - 8)
    await music.midi_synth_start(b, program("tone"), program("uart_rx"),
                                 tone_cfg=music.tone_config(clkdiv=0, gap=0),
                                 rx_cfg=music.midi_rx_config(clkdiv=UART_DIV))
    parser, voice = music.MidiParser(), music.MonoVoice()
    steps = [([0x90, 60, 100], 60), ([0xF8, 64, 90], 64), ([0x80, 64, 0], 60),
             ([0x90, 60, 0], None)]
    for msg, expect in steps:
        src.queue.extend(msg)
        for byte in await b.drain(0, len(msg)):
            for ev in parser.feed(byte):
                note = voice.update(ev)
                if note is not False:
                    await music.midi_synth_play(b, note, tick_s)
        assert voice.playing() == expect
        mark = len(rec.runs)
        await b.run(1200)
        highs = _high_runs(rec, mark)[1:]
        if expect is None:
            assert not highs and rec.level == 0, highs
        else:
            assert highs and set(highs) == {music.half_period_x(expect, tick_s) + 4}, highs



EXT_SCENARIOS = [onewire, onewire_absent, nec_ir, nec_ir_tolerance, nec_ir_fast, nec_ir_midframe, ps2, ps2_busy, ps2_glitch, swd, swd_errors, can_tx, can_arbitration, can_busy, can_rx, can_rx_bad_crc, spi_device, spi_device_abort,
                 encoder, encoder_fast, dht22, dht22_absent, dmx_rx, vga,
                 music_box, midi_synth]
