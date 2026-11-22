# SPDX-License-Identifier: Apache-2.0
"""Top-level tests: the whole chip, driven through its SPI host interface.

Every protocol scenario from sw/pemu/scenarios.py runs here against real
firmware and device models; the same tests run on the gate-level netlist.
"""

import os
import random

import cocotb

from pemu.model import (A_FLAGS, A_ID, A_PIN_OE, A_PIN_OUT, A_RAM_ADDR, A_RAM_DATA,
                        A_VERSION, CHIP_ID)
from pemu.scenarios import SCENARIOS
from rtl_bench import RtlBench


async def bench(dut):
    b = RtlBench(dut)
    await b.start_clock()
    return b


@cocotb.test()
async def test_reset_state(dut):
    """Outputs are quiet after reset; host pins read back the chip ID."""
    b = await bench(dut)
    assert int(dut.uio_oe.value) == 0
    assert int(dut.uio_out.value) == 0
    assert int(dut.uo_out.value) & 0x7F == 0
    assert await b.read(A_ID) == CHIP_ID


@cocotb.test()
async def test_reset_strap(dut):
    """v4: ui_in[7] high during reset starts the dedicated outputs high (the
    idle level of UART, CAN TXD and chip selects); they stay high after
    reset is released, until something writes them."""
    b = RtlBench(dut)
    await b.start_clock(strap=1)
    assert int(dut.uo_out.value) & 0x7F == 0x7F
    assert int(dut.uio_oe.value) == 0
    await b.run(20)
    assert int(dut.uo_out.value) & 0x7F == 0x7F
    assert await b.read(A_PIN_OUT) == 0x7F00
    assert await b.read(A_VERSION) == 4
    await b.write(A_PIN_OUT, 0)
    assert int(dut.uo_out.value) & 0x7F == 0


@cocotb.test()
async def test_spi_registers(dut):
    """Register write/read, burst auto-increment, and a streaming port burst."""
    b = await bench(dut)
    await b.write(A_FLAGS, 0xA5)
    assert await b.read(A_FLAGS) == 0xA5
    rng = random.Random(7)
    words = [rng.randrange(1 << 16) for _ in range(64)]
    await b.write(A_RAM_ADDR, 0)
    await b.write_burst(A_RAM_DATA, words)        # packet RAM: the port stays put
    await b.write(A_RAM_ADDR, 0)
    assert await b.read_burst(A_RAM_DATA, 64) == words
    # A burst crosses from PIN_OUT into PIN_OE
    await b.write_burst(A_PIN_OUT, [0x1234, 0x00F0])
    assert await b.read_burst(A_PIN_OUT, 2) == [0x1234, 0x00F0]
    assert int(dut.uio_oe.value) == 0xF0
    assert int(dut.uio_out.value) == 0x34
    assert int(dut.uo_out.value) & 0x7F == 0x12


@cocotb.test()
async def test_fifo_port_burst(dut):
    """Bursts at a FIFO port stay on the port (no address increment)."""
    b = await bench(dut)
    # Engine 0, loopback-free: an IN/PUSH program is not needed; use the TX
    # FIFO -> OSR -> ISR path with a two-instruction program:
    #   pull ; mov isr, osr ; push   (wraps)
    from pemu import assemble, EngineConfig
    prog = assemble("pull\nmov isr, osr\npush")
    await b.setup(0, prog, 0, EngineConfig())
    await b.write_burst(0x4F, [0x1111, 0x2222, 0x3333])
    await b.start(1)
    await b.run(50)
    assert await b.read_burst(0x4F, 3) == [0x1111, 0x2222, 0x3333]


def _scenario_test(scenario):
    async def run(dut):
        b = await bench(dut)
        await scenario(b)

    run.__name__ = run.__qualname__ = f"test_{scenario.__name__}"
    run.__doc__ = scenario.__doc__
    return cocotb.test()(run)


for _s in SCENARIOS:
    globals()[f"test_{_s.__name__}"] = _scenario_test(_s)


@cocotb.test(skip=os.environ.get("VGA_FRAME") is None)
async def test_vga_frame(dut):
    """A whole VGA frame from the RTL (about 3 minutes, so only when VGA_FRAME
    is set; it names a PNG file for the picture, or is empty)."""
    from pemu import vga
    from pemu.scenarios import program
    b = await bench(dut)
    img = vga.demo_picture()
    await vga.setup(b, vga.picture(img), program("vga_sync"), program("vga_pixels"))
    mon = b.monitor(vga.VgaMonitor())
    await b.run(2 * vga.FRAME_CLOCKS)
    frames, problems = vga.decode(mon.samples)
    assert not problems, problems[:5]
    assert len(frames) == 1 and frames[0] == vga.expected_frame(img)
    if os.environ["VGA_FRAME"]:
        from PIL import Image
        im = Image.new("RGB", (640, 480))
        im.putdata([vga.to_rgb(c) for row in frames[0] for c in row])
        im.save(os.environ["VGA_FRAME"])
