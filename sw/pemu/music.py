"""Music: a music box that plays from the packet RAM with no host, and a
MIDI synthesizer.

Music box: `tone` (engine 1) plays square-wave notes and `conductor`
(engine 0) times them. Both engines' DMA channels stream the same song from
the packet RAM, one word per note, so once started the chip plays the whole
song by itself (up to 127 notes and a final rest).

MIDI synth: `uart_rx` (engine 0) receives MIDI at 31,250 baud on ui_in[3]
and `tone` (engine 1) plays. The host turns MIDI bytes into notes
(MidiParser + MonoVoice) and gives `tone` one word per note change.

Pins: the speaker on uio[7] (pin 7, as on the Tiny Tapeout Audio Pmod),
MIDI in on ui_in[3] (pin 11), through the usual 6N138 optocoupler.
"""

from .host import EngineConfig
from .model import A_FLAG_SET, A_PIN_OE

SPEAKER_PIN = 7
MIDI_PIN = 11
CLK_HZ = 50_000_000
MIDI_BAUD = 31_250
TONE_DIV = 49                   # 1 us ticks
CONDUCTOR_DIV = 499             # 10 us ticks
NEXT = 0                        # the flag that moves `tone` to its next note
X_MAX = 4095

NAMES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def note_number(name):
    """'C4' -> 60, 'F#5' -> 78, 'Bb3' -> 58 (MIDI note numbers)."""
    n = NAMES[name[0].upper()]
    rest = name[1:]
    while rest and rest[0] in "#b":
        n += 1 if rest[0] == "#" else -1
        rest = rest[1:]
    return n + 12 * (int(rest) + 1)


def frequency(note):
    return 440.0 * 2 ** ((note - 69) / 12)


def half_period_x(note, tick_s=(TONE_DIV + 1) / CLK_HZ):
    """`tone`'s value for a MIDI note: each half wave is (x + 4) ticks."""
    x = round(1 / (2 * frequency(note) * tick_s)) - 4
    if not 1 <= x <= X_MAX:
        raise ValueError(f"note {note} is out of range at this tick")
    return x


def played_frequency(x, tick_s=(TONE_DIV + 1) / CLK_HZ):
    return 1 / (2 * (x + 4) * tick_s)


def word(note, steps=1, tick_s=(TONE_DIV + 1) / CLK_HZ):
    """One note word; `note` None is a rest. `steps` is 1-16."""
    if not 1 <= steps <= 16:
        raise ValueError("a note lasts 1-16 steps")
    x = 0 if note is None else half_period_x(note, tick_s)
    return (steps - 1) << 12 | x


def song_words(song, tick_s=(TONE_DIV + 1) / CLK_HZ):
    """[(name or None, steps)] -> packet RAM words, ending with a rest so
    the speaker goes quiet when the song ends."""
    words = [word(None if n is None else note_number(n), s, tick_s) for n, s in song]
    words.append(word(None))
    if len(words) > 128:
        raise ValueError("a song is at most 127 notes")
    return words


def step_crc(step_s, tick_s=(CONDUCTOR_DIV + 1) / CLK_HZ):
    """`conductor`'s CRC value for a step length (a step is CRC + 3 ticks)."""
    return round(step_s / tick_s) - 3


# Beethoven, Symphony No. 9 ("Ode to Joy"), public domain. Steps are 8th notes.
ODE_TO_JOY = [
    ("E4", 2), ("E4", 2), ("F4", 2), ("G4", 2), ("G4", 2), ("F4", 2), ("E4", 2), ("D4", 2),
    ("C4", 2), ("C4", 2), ("D4", 2), ("E4", 2), ("E4", 3), ("D4", 1), ("D4", 4),
    ("E4", 2), ("E4", 2), ("F4", 2), ("G4", 2), ("G4", 2), ("F4", 2), ("E4", 2), ("D4", 2),
    ("C4", 2), ("C4", 2), ("D4", 2), ("E4", 2), ("D4", 3), ("C4", 1), ("C4", 4),
    ("D4", 2), ("D4", 2), ("E4", 2), ("C4", 2), ("D4", 2), ("E4", 1), ("F4", 1), ("E4", 2),
    ("C4", 2), ("D4", 2), ("E4", 1), ("F4", 1), ("E4", 2), ("D4", 2), ("C4", 2), ("D4", 2),
    ("G3", 4),
    ("E4", 2), ("E4", 2), ("F4", 2), ("G4", 2), ("G4", 2), ("F4", 2), ("E4", 2), ("D4", 2),
    ("C4", 2), ("C4", 2), ("D4", 2), ("E4", 2), ("D4", 3), ("C4", 1), ("C4", 4),
]


