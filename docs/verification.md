# Verification

PEMU is verified in layers. Each layer catches a different kind of mistake,
and every layer runs in CI on each push (`.github/workflows/test.yaml`).

```
            docs/isa.md (spec)
                  │
     ┌────────────┴────────────┐
 sw/pemu/model.py          src/*.v (RTL)
 (Python reference)            │
     │   ◄──── lockstep ────►  │      every clock, random + directed programs
     │                         │
 protocol scenarios ───────────┤      same firmware, same device models,
 (model)                       │      RTL driven through its real SPI pins
                               │
                     formal proofs (BMC + k-induction)
                     mutation testing (25 planted bugs)
                     gate-level re-run of the chip tests (CI)
```

## 1. Reference model

`sw/pemu/model.py` is a cycle-accurate model of the core, written from
[isa.md](isa.md) independently of the RTL. It is the oracle for everything
else.

## 2. Lockstep co-simulation (`test/test_cosim.py`)

The RTL core and the model run side by side. Every clock, the test compares
pin outputs, bus read data, event flags and **all architectural state of both
engines**: PC, delay, X, Y, ISR/OSR and their counts, CRC, timers, FIFO
levels and sticky flags.

- **Constrained random:** each seed fills instruction memory with random
  instructions (three opcode mixes: mixed, data-path heavy, control heavy),
  randomises all configuration, toggles input pins and issues random host
  traffic. That traffic includes FIFO pushes and pops, aborted reads, and
  CTRL pulses for restart, step and flush. Every 16-bit word is a legal
  instruction, so random programs exercise the whole encoding space,
  reserved encodings included.
- **Directed sweep:** small loops cover every IN/OUT bit count
  (1, 2, 3, 5, 8, 15, 16) against every push/pull threshold, for both
  autopush/autopull and the explicit `iffull`/`ifempty` forms.
- CI runs 150 random seeds x 3000 clocks plus the directed sweep, about
  600,000 compared clocks. Local runs of 200+ seeds have passed.

```
cd test && make TB=core COSIM_SEEDS=200 COSIM_CYCLES=3000
```

## 3. Protocol scenarios (`sw/pemu/scenarios.py`)

Real firmware from `sw/programs/` runs against independent device models
(`sw/pemu/periph.py`): a UART terminal, a UART source with framing errors, an
SPI flash-like slave, an I2C EEPROM with clock stretching and a hang mode,
WS2812 pulse decoding, and a USB low-speed decoder. The decoder implements
NRZI, bit stuffing, SYNC, PID and CRC5/CRC16 itself and checks that every line
state lasts an exact multiple of the bit time.

Each scenario runs twice with identical code:

- on the Python model (`cd sw && python -m pytest tests`), and
- on the RTL through the chip's real SPI host pins (`cd test && make`), so
  the SPI slave, register map and FIFO handshakes are covered too. The
  Tiny Tapeout CI re-runs these on the gate-level netlist.

| Scenario | Checks |
|----------|--------|
| `uart_tx`, `uart_tx_timing` | message content; every bit exactly 32 clocks |
| `uart_rx` | bytes received; framing error flagged and discarded |
| `uart_loopback` | engine 0 to engine 1 on-chip, both engines concurrently |
| `spi` | full-duplex mode-0 transfer against a slave model |
| `sd_cmd` | CRC7 from the CRC unit matches SD CMD0/CMD8 (0x95, 0x87) |
| `i2c` | write, then repeated-START read, ACK/NAK, clock stretching, missing device |
| `i2c_timeout` | SCL held low: trap fires, bus error flagged, recovery afterwards |
| `ws2812` | 24 bits with exact 10-tick periods and 2/7-tick high times |
| `edge_stamp` | edge timestamps differ by exactly the input pulse widths |
| `single_step` | debug stepping with PC visible after every tick |
| `usb_ls_tx` | SETUP, DATA0 (GET_DESCRIPTOR), ACK, stuffed DATA1 at exactly 1.5 Mbit/s |

## 4. Formal proofs (`formal/run.sh`)

