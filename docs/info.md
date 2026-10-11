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
- **Host interface:** SPI slave (mode 0). The host loads programs, configures engines and moves data
  through the FIFOs.

The full ISA and register map are in [docs/isa.md](isa.md) in the project repository.

### Pins

| Pins | Use |
|------|-----|
| `uio[7:0]` | engine pins 0-7, bidirectional (open-drain capable) |
| `uo_out[6:0]` | engine pins 8-14, outputs |
| `ui_in[3:0]`, `ui_in[7]` | engine inputs (pins 8-11, 15) |
| `ui_in[4]`, `ui_in[5]`, `ui_in[6]`, `uo_out[7]` | host SPI: CS_N, SCK, MOSI, MISO |

## How to test

Clock the design at 50 MHz. The host SPI clock must be at most clk / 8.

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

**Writing your own protocol:** write a program (see `sw/programs/*.pasm`), assemble it with
`python3 -m pemu.asm prog.pasm`, then load the words into instruction memory over SPI.

## External hardware

None for the self-test. For protocol demos: a USB-serial adapter (UART), a WS2812 LED strip on `uo_out[5]`,
or an I2C device on `uio[4]` (SDA) and `uio[5]` (SCL) with pull-up resistors.