def tone_config(clkdiv=TONE_DIV, gap=312, pin=SPEAKER_PIN):
    return EngineConfig(clkdiv=clkdiv, side_base=pin, crc_init=gap)


def conductor_config(clkdiv=CONDUCTOR_DIV, step=step_crc(0.25)):
    return EngineConfig(clkdiv=clkdiv, crc_init=step)


async def music_box(b, words, tone_prog, conductor_prog, tone_cfg=None, conductor_cfg=None,
                    pin=SPEAKER_PIN):
    """Load a song into the packet RAM and start it (any bench)."""
    await b.write(A_PIN_OE, 1 << pin)
    await b.setup(0, conductor_prog, 0, conductor_cfg or conductor_config())
    await b.setup(1, tone_prog, 0, tone_cfg or tone_config(pin=pin))
    await b.ram_write(0, words)
    await b.dma(0, 0, len(words))
    await b.dma(1, 0, len(words))
    await b.write(A_FLAG_SET, 1 << NEXT)    # the first note plays at once
    await b.start(3)


def midi_rx_config(clkdiv=CLK_HZ // (8 * MIDI_BAUD) - 1, pin=MIDI_PIN):
    """uart_rx at MIDI speed: 8 ticks per bit, so CLKDIV 199 at 50 MHz."""
    return EngineConfig(clkdiv=clkdiv, in_base=pin, jmp_pin=pin, in_right=True)


class MidiParser:
    """MIDI bytes to ('on', note, velocity) / ('off', note) events, with
    running status. Other messages are skipped; real-time bytes (clock,
    active sensing) may appear anywhere and are ignored."""

    LENGTHS = {0x80: 2, 0x90: 2, 0xA0: 2, 0xB0: 2, 0xC0: 1, 0xD0: 1, 0xE0: 2}

    def __init__(self):
        self.status = None
        self.data = []

    def feed(self, byte):
        byte &= 0xFF
        if byte >= 0xF8:
            return []                    # real-time: no effect on running status
        if byte & 0x80:
            self.status = byte if byte < 0xF0 else None   # system messages cancel it
            self.data = []
            return []
        if self.status is None:
            return []
        self.data.append(byte)
        if len(self.data) < self.LENGTHS[self.status & 0xF0]:
            return []
        kind, data, self.data = self.status & 0xF0, self.data, []
        if kind == 0x90 and data[1]:
            return [("on", data[0], data[1])]
        if kind in (0x80, 0x90):
            return [("off", data[0])]
        return []


class MonoVoice:
    """One voice, last-note priority: releasing the newest key falls back to
    a key still held. `update(event)` returns the note to play now (None =
    silence), or False if nothing changes."""

    def __init__(self):
        self.held = []

    def playing(self):
        return self.held[-1] if self.held else None

    def update(self, event):
        before = self.playing()
        note = event[1]
        if note in self.held:
            self.held.remove(note)
        if event[0] == "on":
            self.held.append(note)
        after = self.playing()
        return after if after != before else False


async def midi_synth_start(b, tone_prog, rx_prog, tone_cfg=None, rx_cfg=None, pin=SPEAKER_PIN):
    await b.write(A_PIN_OE, 1 << pin)
    await b.setup(0, rx_prog, 0, rx_cfg or midi_rx_config())
    await b.setup(1, tone_prog, 0, tone_cfg or tone_config(pin=pin))
    await b.start(3)


async def midi_synth_play(b, note, tick_s=(TONE_DIV + 1) / CLK_HZ):
    """Switch the synth to `note` (None = silence)."""
    await b.push(1, word(note, 1, tick_s))
    await b.write(A_FLAG_SET, 1 << NEXT)
