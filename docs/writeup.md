# PEMU: write-up

*Entry for the Jane Street protocol emulator ASIC competition. This page is
the overview: what PEMU is, why it is built this way, how we know it works,
and what it cannot do. Each section links to the detailed document.*

## 1. The problem

A protocol emulator has to speak whatever wire protocol it is pointed at:
UART today, a sensor's one-off bus tomorrow, Ethernet the day after. Fixed
silicon for each protocol cannot keep up, and a CPU bit-banging pins cannot
meet the timing, because its interrupts and caches make every edge a little
late, by a different amount each time.

PEMU's answer is to make **timing the one thing the hardware guarantees**,
and leave everything else to firmware.

## 2. The design

Two small engines run programs from their own 64-word memories. Every
instruction takes exactly `1 + delay` ticks, with no exceptions, stalls only
where the program asks to wait, and outputs that can move to the falling
clock edge for 10 ns resolution at 50 MHz. That rule is proven formally, not
just tested. Around the engines:

- **Shift registers, side-set and FIFOs**, after the RP2040's PIO, so a
  serial protocol's inner loop is two to four instructions.
- **Wait timeouts with a trap.** `wait 1 pin 1 timeout` jumps to a handler if
  the line never moves. A hung I2C bus, a missing sensor or an idle line is
  handled on the chip, without host polling.
- **A CRC unit** fed bit by bit, so CRCs (SD card, USB) are added in real time.
- **Edge waits and a timestamp counter**: a logic analyser in four
  instructions.
- **A packet RAM with DMA**, so a whole 10 Mbit/s Ethernet frame streams
  without waiting for the slow host link.
- **Differential outputs and any-edge waits**, which is how about 30
  instructions of firmware speak 10BASE-T Ethernet with no Ethernet hardware
  on the chip.

A host (the Tiny Tapeout board's RP2040, or a PC) loads programs and moves
data over SPI. Protocol layers that need arithmetic (Ethernet's CRC-32, ARP,
IP) run on the host; the chip does everything that needs exact timing.

Details: [README](../README.md) (overview and comparison with PIO and TI's
PRU), [isa.md](isa.md) (the spec), [programming.md](programming.md) (how to
write a protocol), [ethernet.md](ethernet.md).

## 3. Does it generalise? A blind test

The competition is about flexibility, so we tested it directly. We froze the
hardware, then added five protocols that played no part in its design:
1-Wire, NEC infrared remotes, a PS/2 keyboard host, ARM's Serial Wire Debug
and CAN transmit with arbitration. **All five fitted without changing the
hardware.** Four needed only ordinary firmware; CAN transmit fitted, but a
full CAN receiver does not, because it needs counting the engines cannot do.
That gap, and three others, became a short list of the next hardware
changes, and one of them (more program memory) is now built.
Details: [new-protocols.md](new-protocols.md).

Firmware included today: UART, SPI, I2C, WS2812 LEDs, SD-card commands, edge
timestamping, USB low-speed send and sniff, 10BASE-T Ethernet (ping and UDP),
and the five above.

## 4. How we know it works

Correctness rests on checks anyone can re-run, not on review.

| Check | What it shows |
|---|---|
| **Lockstep co-simulation** | The RTL and a separate Python model agree on every register, pin and packet RAM word, every clock, over random programs covering the whole instruction encoding (about 700,000 compared clocks per CI run). |
| **Protocol scenarios** | 40 tests run real firmware against device models that enforce each protocol's own timing rules, through the chip's real SPI pins, on the RTL and again on the routed gate-level netlist. |
| **Formal proofs** | Cycle exactness, FIFO safety and ordering, trap behaviour, DMA rules: proven for all inputs (bounded model checking plus induction). |
| **Mutation testing** | 48 realistic bugs planted one at a time; the tests catch all 48. This checks the tests themselves. |
| **Timed gate-level simulation** | All 40 tests also pass on the routed netlist with its real gate delays, at all three process corners. This catches races that zero-delay simulation cannot. |
| **Real hardware** | The same RTL on a Basys 3 FPGA board passes the UART, USB and Ethernet self-tests. |
| **Layout sign-off** | Tiny Tapeout CI: DRC, LVS, antenna and precheck clean. |

Details: [verification.md](verification.md), including the bugs these checks
found.

## 5. The chip

IHP 130 nm (SG13CMOS5L) through Tiny Tapeout, 8x4 tiles.

| | |
|---|---|
| Logic | 29,260 cells, 0.46 mm², 38% of the area |
| Memory | 4,096 latch bits (two 64-word program memories, a 128-word packet RAM) |
| Speed | 50 MHz on typical silicon (+1.84 ns slack); about 48 MHz at the worst corner (1.08 V, 125 °C) |
| Power | about 10 mW |

The layout was the hardest engineering problem. This process routes signals
on only three metal layers, and the first 8x4 layouts ran out of GitHub's
6-hour limit while still wiring. The router's reports showed the memories'
wiring was the cause. Two redesigns fixed it: each engine got its own
program memory with one read port, and all memories became latch arrays,
half the size of flip-flops. The final layout takes 1 h 45 min.
Details: [verification.md, sections 6 and 7](verification.md#6-physical-implementation).

## 6. Limitations

- **No arithmetic unit.** Firmware branches on bits; counting and comparing
  happen on the host. This is why a CAN receiver does not fit.
- **USB** is low speed only, and the chip sends and sniffs packets but cannot
  answer a USB host within the bus turnaround time, so it does not enumerate
  as a device.
- **Ethernet** is half duplex, with frames up to 124 bytes received and 117
  sent.
- **50 MHz is a typical-silicon rating.** At the worst corner, use 45 MHz or
  less.
- **Not yet tried with outside equipment:** a laptop's Ethernet port, or the
  sensors and transceivers for the five newer protocols. The board driver for
  the Tiny Tapeout demo board has run only against the model.

## 7. How it was built

The project owner directed the work and ran the hardware; an AI (Claude,
through Claude Code) wrote the RTL, software, tests and documents. The
account of who did what, how the AI's output was checked, and where it was
wrong is in [ai-usage.md](ai-usage.md).

## 8. Try it

```bash
pip install -r test/requirements.txt    # cocotb, pytest; needs Icarus Verilog
cd sw && python -m pytest -q tests      # model, assembler, firmware
cd ../test && make                      # the 40 chip tests on the RTL
make TB=core                            # RTL against the model, every clock
../formal/run.sh                        # formal proofs (Yosys + Yices)
```

Or run it on a Basys 3 FPGA board: [fpga.md](fpga.md).
