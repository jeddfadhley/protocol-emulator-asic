# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge

CLK_PERIOD_NS = 20  # 50 MHz
CLKS_PER_BIT = 434  # 115200 baud
TX_BIT = 4  # uo_out[4]
MESSAGE = b"Hello, world!\r\n"


def tx(dut):
    return (int(dut.uo_out.value) >> TX_BIT) & 1


async def reset(dut):
    clock = Clock(dut.clk, CLK_PERIOD_NS, unit="ns")
    cocotb.start_soon(clock.start())
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1


async def uart_receive_byte(dut):
    """Wait for a start bit, then sample each bit at its centre."""
    while tx(dut) != 0:
        await FallingEdge(dut.clk)

    await ClockCycles(dut.clk, CLKS_PER_BIT // 2, rising=False)
    assert tx(dut) == 0, "start bit not low at centre"

    value = 0
    for i in range(8):
        await ClockCycles(dut.clk, CLKS_PER_BIT, rising=False)
        value |= tx(dut) << i

    await ClockCycles(dut.clk, CLKS_PER_BIT, rising=False)
    assert tx(dut) == 1, "stop bit not high (framing error)"
    return value


@cocotb.test()
async def test_idle_high_in_reset(dut):
    clock = Clock(dut.clk, CLK_PERIOD_NS, unit="ns")
    cocotb.start_soon(clock.start())
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 5)
    assert tx(dut) == 1
    assert int(dut.uo_out.value) & ~(1 << TX_BIT) == 0, "unused outputs must be 0"
    assert int(dut.uio_oe.value) == 0


@cocotb.test()
async def test_uart_message(dut):
    await reset(dut)

    # Receive twice the message length so we check it repeats correctly.
    received = bytearray()
    for _ in range(2 * len(MESSAGE)):
        received.append(await uart_receive_byte(dut))

    dut._log.info(f"Received: {bytes(received)!r}")
    assert bytes(received) == MESSAGE * 2


@cocotb.test()
async def test_uart_bit_period(dut):
    """Bit timing is exact: the first low run of "H" lasts exactly 4 bit periods."""
    await reset(dut)

    # First char is 'H' = 0x48 = 0b01001000, sent LSB first after the start bit:
    # start(0) 0 0 0 1 0 0 1 0 stop(1). Low run from start bit through bit 2 is 4 bits.
    await FallingEdge(dut.clk)
    while tx(dut) != 0:
        await FallingEdge(dut.clk)
    cycles = 0
    while tx(dut) == 0:
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
        cycles += 1
    assert cycles == 4 * CLKS_PER_BIT, f"low run was {cycles} cycles"
