# SPDX-License-Identifier: Apache-2.0
"""cocotb bench for the full chip: host access over SPI, devices on the pins.

Implements the same bench API as sw/pemu/bench.py (ModelBench), so the
protocol scenarios in sw/pemu/scenarios.py run unchanged on the RTL and on
the gate-level netlist.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge

from pemu.hostapi import HostApi
from pemu.periph import resolve

CS_BIT, SCK_BIT, MOSI_BIT, MISO_BIT = 4, 5, 6, 7
HOST_MASK = (1 << CS_BIT) | (1 << SCK_BIT) | (1 << MOSI_BIT)


class RtlBench(HostApi):
    def __init__(self, dut, half_period=4):
        self.dut = dut
        self.half = half_period  # SCK half period in clocks (4 = clk / 8)
        self.gpio_devs = []
        self.ui_devs = []
        self.monitors = []
        self.dev_low = 0
        self.ui_drive = 0
        self.spi_bits = 1 << CS_BIT
        self.cycles = 0
        self.wires = 0xFF

    async def start_clock(self):
        cocotb.start_soon(Clock(self.dut.clk, 20, unit="ns").start())
        d = self.dut
        d.ena.value = 1
        d.ui_in.value = self.spi_bits
        d.uio_in.value = 0xFF
        d.rst_n.value = 0
        await ClockCycles(d.clk, 10)
        d.rst_n.value = 1
        cocotb.start_soon(self._pins())

    # ----- wiring (same as ModelBench) -----

    def gpio(self, dev):
        self.gpio_devs.append(dev)
        return dev

    def ui(self, dev, ui_bit):
        self.ui_devs.append((dev, ui_bit))
        self.ui_drive |= 1 << ui_bit
        return dev

    def monitor(self, dev):
        self.monitors.append(dev)
        return dev

    def outputs(self):
        d = self.dut
        uo = int(d.uo_out.value)
        pin_out = int(d.uio_out.value) | (uo & 0x7F) << 8
        return pin_out, int(d.uio_oe.value), (uo >> MISO_BIT) & 1

    async def _pins(self):
        """Every clock: resolve wires, step device models, drive inputs."""
        d = self.dut
        while True:
            await FallingEdge(d.clk)
            pin_out, pin_oe, _ = self.outputs()
            wires, uio, ui = resolve(pin_out, pin_oe, self.dev_low, self.ui_drive)
            self.wires = wires
            low = 0
            for dev in self.gpio_devs:
                low |= dev.step(wires)
            drive = self.ui_drive
            for dev, b in self.ui_devs:
                drive = (drive & ~(1 << b)) | (dev.step(wires) << b)
            for dev in self.monitors:
                dev.step(wires)
            d.uio_in.value = uio
            d.ui_in.value = (ui & ~HOST_MASK) | self.spi_bits
            self.dev_low, self.ui_drive = low & 0xFF, drive & 0xFF
            self.cycles += 1

    # ----- time -----

    async def run(self, n):
        await ClockCycles(self.dut.clk, n, rising=False)

    async def run_until(self, cond, limit=1_000_000):
        for _ in range(limit):
            if cond():
                return
            await FallingEdge(self.dut.clk)
        raise TimeoutError("condition not met")

    # ----- SPI host -----

    def _set(self, bit, v):
        self.spi_bits = (self.spi_bits & ~(1 << bit)) | (int(v) << bit)

    async def _wait(self, n):
        await ClockCycles(self.dut.clk, n, rising=False)

    async def transfer(self, tx):
        """One SPI transaction (mode 0). Returns the bytes read on MISO."""
        rx = []
        self._set(CS_BIT, 0)
        await self._wait(self.half)
        for byte in tx:
            r = 0
            for i in range(7, -1, -1):
                self._set(MOSI_BIT, (byte >> i) & 1)
                self._set(SCK_BIT, 0)
                await self._wait(self.half)
                self._set(SCK_BIT, 1)
                await self._wait(1)  # the new SCK level reaches the pin
                r = (r << 1) | self.outputs()[2]
                await self._wait(self.half - 1)
            rx.append(r)
        self._set(SCK_BIT, 0)
        await self._wait(self.half)
        self._set(CS_BIT, 1)
        await self._wait(self.half)
        return rx

    async def write(self, addr, value):
        await self.transfer([addr & 0x7F, (value >> 8) & 0xFF, value & 0xFF])

    async def write_burst(self, addr, values):
        tx = [addr & 0x7F]
        for v in values:
            tx += [(v >> 8) & 0xFF, v & 0xFF]
        await self.transfer(tx)

    async def read(self, addr, pop=True):
        rx = await self.transfer([0x80 | (addr & 0x7F), 0, 0])
        return rx[1] << 8 | rx[2]

    async def read_burst(self, addr, n):
        rx = await self.transfer([0x80 | (addr & 0x7F)] + [0, 0] * n)
        return [rx[1 + 2 * i] << 8 | rx[2 + 2 * i] for i in range(n)]
