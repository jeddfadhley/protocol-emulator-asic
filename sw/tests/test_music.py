"""The MIDI synth's note maths, chord stepping and firmware timing."""

import asyncio
import math

import pytest

from pemu import music
from pemu.bench import ModelBench
from pemu.periph import PulseRecorder
from pemu.scenarios import program


def cents(a, b):
    return abs(1200 * math.log2(a / b))


def test_synth_covers_the_piano_in_tune():
    tick = 1e-6
    for note in range(128):
        w = music.synth_word(note)
        want = 1 / (2 * music.frequency(note) * tick)
        assert cents(music.synth_half(w), want) < (7 if 21 <= note <= 108 else 15), note
    assert music.synth_word(21) >> 12 > 0          # the bottom A needs the repeat count
    assert music.synth_word(108) >> 12 == 0        # the top C uses single ticks
    assert music.synth_word(None) == 0


@pytest.mark.parametrize("word", [0x0001, 0x0007, 0x1005, 0x3002, 0xF005, 0x7020])
def test_synth_firmware_half_waves(word):
    """Every half wave lasts exactly (M + 1) * (X + 3) + 3 ticks, high and low."""
    async def go():
        b = ModelBench()
        rec = b.monitor(PulseRecorder(music.SPEAKER_PIN))
        await music.midi_synth_start(b, program("synth"), program("uart_rx"),
                                     synth_cfg=music.synth_config(clkdiv=0))
        await b.push(1, word)
        await b.flag_set(1 << music.NEXT)
        await b.run(12 * music.synth_half(word) + 100)
        return rec.runs[2:-1]
    runs = asyncio.run(go())
    assert len(runs) >= 8
    assert {n for _, n in runs} == {music.synth_half(word)}, runs


def test_arpeggiator():
    arp = music.Arpeggiator(step_s=0.025)
    assert arp.note(0) is None
    arp.update(("on", 64, 100), 0.0)
    assert [arp.note(t / 1000) for t in (0, 30, 60)] == [64, 64, 64]
    arp.update(("on", 60, 100), 0.1)
    arp.update(("on", 67, 100), 0.1)
    seq = [arp.note(0.1 + t / 1000) for t in range(0, 200, 25)]
    assert seq == [60, 64, 67, 60, 64, 67, 60, 64]   # lowest first, cycling
    arp.update(("off", 64), 0.4)
    assert [arp.note(0.4 + t / 1000) for t in (0, 25, 50)] == [60, 67, 60]
    arp.update(("off", 60), 0.5)
    arp.update(("off", 67), 0.5)
    assert arp.note(0.6) is None


def test_note_name():
    assert [music.note_name(n) for n in (21, 60, 61, 69, 108)] == ["A0", "C4", "C#4", "A4", "C8"]
    for n in range(12, 120):
        assert music.note_number(music.note_name(n)) == n
