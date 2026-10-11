# Running PEMU on a Basys 3 FPGA board

This guide puts the PEMU chip design onto a **Digilent Basys 3** FPGA board,
so you can use it before (or without) real silicon. It is the same design that
goes on the chip, unchanged; the FPGA just adds a small adapter so your Mac can
talk to it over the board's USB cable.

```
  Mac ──USB cable──► Basys 3: USB-serial ──► UART-to-SPI bridge ──► PEMU (the chip design)
                                                                     │
                                         Pmod headers JA, JB, JC ◄───┘  (PEMU's pins)
```

No programming is needed to follow this guide: you copy and paste a few
commands.

## 1. What you need

- A **Digilent Basys 3** board (Artix-7 xc7a35t).
- A **micro-USB cable** (data, not charge-only). It powers the board and
  carries both the programming link and the serial link.
- A Mac (Linux works too) with Python 3.

Optional, for experiments:

- Jumper wires (female-to-female for the Pmod headers).
- A USB-serial adapter (any 3.3 V one) to see PEMU's UART output.
- For Ethernet: an **RJ45 MagJack breakout** (an RJ45 socket with built-in
  transformer, e.g. a HanRun HR911105A breakout board), two 47 Ω resistors,
  two 49.9 Ω (or 51 Ω) resistors, two 470 Ω resistors, a 2.2 kΩ and a 1.5 kΩ
  resistor and a 100 nF capacitor. See [section 7](#7-ethernet-wiring).

## 2. Install the tools (once)

Open the **Terminal** app and paste:

```bash
brew install openfpgaloader     # loads designs onto the FPGA (needs Homebrew: https://brew.sh)
pip3 install pyserial           # lets Python talk to the board's serial port
```

You also need a copy of this repository (on GitHub: **Code → Download ZIP**,
then unzip it, or `git clone`).

## 3. Get the FPGA design file (the "bitstream")

The design file is called `pemu_basys3.bit`. GitHub builds it automatically
with open-source tools every time the code changes.

1. On the repository's GitHub page, click **Actions**, then the **basys3**
   workflow on the left.
2. Click the most recent run with a green tick.
3. Under **Artifacts** at the bottom, download **pemu_basys3**. It is a zip
   file; unzip it to get `pemu_basys3.bit`.

(With the GitHub command-line tool this is one command:
`gh run download --repo jeddfadhley/protocol-emulator-asic -n pemu_basys3`.)

**Alternatively, build it with Vivado** (Xilinx's free tools, Windows or
Linux only): in `fpga/basys3/` run
`vivado -mode batch -source build_vivado.tcl`. Vivado also checks the design's
timing exactly, which the open-source tools can only estimate.

## 4. Load the design onto the board

1. Plug the board into your Mac with the micro-USB cable (the port marked
   PROG/UART) and switch it on (the power switch next to the USB port).
2. In Terminal, go to the folder with `pemu_basys3.bit` and run:

```bash
openFPGALoader -b basys3 pemu_basys3.bit
```

This loads the design into the FPGA's working memory. It takes about a
second, and is forgotten when the board is switched off.

To make it permanent, write it to the board's flash memory instead:

```bash
openFPGALoader -b basys3 -f pemu_basys3.bit
```

Then move the programming-mode jumper **JP1** to **QSPI**, and the board
loads PEMU by itself at every power-up.

When the design is running, LED 15 (far left) blinks whenever the Mac talks
to PEMU, and LEDs 0-7 show the level of PEMU pins 0-7 on header JA. With
nothing plugged in those eight LEDs are lit, because the pins have pull-up
resistors. The centre button (**btnC**) resets PEMU.

## 5. Talk to PEMU

In Terminal, go to the `sw` folder of the repository:

```bash
cd path/to/repository/sw
python3 -m pemu.fpga check
```

You should see `PEMU found: chip ID 0x5045`. Then run the self-test:

```bash
python3 -m pemu.fpga selftest
```

It prints `PEMU self-test PASSED`: one of PEMU's two engines sends serial
(UART) data, the other receives it through an internal loopback, and the bytes
come back intact. No wiring is needed.

Other commands:

| Command | What it does |
|---------|--------------|
| `python3 -m pemu.fpga ports` | lists serial ports; the Basys 3 one is marked |
| `python3 -m pemu.fpga check` | reads PEMU's chip ID |
| `python3 -m pemu.fpga selftest` | on-chip UART loopback test |
| `python3 -m pemu.fpga uart "Hello\r\n"` | PEMU sends the text as 115200-baud serial out of **Pmod JB7** (PEMU pin 12). Connect JB7 to a USB-serial adapter's RX and a GND pin (JB5 or JB11) to its GND to see it. |
| `python3 -m pemu.fpga peek 0x65` | reads a PEMU register (`peek ADDR COUNT` reads several) |
| `python3 -m pemu.fpga poke 0x63 0x0055` | writes a register. This example sets PIN_OUT; follow it with `poke 0x64 0x00ff` (PIN_OE: pins 0-7 become outputs) and LEDs 1, 3, 5 and 7 go dark. |

The register map is in [isa.md](isa.md#register-map).

Ethernet commands (`python3 -m pemu.fpga eth-...`) are being added separately
and will be described here.

If a command says the board was not found, check the cable and the power
switch, run `python3 -m pemu.fpga ports`, and pass the port explicitly, e.g.
`python3 -m pemu.fpga --port /dev/tty.usbserial-210183A27BA71 check`. The
Basys 3 shows up as two ports; PEMU is on the one ending in **1**.

## 6. Pin map

Pmod pin numbers: each header has two rows of six pins. Pins 1-6 are one row
(pin 1 is marked on the board) and pins 7-12 the other, with 7 next to 1.
Pins 5 and 11 are GND, pins 6 and 12 are 3.3 V.

| PEMU pin | Chip signal | Basys 3 | FPGA pin | Notes |
|----------|-------------|---------|----------|-------|
| 0 | uio[0] | JA1 | J1 | bidirectional, pull-up; Ethernet TD+ |
| 1 | uio[1] | JA2 | L2 | bidirectional, pull-up; Ethernet TD- |
| 2 | uio[2] | JA3 | J2 | bidirectional, pull-up |
| 3 | uio[3] | JA4 | G2 | bidirectional, pull-up |
| 4 | uio[4] | JA7 | H1 | bidirectional, pull-up; I2C SDA in the demos |
| 5 | uio[5] | JA8 | K2 | bidirectional, pull-up; I2C SCL in the demos |
| 6 | uio[6] | JA9 | H2 | bidirectional, pull-up |
| 7 | uio[7] | JA10 | G3 | bidirectional, pull-up |
| 8 (out) | uo_out[0] | JB1 | A14 | output |
| 9 (out) | uo_out[1] | JB2 | A16 | output |
| 10 (out) | uo_out[2] | JB3 | B15 | output |
| 11 (out) | uo_out[3] | JB4 | B16 | output |
| 12 (out) | uo_out[4] | JB7 | A15 | output; UART TX in the demos |
| 13 (out) | uo_out[5] | JB8 | A17 | output; WS2812 data in the demos |
| 14 (out) | uo_out[6] | JB9 | C15 | output |
| 8 (in) | ui_in[0] | JXADC1 / JXADC7 | J3 / K3 | differential input (XA1_P / XA1_N); Ethernet RD+ / RD- |
| 9 (in) | ui_in[1] | JC1 | K17 | input, pull-up |
| 10 (in) | ui_in[2] | JC2 | M18 | input, pull-up |
| 11 (in) | ui_in[3] | JC3 | N17 | input, pull-up |
| 15 (in) | ui_in[7] | JC4 | P18 | input, pull-up |
| host CS_N, SCK, MOSI, MISO | ui_in[4..6], uo_out[7] | inside the FPGA | | driven by the UART bridge |
| | | RsRx / RsTx | B18 / A18 | USB serial, 1,000,000 baud |
| | | LED 0-7 | | actual levels of PEMU pins 0-7 |
| | | LED 8-14 | | PEMU output pins 8-14 |
| | | LED 15 | | serial activity |
| | | btnC | U18 | reset |

PEMU pins 8-11 exist both as an output (JB) and as an input (JXADC/JC), as on
the chip. All signals are 3.3 V. **The Basys 3 has a 200 Ω resistor in series
with every JA, JB and JC pin** (protection; see the board schematic), but not
on JXADC.

## 7. Ethernet wiring

PEMU can produce 10BASE-T Ethernet signals in firmware: the transmit pair comes
from PEMU pins 0 and 1 (JA1, JA2), and the receive pair goes into the
differential input on JXADC. An Ethernet "MagJack" contains the isolation
transformer that every Ethernet port needs, so you only add a few resistors.

### Transmit (FPGA to cable)

```
  Basys 3 Pmod JA                                    RJ45 MagJack breakout
  (each pin already has 200 Ω on the board)

  JA1  (PEMU pin 0) ──────[ 47 Ω ]──────────────────  TD+
  JA2  (PEMU pin 1) ──────[ 47 Ω ]──────────────────  TD-
                                                      TCT (TX centre tap) ── 100 nF ── GND
                                                                             (or leave unconnected)
  JA5  (GND) ───────────────────────────────────────  GND
```

The FPGA drives TD+ and TD- in opposite directions (the firmware does this),
with the strongest, fastest output setting (DRIVE 16, SLEW FAST). The 47 Ω
resistors are the usual recipe for driving a MagJack from logic pins. On the
Basys 3 the on-board 200 Ω resistors already do this job, so the 47 Ω
resistors are optional: leaving them out gives a slightly bigger signal.

**Expect a weak signal.** With roughly 250 Ω in series with each wire and the
cable's 100 Ω load, the signal is about 0.6 V instead of the 2.2-2.8 V the
standard asks for. Most network cards and switches still detect it over a
short cable (their threshold is typically 0.3-0.6 V), but this has not been
tried yet with this design: use a short cable straight to a PC or switch.

### Receive (cable to FPGA)

```
  RJ45 MagJack breakout                          Basys 3 Pmod JXADC

  RD+ ───┬───────────────────[ 470 Ω ]───────── JXADC1  (XA1_P, FPGA pin J3)
         │
      [ 49.9 Ω ]
         │
         ├──────────── bias point (~1.3 V)
         │                  │
      [ 49.9 Ω ]          100 nF
         │                  │
  RD- ───┴──────────────────┼─[ 470 Ω ]──────── JXADC7  (XA1_N, FPGA pin K3)
                            │
                           GND

  bias point:  JXADC6 (3.3 V) ──[ 2.2 kΩ ]──┬──[ 1.5 kΩ ]── JXADC5 (GND)
                                             └── bias point
```

What each part does:

- **2 x 49.9 Ω (total 100 Ω)** terminates the cable properly. FPGA's own
  internal termination cannot be used in this bank (it runs at 3.3 V).
- **The bias point** sets the middle voltage of the pair to about 1.3 V,
  where the FPGA's differential receiver works (the transformer's output
  floats otherwise). The 2.2 kΩ / 1.5 kΩ divider makes the voltage; the
  100 nF capacitor keeps it steady.
- **470 Ω in series** with each wire protects the FPGA pins. The inputs draw
  almost no current, so these do not reduce the signal.

The FPGA input is configured as `LVDS_25` with internal termination off,
which Xilinx allows in a 3.3 V bank. A 10BASE-T signal (up to about ±2.5 V
between the wires, so about 0.1-2.6 V on each pin with this bias) is larger
than LVDS but stays inside the pins' safe range.

## 8. How it works (for the curious)

The bridge inside the FPGA (`fpga/rtl/uart_spi_bridge.v`) turns serial
commands from the Mac into SPI transactions on PEMU's host port, exactly as a
microcontroller would on the real chip:

| Mac sends | Bridge does | Reply |
|-----------|-------------|-------|
| `S`, length (2 bytes, high first), then that many bytes | one SPI transaction: CS low, each byte exchanged, CS high | the bytes read back |
| `R` | resets PEMU | `R` |
| `I` | nothing | `PEMU` |

PEMU runs at 50 MHz (a PLL makes it from the board's 100 MHz clock), the SPI
clock is 5 MHz, and the serial link runs at 1,000,000 baud. If the Mac stops
half way through a transaction, the bridge gives up after half a second.

Files:

| File | Purpose |
|------|---------|
| `fpga/rtl/uart_spi_bridge.v` | UART-to-SPI bridge (portable Verilog) |
| `fpga/rtl/pemu_fpga_top.v` | PEMU + bridge + power-on reset (portable) |
| `fpga/basys3/pemu_basys3.v` | Basys 3 top: clock, I/O buffers, LEDs |
| `fpga/basys3/basys3.xdc` | pin constraints |
| `fpga/basys3/Makefile` | open-source build (Yosys, nextpnr-xilinx, Project X-Ray) |
| `fpga/basys3/build_vivado.tcl` | Vivado build |
| `fpga/test/` | simulation test: UART in, PEMU out, including the self-test |
| `sw/pemu/fpga.py` | the `python3 -m pemu.fpga` tool |

The open-source build runs in GitHub Actions in the `regymm/openxc7` Docker
image. To run it yourself with Docker:

```bash
docker run --rm -v "$PWD":/work -w /work/fpga/basys3 regymm/openxc7 make
```

## 9. What has been tested

- In simulation (`fpga/test`, run in CI): the bridge, the chip ID, register
  and instruction-memory read/write, reset, recovery from an abandoned
  transaction, the self-test and UART output, all through the serial line.
- The `pemu.fpga` tool, against the Python model of the chip (`sw/tests`).
- The open-source tools build a bitstream in CI, and their timing estimate
  says the design runs at about 56 MHz, above the 50 MHz it needs (the
  Vivado script also stops if timing is not met).
- **Not yet tested on a real board**: everything that depends on hardware
  (the serial port, the PLL, the pins, Ethernet levels). Please report what
  you find.
