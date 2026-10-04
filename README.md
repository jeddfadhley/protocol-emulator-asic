![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg) ![](../../workflows/fpga/badge.svg)

# Protocol Emulator ASIC

An open-source, programmable protocol emulator chip: a tiny processor whose instruction set is built for driving and sampling pins with cycle-exact timing, so hardware protocols (UART, SPI, I2C, and beyond) are implemented in firmware rather than fixed logic.

Built for the [Jane Street protocol emulator ASIC competition](https://blog.janestreet.com/), targeting IHP 130nm CMOS5L via [Tiny Tapeout](https://tinytapeout.com).

> **Status:** early design. The ISA and architecture below are drafts and will change.

---

## Goals

- **Reprogrammable after fabrication.** New protocols are new programs, not new silicon.
- **Cycle-exact timing.** Every instruction has a fixed, documented cycle count.
- **Small.** Fits in a 6×4 Tiny Tapeout allocation (~24 tiles, ~0.7 mm², roughly 24k logic cells).
- **Well verified.** Formal properties on the core, plus protocol-level co-simulation against reference models.
- **Usable.** Ships with an assembler, an instruction-level simulator, and example programs.

### Protocol targets

| Tier | Protocols |
|------|-----------|
| Core | UART, SPI, I2C |
| Stretch | USB low-speed, 10BASE-T Ethernet |
| Nice to have | JTAG, SWD, PS/2, CAN |

---

## Constraints

| Item | Value |
|------|-------|
| Process | IHP SG13G2 130nm (CMOS5L shuttle) |
| Template | Tiny Tapeout CMOS5L Verilog template |
| Area | 6×4 tiles (`info.yaml`), possibly 8×4 later |
| Tile size | ~200 µm × 150 µm, ~1k cells per tile |
| Deadline | **18 January 2027** |
| Target shuttle | March 2027 (subject to foundry schedule) |

---

## Architecture (draft)

```
            ┌──────────────────────────────────────────────┐
  host ───► │  config / load interface (SPI or UART slave) │
            └──────────────┬───────────────────────────────┘
                           │
            ┌──────────────▼───────────────┐
            │  instruction memory (SRAM)   │
            └──────────────┬───────────────┘
                           │
   ┌───────────────────────▼────────────────────────┐
   │  execution engine(s)                           │
   │   • PC + fixed-cycle decoder                   │
   │   • shift registers (in / out)                 │
   │   • scratch registers + loop counters          │
   │   • fractional clock divider                   │
   │   • TX / RX FIFOs                              │
   │   • optional assists: CRC, bit stuffing, edge  │
   │     wait with timeout                          │
   └───────────────────────┬────────────────────────┘
                           │
            ┌──────────────▼───────────────┐
            │  pin mapping / IO mux        │ ◄──► GPIO
            └──────────────────────────────┘
```

Open questions:
- One engine or two? Two allows full duplex and multi-wire protocols but costs area.
- Instruction memory size: SRAM macro versus flip-flops.
- Which hardware assists earn their area (USB and CAN mostly need CRC and bit stuffing).
- How the host loads programs and moves data in and out.

### ISA (draft)

Inspired by RP2040 PIO and TI PRU. Candidate instructions:

| Instruction | Purpose |
|-------------|---------|
| `SET pins, imm` | Drive pins to a value |
| `WAIT pin, level` | Stall until a pin reaches a level (optional timeout) |
| `OUT pins, n` | Shift n bits from the output shift register to pins |
| `IN pins, n` | Shift n bits from pins into the input shift register |
| `PUSH` / `PULL` | Move data between shift registers and FIFOs |
| `JMP cond, addr` | Branch on counter, pin, or shift-register state |
| `MOV dst, src` | Register moves |
| delay field | Every instruction can idle for N extra cycles |

Full encoding: see [`docs/isa.md`](docs/isa.md) (to be written).

---

## Repository layout

```
.
├── src/            # RTL (Verilog), top module matches Tiny Tapeout template
├── test/           # cocotb testbenches and protocol reference models
├── formal/         # SymbiYosys properties
├── sw/
│   ├── asm/        # assembler
│   ├── sim/        # instruction-level simulator (Python)
│   └── programs/   # example protocol programs (uart_tx.s, spi.s, i2c.s, ...)
├── fpga/           # FPGA bring-up wrapper and constraints
├── docs/           # ISA spec, architecture notes, area/timing reports
├── info.yaml       # Tiny Tapeout project config (tiles: 6x4)
└── README.md
```

---

## Getting started

### Prerequisites

- Python 3.11+
- [Icarus Verilog](https://steveicarus.github.io/iverilog/) or [Verilator](https://www.veripool.org/verilator/)
- [cocotb](https://www.cocotb.org/)
- [Yosys](https://yosyshq.net/yosys/) and [SymbiYosys](https://symbiyosys.readthedocs.io/) for formal
- The Tiny Tapeout CMOS5L flow (see the [Tiny Tapeout docs](https://tinytapeout.com))

### Run the tests

```bash
cd test
make            # runs all cocotb tests
make TEST=uart  # run a single protocol test
```

### Run formal checks

```bash
cd formal
sby -f core.sby
```

### Assemble and simulate a program

```bash
python sw/asm/asm.py sw/programs/uart_tx.s -o build/uart_tx.hex
python sw/sim/sim.py build/uart_tx.hex --trace
```

### Harden

Follow the Tiny Tapeout CMOS5L template flow. Check area and timing after every significant change; see `docs/reports/`.

---

## Verification strategy

1. **Instruction-level simulator** is the golden model. RTL is checked against it instruction by instruction.
2. **Formal (SymbiYosys):** fixed cycle counts per instruction, FIFO safety, no deadlock in the decoder, pin outputs only change on documented cycles.
3. **Protocol co-simulation:** real firmware runs on the RTL against reference UART/SPI/I2C models in cocotb, including randomised baud rates, clock modes and bus contention.
4. **Constrained random:** random programs run on both RTL and simulator, outputs compared.
5. **FPGA:** run on hardware against real peripherals before tapeout.

---

## Roadmap

| Target | Milestone |
|--------|-----------|
| Mid Oct | Template flow builds; hard-coded UART TX out of a pin |
| End Oct | ISA v1 frozen; Python simulator and assembler |
| Mid Nov | RTL engine passing UART/SPI tests in simulation |
| End Nov | I2C working; formal properties in place; first area report |
| Mid Dec | FPGA bring-up; stretch protocol attempts (USB LS) |
| Early Jan | Area and timing closure; documentation |
| **18 Jan 2027** | **Submit** |

---

## Working with Claude Code

This repo is developed with Claude Code. Guidelines for agents and humans alike:

- Keep the simulator, assembler and RTL in sync. An ISA change means updating all three plus `docs/isa.md` in the same change.
- Every RTL change needs a passing test run. Never mark work done with failing tests.
- Run synthesis after any change that adds state or logic and record cell count in `docs/reports/`.
- Prefer small, reviewable commits.
- Don't hand-edit generated files (GDS, netlists, `build/`).

Project-specific instructions for Claude Code live in [`CLAUDE.md`](CLAUDE.md).

---

## License

Apache 2.0 (as required: the submission must be open source).
