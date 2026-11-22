"""VGA output: 640x480 at 60 Hz from the two engines (vga_sync + vga_pixels).

The pins follow the TinyVGA PMOD, plugged into the bidirectional (uio)
header: uio[0] R1, [1] G1, [2] B1, [3] VSYNC, [4] R0, [5] G0, [6] B0,
[7] HSYNC. Engine 0 makes the sync pulses and a dark blue background,
engine 1 draws a 64x32 one-bit picture from the packet RAM in green.

`picture()` turns text into the 128 words, `setup()` starts it on any bench,
and `VgaMonitor` + `decode()` check the timing and recover the picture the
way a monitor would.
"""

from .host import EngineConfig
from .model import A_FLAG_SET, A_PIN_OE

R1, G1, B1, VSYNC, R0, G0, B0, HSYNC = range(8)
WIDTH, HEIGHT = 64, 32          # the picture
CLOCKS_PER_PIXEL = 2            # 50 MHz clock, 25 MHz pixel clock
H_VISIBLE, H_FRONT, H_SYNC, H_BACK = 640, 16, 96, 48
V_VISIBLE, V_FRONT, V_SYNC, V_BACK = 480, 10, 2, 33
H_TOTAL = H_VISIBLE + H_FRONT + H_SYNC + H_BACK         # 800 pixels
V_TOTAL = V_VISIBLE + V_FRONT + V_SYNC + V_BACK         # 525 lines
LINE_CLOCKS = H_TOTAL * CLOCKS_PER_PIXEL                # 1600
FRAME_CLOCKS = LINE_CLOCKS * V_TOTAL                    # 840,000 (59.5 Hz)

# A 5x7 font, just the characters the demo pictures use.
FONT = {
    " ": [0, 0, 0, 0, 0, 0, 0],
    "0": [14, 17, 19, 21, 25, 17, 14], "1": [4, 12, 4, 4, 4, 4, 14],
    "2": [14, 17, 1, 2, 4, 8, 31], "3": [31, 2, 4, 2, 1, 17, 14],
    "4": [2, 6, 10, 18, 31, 2, 2], "5": [31, 16, 30, 1, 1, 17, 14],
    "6": [6, 8, 16, 30, 17, 17, 14], "7": [31, 1, 2, 4, 8, 8, 8],
    "8": [14, 17, 17, 14, 17, 17, 14], "9": [14, 17, 17, 15, 1, 2, 12],
    "A": [14, 17, 17, 31, 17, 17, 17], "B": [30, 17, 17, 30, 17, 17, 30],
    "C": [14, 17, 16, 16, 16, 17, 14], "D": [30, 17, 17, 17, 17, 17, 30],
    "E": [31, 16, 16, 30, 16, 16, 31], "F": [31, 16, 16, 30, 16, 16, 16],
    "G": [14, 17, 16, 23, 17, 17, 15], "H": [17, 17, 17, 31, 17, 17, 17],
    "I": [14, 4, 4, 4, 4, 4, 14], "K": [17, 18, 20, 24, 20, 18, 17],
    "L": [16, 16, 16, 16, 16, 16, 31], "M": [17, 27, 21, 21, 17, 17, 17],
    "N": [17, 17, 25, 21, 19, 17, 17], "O": [14, 17, 17, 17, 17, 17, 14],
    "P": [30, 17, 17, 30, 16, 16, 16], "R": [30, 17, 17, 30, 20, 18, 17],
    "S": [15, 16, 16, 14, 1, 1, 30], "T": [31, 4, 4, 4, 4, 4, 4],
    "U": [17, 17, 17, 17, 17, 17, 14], "V": [17, 17, 17, 17, 17, 10, 4],
    "W": [17, 17, 17, 21, 21, 21, 10], "X": [17, 17, 10, 4, 10, 17, 17], "Y": [17, 17, 10, 4, 4, 4, 4],
    "Z": [31, 1, 2, 4, 8, 16, 31], "!": [4, 4, 4, 4, 4, 0, 4],
    "-": [0, 0, 0, 31, 0, 0, 0], ".": [0, 0, 0, 0, 0, 12, 12],
}


def blank():
    return [[0] * WIDTH for _ in range(HEIGHT)]


def draw_text(img, text, top, scale=1):
    """Draw `text` centred horizontally with its top at row `top`."""
    w = len(text) * 6 * scale - scale
    left = (WIDTH - w) // 2
    for i, ch in enumerate(text.upper()):
        for r, bits in enumerate(FONT[ch]):
            for c in range(5):
                if bits >> (4 - c) & 1:
                    for dy in range(scale):
                        for dx in range(scale):
                            y, x = top + r * scale + dy, left + (i * 6 + c) * scale + dx
                            if 0 <= y < HEIGHT and 0 <= x < WIDTH:
                                img[y][x] = 1
    return img


def border(img):
    for x in range(WIDTH):
        img[0][x] = img[HEIGHT - 1][x] = 1
    for y in range(HEIGHT):
        img[y][0] = img[y][WIDTH - 1] = 1
    return img


def demo_picture():
    return text_picture(["PEMU", "HELLO VGA"])


def text_picture(lines):
    """A bordered picture of up to 3 lines of text (10 characters each).
    A first line of up to 5 letters is drawn twice as big when it is alone
    or has one line under it (the playground draws pictures the same way)."""
    img = border(blank())
    lines = [line.upper()[:10] for line in lines[:3]]
    for line in lines:
        bad = set(line) - set(FONT)
        if bad:
            raise ValueError(f"no picture for {''.join(sorted(bad))!r} in the VGA font")
    lines = [line for line in lines if line]
    if len(lines) == 1 and len(lines[0]) <= 5:
        draw_text(img, lines[0], 9, scale=2)
    elif len(lines) == 2 and len(lines[0]) <= 5:
        draw_text(img, lines[0], 3, scale=2)
        draw_text(img, lines[1], 21)
    else:
        top = (HEIGHT - 9 * len(lines) + 2) // 2
        for i, line in enumerate(lines):
            draw_text(img, line, top + 9 * i)
    return img


