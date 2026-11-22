"""VGA: whole frames from the model, checked the way a monitor sees them."""

import asyncio

from pemu import vga
from pemu.bench import ModelBench
from pemu.scenarios import program


def test_picture_words():
    img = vga.blank()
    img[0][0] = img[31][63] = img[5][17] = 1
    words = vga.picture(img)
    assert len(words) == 128
    assert words[0] == 0x8000 and words[127] == 0x0001 and words[5 * 4 + 1] == 0x4000


def test_two_frames():
    """Two complete frames: 525 lines of 1600 clocks, 96-pixel HSYNC,
    2-line VSYNC, black in the blanking, and the picture on every line."""
    async def run():
        b = ModelBench()
        img = vga.demo_picture()
        await vga.setup(b, vga.picture(img), program("vga_sync"), program("vga_pixels"))
        mon = b.monitor(vga.VgaMonitor())
        await b.run(3 * vga.FRAME_CLOCKS - 2000)
        return img, vga.decode(mon.samples)
    img, (frames, problems) = asyncio.run(run())
    assert not problems, problems[:5]
    assert len(frames) == 2
    want = vga.expected_frame(img)
    for f in frames:
        assert f == want
