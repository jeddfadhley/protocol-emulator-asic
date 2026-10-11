<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

## How it works

PEMU is a programmable protocol emulator: a tiny processor whose instruction set is built for driving and
sampling pins with cycle-exact timing. Hardware protocols (UART, SPI, I2C, WS2812, SD card commands and
more) are firmware, so new protocols can be added after the chip is made.

- **Two engines** run independent programs from a shared 64-word instruction memory. Each has X/Y
  registers, 16-bit input and output shift registers, 4-word TX and RX FIFOs, a clock divider, and
  side-set pins that change in the same cycle as any instruction.
- **Every instruction takes exactly 1 + delay ticks**, so the timing is set by the program. The core
  properties are proven formally.
- **Beyond RP2040 PIO:** waits with a hardware timeout that traps to a recovery routine (bus hang
  detection, presence detect); a CRC unit for any polynomial up to 16 bits; edge-triggered waits; a
  timestamp counter; relocatable programs; single-step debugging; and an **on-chip loopback mode**, in which
  one engine can test the other with no external hardware.
- **Packet RAM and DMA:** 128 words and one DMA channel per engine, so frames stream at wire speed.
- **USB low-speed sniffer:** records and decodes USB keyboard/mouse traffic (firmware + ring-buffer DMA).
- **Half-clock pin timing:** 10 ns output resolution at 50 MHz.
- **10BASE-T Ethernet in firmware:** the chip can be plugged into a laptop and pinged (see
  docs/ethernet.md). Differential output pairs and any-edge waits make this possible.
- **Host interface:** SPI slave (mode 0). The host loads programs, configures engines and moves data
  through the FIFOs and the packet RAM.

The full ISA and register map are in [docs/isa.md](isa.md) in the project repository.

### Pins

| Pins | Use |
|------|-----|
| `uio[7:0]` | engine pins 0-7, bidirectional (open-drain capable) |
| `uo_out[6:0]` | engine pins 8-14, outputs |
| `ui_in[3:0]`, `ui_in[7]` | engine inputs (pins 8-11, 15) |
| `ui_in[4]`, `ui_in[5]`, `ui_in[6]`, `uo_out[7]` | host SPI: CS_N, SCK, MOSI, MISO |

## How to test

Clock the design at 50 MHz. The layout meets timing at 50 MHz on typical
silicon (1.8 ns slack at 1.2 V, 25 °C); at the worst-case corner (1.08 V,
125 °C) it needs about 30 ns, so use 33 MHz or less if you need that margin.
The host SPI clock must be at most clk / 8.

**Self-test with no external hardware.** Copy `sw/board/pemu_board.py` and `sw/board/pemu_demos.py` from
the repository to the demo board and run:

```python
import pemu_board
pemu_board.selftest()
```

Engine 0 transmits UART frames, and engine 1 receives them through the internal loopback. The test
prints PASSED when every byte comes back intact.

**UART output:** `pemu_board.Pemu(pemu_board.TTBoardSpi()).uart_print("hello\r\n")` sends 115200 8N1 on
`uo_out[4]`, the demo board's UART pin.

**Ethernet self-test (no hardware):** `pemu_board.eth_selftest()` (also copy `sw/pemu/eth.py` to the
board as `pemu_eth.py`). Engine 0 transmits a 10BASE-T frame, and engine 1 receives it through the
loopback.

**Ping the chip:** wire an RJ45 jack with magnetics (TD+/TD- from `uio[0]`/`uio[1]` through 33-47 Ω,
RD+/RD- through a comparator into `ui_in[0]`), give the laptop the address 192.168.7.1/24, run
`chip.eth_start(); chip.eth_serve()`, then `ping 192.168.7.2`.

**Writing your own protocol:** write a program (see `sw/programs/*.pasm`), assemble it with
`python3 -m pemu.asm prog.pasm`, then load the words into instruction memory over SPI.

## External hardware

None for the self-tests. For protocol demos: a USB-serial adapter (UART), a WS2812 LED strip on `uo_out[5]`,
an I2C device on `uio[4]` (SDA) and `uio[5]` (SCL) with pull-up resistors, or for Ethernet an RJ45 jack
with magnetics, two resistors and a comparator.