The properties live in `ifdef FORMAL` blocks in `src/pe_engine.v` and
`src/pe_fifo.v`. They are checked with Yosys and yosys-smtbmc (Yices) by
bounded model checking from reset **and k-induction**, so they hold in every
reachable state, not just the first 16 cycles.

Engine:
- shift counts stay within 0-16 and FIFO levels within 0-4;
- the engine never pushes a full FIFO or pops an empty one;
- between ticks nothing moves and nothing leaves the engine (no pin, flag
  or FIFO activity);
- delay ticks are inert: only the delay counter and TIME change;
- **cycle exactness:** a completing instruction loads exactly its delay
  field, so it occupies `1 + delay` ticks;
- a stalled instruction holds its PC and starts no delay;
- only a `WAIT ... timeout` can trap, and a trap lands on `TRAP` with
  `TIMED_OUT` set;
- TIME counts ticks exactly, and the clock divider spaces ticks
  `CLKDIV + 1` clocks apart.

FIFO:
- data comes out in the order it went in. One arbitrary word is tracked
  through the FIFO, with an induction-strengthening invariant on storage;
- the level matches the pointer difference.

Inside the engine the FIFO's "never overflow" contract is **proven**, not
assumed. It is assumed only when the FIFO is checked on its own.

## 5. Mutation testing (`test/mutate.py`)

Mutation testing asks whether the tests would notice a bug. The script
plants 25 realistic bugs, one at a time, and reruns the relevant tests. The
bugs include an off-by-one in a threshold, a missing decrement, a wrong CRC
tap, a missing synchronizer stage, broken engine priority, and an SPI pop on
the wrong clock edge. **All 25 are caught.**

The first run caught 24. The survivor, a PUSH `iffull` off by one, led to the
directed threshold sweep in section 2.

## 6. Physical implementation

The Tiny Tapeout flow (LibreLane/OpenROAD on IHP SG13CMOS5L) hardens the
design in a 6x4-tile slot, and CI re-runs the chip tests on the routed
gate-level netlist. Results:

| Metric | Value |
|--------|-------|
| DRC, LVS, antenna, TT precheck | clean |
| Gate-level chip tests | pass |
| Standard cells (placed) | 21,878 cells, 0.31 mm² (34% utilisation) |
| Flip-flops | about 2,000 (1,990 clock sinks) |
| Setup slack at 50 MHz, typical (1.20 V, 25 °C) | +1.77 ns |
| Setup slack at 50 MHz, fast (1.32 V, -40 °C) | +8.06 ns |
| Setup slack at 50 MHz, slow (1.08 V, 125 °C) | -9.43 ns, so about 33 MHz worst case |
| Hold slack (all corners) | +0.12 ns |
| Estimated power | 7.5 mW |

The flow optimises timing at the typical corner, as is usual for Tiny
Tapeout, so 50 MHz is the design's typical rating. All protocol firmware
except the USB demo (which needs 48 MHz for exact 1.5 Mbit/s) runs at any
clock by recomputing `CLKDIV`.

## Bugs found by this process

- **SPI read popped a word on the trailing clock edge.** In SPI mode 0, SCK
  always falls once more after the last bit, before CS_N rises. The first SPI
  slave treated that edge as the start of the next word and popped the RX
  FIFO, silently losing a byte per read. The SPI-level UART RX scenario found
  it, and the design changed: a FIFO word is now consumed only when its 16th
  bit has been clocked out, and only if the FIFO was non-empty when the word
  started. Aborted reads lose nothing.
- **Shift-count overflow.** `count + n` can reach 32, which does not fit in 5
  bits. Found in model/RTL review; the co-simulation and the formal bounds now
  cover it.
- **TIME read semantics.** The model and RTL disagreed on whether an
  instruction sees TIME before or after its own tick. The spec now says
  before, and both implementations follow it.

## Running everything locally

```
cd sw && python -m pytest -q tests          # model, assembler, USB, board driver
cd test && make TB=core                      # lockstep co-simulation
cd test && make                              # chip-level scenarios over SPI
formal/run.sh                                # formal proofs (needs yices)
python3 test/mutate.py                       # mutation testing (~10 minutes)
```

Note: cocotb's Makefile cannot handle a space in the project path. Run from a
path without spaces, for example through a symlink.
