![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg)

# PEMU: a programmable protocol emulator ASIC

PEMU is an open-source chip that speaks hardware protocols in firmware. Two
small engines run programs whose instructions drive and sample pins with
**cycle-exact, formally proven timing**. UART, SPI, I2C, WS2812, SD card
commands and USB low-speed are just programs, and new protocols need no new
silicon.

Built for the [Jane Street protocol emulator ASIC competition](https://blog.janestreet.com/protocol-emulator-asic-competition/)
on IHP 130 nm CMOS5L through [Tiny Tapeout](https://tinytapeout.com), in a
6x4-tile allocation.

## Highlights

| | |
|---|---|
| **Engines** | 2, each with X/Y, 16-bit in/out shift registers, 4-word TX/RX FIFOs, clock divider, side-set, CRC unit, timestamp counter |
| **Program memory** | 64 x 16-bit words, shared; programs are relocatable (up to 32 instructions each) |
| **Timing** | every instruction takes exactly `1 + delay` ticks; tick = `CLKDIV + 1` clocks at 50 MHz (typical silicon; see [timing](docs/verification.md#6-physical-implementation)) |
| **Pins** | 8 bidirectional (open-drain capable), 7 outputs, 5 inputs, all with 2-flop synchronizers |
| **Host link** | SPI slave: load programs, configure, stream data, debug |
| **Firmware included** | UART TX/RX, SPI master, I2C master, WS2812, SD CMD + CRC7, edge timestamping, USB low-speed TX |

### What is new compared with RP2040 PIO and TI PRU

PIO inspired the shift-register and side-set model. PEMU adds features aimed
at robust protocols and at being verifiable:

- **Wait timeouts with a trap vector.** `wait 1 pin 1 timeout` jumps to a
  handler if the condition never comes, so a hung I2C bus, a missing 1-Wire
  device or an idle line is handled in firmware without host polling. PIO
  waits forever.
- **CRC unit.** A left-justified Galois LFSR with any polynomial up to 16
  bits (CRC-5/7/15/16) is fed by `IN`/`OUT` bit by bit. The SD-card example
  appends CRC7 in real time.
- **Edge waits and a timestamp counter.** `wait 1 edge n` and `in time, 16`
  make a logic analyser or pulse measurer in four instructions.
- **On-chip loopback.** One engine can drive a protocol while the other
  receives it, with no external wiring. The demo-board self-test uses this.
- **Debuggable.** Single-step, readable PC/X/Y/ISR/OSR/status, sticky
  overflow and timeout flags.
- **Lossless host reads.** A FIFO word is only consumed once all 16 bits
  have reached the host. The verification flow found the bug that made this
  necessary.
- **Small.** Two 16-bit engines in 0.31 mm² of placed cells, 34% of the
  6x4-tile slot. PRU is a
  full 32-bit RISC core.

## Architecture

```
   host (e.g. RP2040)
        │ SPI: CS_N/SCK/MOSI on ui_in[6:4], MISO on uo_out[7]
  ┌─────▼──────┐  register bus  ┌──────────────────────────────────────────┐
  │  pe_spi    ├───────────────►│ pe_core                                  │
  └────────────┘                │  instruction memory (64 x 16)            │
                                │  ┌───────────────┐   ┌───────────────┐   │
                                │  │ pe_engine 0   │   │ pe_engine 1   │   │
                                │  │ PC X Y ISR OSR│   │ PC X Y ISR OSR│   │
                                │  │ CRC TIME FIFOs│   │ CRC TIME FIFOs│   │
                                │  └──────┬────────┘   └──────┬────────┘   │
                                │      pin registers, event flags,         │
                                │      synchronizers, loopback             │
                                └──────────────┬───────────────────────────┘
                                   uio[7:0], uo_out[6:0], ui_in
```

## Documentation

- [docs/isa.md](docs/isa.md): ISA, timing rules and register map (the spec)
- [docs/programming.md](docs/programming.md): how to write a protocol
- [docs/verification.md](docs/verification.md): verification strategy and results
- [docs/info.md](docs/info.md): Tiny Tapeout datasheet page

## Verification at a glance

- **Lockstep co-simulation** of the RTL against an independent Python
  reference model, comparing every architectural register every clock:
  constrained-random programs (the whole 16-bit encoding space) plus a
  directed sweep of shift-count/threshold boundaries. About 600k compared
  clocks per CI run.
- **Protocol scenarios.** The same firmware and device models run on the
  model and on the RTL through its SPI pins, and then on the gate-level
  netlist in CI.
- **Formal proofs** (BMC + k-induction) of cycle exactness, FIFO safety and
  ordering, trap behaviour, and that nothing moves between ticks.
- **Mutation testing.** All 25 deliberately planted RTL bugs are caught.
- **Layout signed off** in Tiny Tapeout CI: DRC, LVS, antenna and precheck
  clean, with the chip tests re-run on the gate-level netlist.

## Repository layout

```
src/            RTL: project.v (top), pe_spi.v, pe_core.v, pe_engine.v, pe_fifo.v
test/           cocotb: chip-level tests (test.py), co-simulation (test_cosim.py), mutate.py
formal/         run.sh: Yosys + yosys-smtbmc proofs
sw/pemu/        Python: assembler, cycle-accurate model, device models, USB coder, scenarios
sw/programs/    protocol firmware (*.pasm)
sw/board/       MicroPython driver and demos for the Tiny Tapeout demo board
sw/tests/       pytest suite for the Python side
docs/           spec, programming guide, verification report, datasheet
```

## Quick start

```bash
pip install -r test/requirements.txt         # cocotb, pytest; needs Icarus Verilog
cd sw && python -m pytest -q tests           # model + assembler + firmware
python -m pemu.asm programs/uart_tx.pasm     # assemble a program
cd ../test && make                           # chip-level tests on the RTL
make TB=core                                 # RTL vs model co-simulation
../formal/run.sh                             # formal proofs (yosys + yices)
```

On the demo board, copy `sw/board/pemu_board.py` and `sw/board/pemu_demos.py`
to the RP2040 and run `import pemu_board; pemu_board.selftest()`.

## Limitations and future work

- USB low-speed **receive** and 10BASE-T need a sample stream faster than the
  host can drain over SPI. Hardware NRZI decoding, de-stuffing and deeper
  FIFOs would make this possible within the area budget.
- No ALU: arithmetic is done on the host, and firmware branches on bits with
  `out x, 1` / `jmp !x`.
- The MicroPython board driver is tested against the model but has not yet
  run on silicon or an FPGA.

## License

Apache 2.0.
