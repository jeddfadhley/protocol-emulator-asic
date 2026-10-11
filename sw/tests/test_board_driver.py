"""The MicroPython board driver, run against the reference model.

A fake transport decodes the driver's SPI byte stream into model register
accesses, so the driver's framing and demo sequences are checked end to end
(everything except the GPIO bit-banging itself).
"""

import os
import subprocess
import sys

import pytest

from pemu.model import Core

BOARD = os.path.join(os.path.dirname(__file__), "..", "board")
sys.path.insert(0, BOARD)

import pemu_board  # noqa: E402


class ModelLink:
    """Implements transfer() by running each SPI transaction on the model."""

    def __init__(self, idle_clocks=400):
        self.core = Core()
        self.idle = idle_clocks  # clocks that pass per transaction, like a slow host

    def transfer(self, data):
        cmd, payload = data[0], data[1:]
        addr, rd = cmd & 0x7F, cmd & 0x80
        rx = bytearray(1)
        for i in range(0, len(payload), 2):
            if rd:
                w = self.core.clock(re=True, addr=addr)
                self.core.clock(rc=True, addr=addr)
                rx += bytes((w >> 8, w & 0xFF))
            else:
                self.core.clock(we=True, addr=addr, wdata=payload[i] << 8 | payload[i + 1])
            if not (addr & 0x60 == 0x40 and addr & 15 == 15):
                addr = (addr + 1) & 0x7F
        for _ in range(self.idle):
            self.core.clock(ui_in=0xFF, uio_in=0xFF)
        return rx


def test_demos_file_is_current():
    out = subprocess.run([sys.executable, os.path.join(BOARD, "gen_demos.py")],
                         capture_output=True, text=True, check=True).stdout
    assert out == open(os.path.join(BOARD, "pemu_demos.py")).read(), \
        "regenerate: python3 sw/board/gen_demos.py > sw/board/pemu_demos.py"


def test_selftest_passes_on_model():
    assert pemu_board.selftest(ModelLink())


def test_check_and_burst():
    chip = pemu_board.Pemu(ModelLink())
    chip.check()
    chip.write(0, 1, 2, 3)
    assert chip.read(0, 3) == [1, 2, 3]


def test_bad_id_raises():
    class Dead:
        def transfer(self, data):
            return bytearray(len(data))
    with pytest.raises(RuntimeError):
        pemu_board.Pemu(Dead()).check()
