# SPDX-License-Identifier: Apache-2.0
"""FPGA wrapper tests: a PC's view of PEMU through the UART-to-SPI bridge.

The test plays the PC: it drives pemu_fpga_top's UART RX line and decodes its
UART TX line, using the same byte protocol as sw/pemu/fpga.py. The last tests
run the unmodified MicroPython board driver (sw/board/pemu_board.py) through
the bridge, including the on-chip UART loopback self-test.
"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.queue import Queue
from cocotb.task import bridge, resume
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge, with_timeout

import pemu_board

BIT = 8                   # clocks per UART bit (BAUD = CLK_HZ / 8 in tb_fpga.v)
TIMEOUT_CLKS = 20_000     # bridge transaction timeout in tb_fpga.v
A_X0 = 0x49               # engine 0 X register
A_PIN_OUT, A_PIN_OE, A_ID = 0x63, 0x64, 0x65


class Pc:
    """The host side of the serial link."""

    def __init__(self, dut):
        self.dut = dut
        self.rx = Queue()
        self.gap = 0          # idle clocks between transmitted bytes

    async def start(self):
        dut = self.dut
        dut.uart_rx.value = 1
        dut.ui_ext.value = 0xFF
        dut.rst_n.value = 0
        cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
        await ClockCycles(dut.clk, 10)
        dut.rst_n.value = 1
        cocotb.start_soon(self._receiver())
        await ClockCycles(dut.clk, 400)   # power-on reset

    async def _receiver(self):
        dut = self.dut
        while True:
            await FallingEdge(dut.uart_tx)
            await ClockCycles(dut.clk, BIT + BIT // 2)
            b = 0
            for i in range(8):
                b |= int(dut.uart_tx.value) << i
                await ClockCycles(dut.clk, BIT)
            assert int(dut.uart_tx.value) == 1, "UART TX framing error"
            await self.rx.put(b)

    async def send(self, data):
        dut = self.dut
        for byte in data:
            for bit in [0] + [(byte >> i) & 1 for i in range(8)] + [1]:
                dut.uart_rx.value = bit
                await ClockCycles(dut.clk, BIT)
            if self.gap:
                await ClockCycles(dut.clk, self.gap)

    async def recv(self, n, timeout_us=2000):
        out = bytearray()
        for _ in range(n):
            out.append(await with_timeout(self.rx.get(), timeout_us, "us"))
        return bytes(out)

    async def transfer(self, data):
        """One SPI transaction: 'S', length, bytes; returns the MISO bytes."""
        data = bytes(data)
        sender = cocotb.start_soon(self.send(b"S" + len(data).to_bytes(2, "big") + data))
        rx = await self.recv(len(data), timeout_us=2000 + 10 * len(data) * (1 + self.gap // 50))
        await sender
        return rx

    async def write(self, addr, *words):
        await self.transfer(bytes([addr & 0x7F]) + b"".join(w.to_bytes(2, "big") for w in words))

    async def read(self, addr, n=1):
        rx = await self.transfer(bytes([0x80 | addr]) + bytes(2 * n))
        return [int.from_bytes(rx[1 + 2 * i:3 + 2 * i], "big") for i in range(n)]

    async def command(self, cmd, n_reply):
        await self.send(cmd)
        return await self.recv(n_reply)


async def pc(dut):
    p = Pc(dut)
    await p.start()
    return p


async def cs_watch(dut, log):
    """Record every CS_N rising edge (end of an SPI transaction)."""
    while True:
        await RisingEdge(dut.spi_cs_n)
        log.append(1)


@cocotb.test()
async def test_identify(dut):
    """'I' replies "PEMU"."""
    p = await pc(dut)
    assert await p.command(b"I", 4) == b"PEMU"


@cocotb.test()
async def test_chip_id(dut):
    """An 'S' transaction reading register 0x65 returns the chip ID 0x5045."""
    p = await pc(dut)
    rx = await p.transfer(bytes([0x80 | A_ID, 0, 0]))
    assert rx[1:] == b"\x50\x45", rx.hex()
    assert int(dut.spi_cs_n.value) == 1


@cocotb.test()
async def test_registers(dut):
    """Write and read back X, PIN_OUT/PIN_OE (seen on the pins) and IMEM."""
    p = await pc(dut)
    await p.write(A_X0, 0xBEEF)
    assert await p.read(A_X0) == [0xBEEF]
    await p.write(A_PIN_OUT, 0x5AA5, 0x00FF)     # burst: PIN_OUT then PIN_OE
    assert await p.read(A_PIN_OUT, 2) == [0x5AA5, 0x00FF]
    assert int(dut.uio_oe.value) == 0xFF
    assert int(dut.uio_out.value) == 0xA5
    assert int(dut.uo_out.value) & 0x7F == 0x5A & 0x7F
    rng = random.Random(1)
    words = [rng.randrange(1 << 16) for _ in range(64)]
    await p.write(0, *words)                      # 129-byte transaction
    assert await p.read(0, 64) == words


@cocotb.test()
async def test_slow_uart_keeps_cs_low(dut):
    """CS_N stays low between bytes when the PC is much slower than SPI."""
    p = await pc(dut)
    ends = []
    cocotb.start_soon(cs_watch(dut, ends))
    p.gap = 500                                   # >> one SPI byte (80 clocks)
    await p.write(A_X0, 0x1234)
    assert await p.read(A_X0) == [0x1234]
    assert len(ends) == 2, f"expected 2 transactions, saw {len(ends)}"


@cocotb.test()
async def test_reset(dut):
    """'R' resets PEMU (X back to 0) and replies 'R'."""
    p = await pc(dut)
    await p.write(A_X0, 0x7777)
    assert await p.read(A_X0) == [0x7777]
    assert await p.command(b"R", 1) == b"R"
    assert await p.read(A_X0) == [0]
    assert await p.read(A_ID) == [0x5045]


@cocotb.test()
async def test_noise_and_timeout(dut):
    """Unknown bytes are ignored; an abandoned 'S' times out and frees the bridge."""
    p = await pc(dut)
    await p.send(b"\x00\xffxyz\n")
    assert await p.command(b"I", 4) == b"PEMU"
    # Promise 3 bytes, send 1, then go quiet.
    await p.send(b"S\x00\x03" + bytes([0x80 | A_ID]))
    assert await p.recv(1) == b"\x00"
    await ClockCycles(dut.clk, TIMEOUT_CLKS + 200)
    assert int(dut.spi_cs_n.value) == 1
    assert await p.command(b"I", 4) == b"PEMU"
    assert await p.read(A_ID) == [0x5045]


class BridgeLink:
    """transfer() for the blocking board driver, run in a cocotb bridge thread."""

    def __init__(self, p):
        self.transfer = resume(p.transfer)


@cocotb.test()
async def test_board_driver_selftest(dut):
    """pemu_board.selftest(): on-chip UART loopback, driven over the bridge."""
    p = await pc(dut)
    ok = await bridge(pemu_board.selftest)(BridgeLink(p))
    assert ok


@cocotb.test()
async def test_board_driver_eth_selftest(dut):
    """pemu_board.eth_selftest(): a 10BASE-T frame through the on-chip loopback,
    with the frame loaded into the packet RAM over the bridge."""
    p = await pc(dut)
    ok = await bridge(pemu_board.eth_selftest)(BridgeLink(p))
    assert ok


async def uart_decode(sig, clk, bit_clks, n):
    """Decode n 8N1 bytes from a PEMU output pin."""
    high = 0                      # wait for the line to idle high first
    while high < 2 * bit_clks:
        await RisingEdge(clk)
        high = high + 1 if int(sig.value) == 1 else 0
    out = bytearray()
    for _ in range(n):
        while int(sig.value) == 1:
            await RisingEdge(clk)
        await ClockCycles(clk, bit_clks + bit_clks // 2)
        b = 0
        for i in range(8):
            b |= (int(sig.value) & 1) << i
            await ClockCycles(clk, bit_clks)
        out.append(b)
    return bytes(out)


@cocotb.test()
async def test_uart_print(dut):
    """Pemu.uart_print() sends 115200 8N1 out of PEMU pin 12 (uo_out[4], Pmod JB7)."""
    p = await pc(dut)
    chip = pemu_board.Pemu(BridgeLink(p))

    class Pin:
        @property
        def value(self):
            return (int(dut.uo_out.value) >> 4) & 1

    decoder = cocotb.start_soon(uart_decode(Pin(), dut.clk, 8 * (53 + 1), 3))
    await bridge(chip.uart_print)("Hi!")
    got = await with_timeout(decoder, 2, "ms")
    assert got == b"Hi!", got
