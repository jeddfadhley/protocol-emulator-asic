"""Scenarios for the protocols added after the hardware was frozen
(docs/new-protocols.md). They run on the Python model and on the RTL, like
scenarios.py, and are appended to its SCENARIOS list.
"""

import os

from .asm import assemble_file
from .host import EngineConfig
from .model import A_PIN_OE, A_PIN_SET
from .periph_ext import CanBus, CanNode, NecRemote, OneWireDevice, Ps2Keyboard, SwdTarget, crc8_maxim, odd_parity
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


EXT_SCENARIOS = [onewire, onewire_absent, nec_ir, nec_ir_tolerance, nec_ir_fast, nec_ir_midframe, ps2, ps2_busy, ps2_glitch, swd, swd_errors, can_tx, can_arbitration, can_busy]
