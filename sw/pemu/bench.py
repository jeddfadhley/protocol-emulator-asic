"""A board around the Python model: the core plus external device models.

The bench API is async so that the same test scenarios (scenarios.py) run
here and on the RTL under cocotb (test/rtl_bench.py), where every host access
goes over the SPI interface.
"""

from .model import Core
from .periph import resolve
from .hostapi import HostApi


class ModelBench(HostApi):
    def __init__(self, core=None):
        self.core = core or Core()
        self.gpio_devs = []
        self.ui_devs = []  # (device, ui bit)
        self.monitors = []
        self.dev_low = 0
        self.ui_drive = 0
        self.cycles = 0
        self.wires = 0xFF

    # ----- wiring -----

    def gpio(self, dev):
        self.gpio_devs.append(dev)
        return dev

    def ui(self, dev, ui_bit):
        self.ui_devs.append((dev, ui_bit))
        self.ui_drive |= 1 << ui_bit  # idle high until the device speaks
        return dev

    def monitor(self, dev):
        self.monitors.append(dev)
        return dev

    # ----- clocking -----

    def _cycle(self, we=False, re=False, addr=0, wdata=0, rc=False):
        c = self.core
        wires, uio, ui = resolve(c.pin_out, c.pin_oe, self.dev_low, self.ui_drive)
        self.wires = wires
        low = 0
        for d in self.gpio_devs:
            low |= d.step(wires)
        drive = self.ui_drive
        for d, b in self.ui_devs:
            drive = (drive & ~(1 << b)) | (d.step(wires) << b)
        for d in self.monitors:
            d.step(wires)
        rdata = c.clock(ui, uio, we=we, re=re, addr=addr, wdata=wdata, rc=rc)
        self.dev_low, self.ui_drive = low & 0xFF, drive & 0xFF
        self.cycles += 1
        return rdata

    async def run(self, n):
        for _ in range(n):
            self._cycle()

    async def run_until(self, cond, limit=1_000_000):
        for _ in range(limit):
            if cond():
                return
            self._cycle()
        raise TimeoutError("condition not met")

    async def write(self, addr, value):
        self._cycle(we=True, addr=addr, wdata=value)

    async def read(self, addr, pop=True):
        rdata = self._cycle(re=True, addr=addr)
        self._cycle(rc=pop, addr=addr)
        return rdata