def picture(img):
    """64x32 rows of 0/1 to the 128 packet-RAM words (4 per row, MSB left)."""
    words = []
    for row in img:
        for w in range(4):
            v = 0
            for b in row[16 * w:16 * w + 16]:
                v = v << 1 | (b & 1)
            words.append(v)
    return words


def configs():
    sync = EngineConfig(clkdiv=15, set_base=VSYNC, set_count=5, crc_init=478, x=14)
    pixels = EngineConfig(clkdiv=1, out_base=G1, set_base=G1, set_count=1, pull_thresh=15)
    return sync, pixels


async def setup(b, words, sync_prog, pixel_prog):
    """Load both programs and the picture, then start (any bench)."""
    sync, pixels = configs()
    await b.write(A_PIN_OE, 0xFF)
    await b.setup(0, sync_prog, 0, sync)
    await b.setup(1, pixel_prog, 0, pixels)
    await b.ram_write(0, words)
    await b.dma(1, 0, 255)          # engine 1, ring over the whole RAM
    await b.write(A_FLAG_SET, 1 << 1)   # load the first row at once
    await b.start(3)


class VgaMonitor:
    """Records the 8 uio wires on every clock (what the VGA cable carries)."""

    def __init__(self):
        self.samples = bytearray()

    def step(self, wires):
        self.samples.append(wires & 0xFF)
        return 0


def hsync_falls(samples):
    hs = [s >> HSYNC & 1 for s in samples]
    return [i for i in range(1, len(hs)) if hs[i - 1] and not hs[i]]


def read_line(samples, line, problems):
    """The 640 colours of the line whose HSYNC falls at clock `line`, as a
    monitor samples them, counting from that edge. Also checks that each
    pixel holds its colour for both of its clocks and that nothing is lit in
    the blanking, where monitors measure black."""
    x0 = line + (H_SYNC + H_BACK) * CLOCKS_PER_PIXEL
    pixels = []
    for x in range(H_VISIBLE):
        a, b = samples[x0 + CLOCKS_PER_PIXEL * x], samples[x0 + CLOCKS_PER_PIXEL * x + 1]
        if (a ^ b) & 0x77:
            problems.append(f"pixel {x} of the line at clock {line} changes mid-pixel")
        pixels.append(b & 0x77)
    for t in list(range(line, x0)) + list(range(x0 + H_VISIBLE * CLOCKS_PER_PIXEL,
                                                 line + LINE_CLOCKS)):
        if samples[t] & 0x77:
            problems.append(f"colour during blanking at clock {t}")
            break
    return pixels


def check_timing(samples, falls, problems):
    for a, b in zip(falls, falls[1:]):
        if b - a != LINE_CLOCKS:
            problems.append(f"line at clock {a} is {b - a} clocks long")
    for a in falls:
        low = 0
        while a + low < len(samples) and not samples[a + low] >> HSYNC & 1:
            low += 1
        if a + low < len(samples) and low != H_SYNC * CLOCKS_PER_PIXEL:
            problems.append(f"hsync at clock {a} is {low} clocks long")


def decode(samples):
    """Check the sync timing and read back every complete frame, as a monitor
    would. Returns (frames, problems): each frame is 480 rows of 640 colours
    (bits 0-2 = the high R G B bits, bits 4-6 the low ones)."""
    problems = []
    falls = hsync_falls(samples)
    check_timing(samples, falls, problems)
    # Lines whose VSYNC is low at the HSYNC edge are sync lines.
    vs_line = [samples[a] >> VSYNC & 1 for a in falls]
    starts = [i for i in range(1, len(vs_line)) if vs_line[i - 1] and not vs_line[i]]
    for a, b in zip(starts, starts[1:]):
        if b - a != V_TOTAL:
            problems.append(f"frame at line {a} is {b - a} lines")
    frames = []
    for s in starts:
        if s + V_TOTAL > len(falls) - 1:
            break
        if vs_line[s:s + V_SYNC] != [0] * V_SYNC or vs_line[s + V_SYNC] != 1:
            problems.append(f"vsync at line {s} is not {V_SYNC} lines")
        first = s + V_SYNC + V_BACK
        frames.append([read_line(samples, line, problems)
                       for line in falls[first:first + V_VISIBLE]])
    return frames, problems


def first_lines(samples, n):
    """Visible lines 1 to n after a start (line 0 begins before the first
    HSYNC edge): (rows, problems), for checks shorter than a frame."""
    problems = []
    falls = hsync_falls(samples)
    check_timing(samples, falls[:n + 1], problems)
    for a in falls[:n]:
        if not samples[a] >> VSYNC & 1:
            problems.append(f"vsync during visible line at clock {a}")
    return [read_line(samples, a, problems) for a in falls[:n]], problems


def to_rgb(c):
    """A colour from the wires as 8-bit RGB (2 bits per channel)."""
    def ch(hi, lo):
        return 85 * (2 * (c >> hi & 1) + (c >> lo & 1))
    return ch(R1, R0), ch(G1, G0), ch(B1, B0)


def expected_frame(img, background=1 << B0, ink=1 << G1):
    """The 640x480 screen a picture should give."""
    rows = []
    for y in range(V_VISIBLE):
        src = img[y // (V_VISIBLE // HEIGHT)]
        rows.append([background | (ink if src[x // (H_VISIBLE // WIDTH)] else 0)
                     for x in range(H_VISIBLE)])
    return rows
