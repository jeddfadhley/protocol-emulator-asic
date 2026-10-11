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
                     mutation testing (43 planted bugs)
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
  traffic. That traffic includes FIFO pushes and pops, aborted reads, CTRL
  pulses for restart, step and flush, packet RAM reads and writes, and DMA
  transfers in both directions on both channels. The packet RAM contents
  are compared too. Every 16-bit word is a legal
  instruction, so random programs exercise the whole encoding space,
  reserved encodings included.
- **Pads in both clock halves:** the co-simulation compares the pins the
  chip actually drives in the first and the second half of every clock, so
  half-clock (`late`) writes are checked against the model too.
- **Directed sweep:** small loops cover every IN/OUT bit count
  (1, 2, 3, 5, 8, 15, 16) against every push/pull threshold, for both
  autopush/autopull and the explicit `iffull`/`ifempty` forms.
- **Feature programs:** edge and any-edge waits on fast-toggling pins, and
  differential pairs written by every path (SET, MOV, OUT, side-set, values
  and directions).
- CI runs 150 random seeds x 3000 clocks plus the directed programs, about
  700,000 compared clocks. Local runs of 200+ seeds have passed.

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
| `eth_loopback` | a 10BASE-T frame from engine 0 to engine 1 on-chip, FCS intact |
| `eth_tx` | pins sampled twice per clock: every half bit exactly 50 ns, preamble, TP_IDL, link pulses |
| `eth_rx`, `eth_rx_drift` | frames from an independent clock: phase, +100 ppm, jitter, link pulses, oversize frame |
| `eth_ping` | a laptop model sends ARP and ping; the host code answers through the chip |
| `usb_ls_rx`, `_slow`, `_fast` | a USB device model (its own encoder, ±1.2% clock, keep-alives) sends 7 packets; the sniffer's ring buffer decodes them all |
| `usb_loopback` | engine 0 sends USB, engine 1 sniffs it on-chip |

The USB receiver decodes every packet at every phase with the sender's
clock off by up to ±2% (USB allows ±1.5%).

The Ethernet receiver also has a tolerance test: every sender clock phase,
±2000 ppm, ±4 ns of jitter on every edge, all frames must arrive intact
([ethernet.md](ethernet.md)).

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
  `CLKDIV + 1` clocks apart;
- the divider compare that is computed a clock early (for timing) always
  equals `divcnt >= clkdiv`;
- late-pin flags only ever mark pins that are being written.

DMA (`pe_dma`):
- it never pushes a full FIFO or pops an empty one, and never collides with
  the host on the same FIFO or the RAM write port;
- at most one transfer per clock, channel 0 first, and each transfer moves
  its channel by exactly one word.

Inside the engine these DMA guarantees are assumptions, so the engine's own
FIFO safety proof still covers every source of pushes and pops.

FIFO:
- data comes out in the order it went in. One arbitrary word is tracked
  through the FIFO, with an induction-strengthening invariant on storage;
- the level matches the pointer difference.

Inside the engine the FIFO's "never overflow" contract is **proven**, not
assumed. It is assumed only when the FIFO is checked on its own.

## 5. Mutation testing (`test/mutate.py`)

Mutation testing asks whether the tests would notice a bug. The script
plants 43 realistic bugs, one at a time, and reruns the relevant tests. The
bugs include an off-by-one in a threshold, a missing decrement, a wrong CRC
tap, a missing synchronizer stage, broken engine priority, and an SPI pop on
the wrong clock edge, plus 12 for v2: DMA priority and address bugs, RAM
port bugs, a differential pin that is not inverted, an any-edge wait
compared with the wrong level, and 6 for the timing work (late pins that are
not late, a mis-wired STEP pulse, a ring buffer that runs out...). **All 43
are caught.**

The first run caught 24. The survivor, a PUSH `iffull` off by one, led to the
directed threshold sweep in section 2. For v2, the first run caught 10 of
12. The survivors (an any-edge wait compared with the polarity bit, and a
differential pin whose direction was not mirrored) led to the directed
feature programs.

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

## 7. Timing closure

The first v1 layout met 50 MHz on typical silicon but not on the slow
corner (1.08 V, 125 °C: -9.4 ns). The report showed why: the worst paths
started at the host bus address register and ran through the CTRL decode
(the STEP and RESTART pulses) into every engine's next-state logic, and
the timing repair was only run at the typical corner. The fixes:

- CTRL pulses act on the clock after the write, so the host bus no longer
  reaches the engines combinationally (spec updated).
- The clock divider compare is computed a clock early, with a formal proof
  that it always equals the original.
- The resizer now optimises the slow, typical and fast corners
  (`RSZ_CORNERS` in `src/config.json`).

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
- **Ethernet receiver timing.** The jitter sweep found that the first
  `eth_rx` lost about 1 frame in 30 at ±2 ns of jitter. Two hunting paths
  re-waited 4 clocks after an edge instead of 3, so the receiver could slip
  onto bit-boundary transitions just as the SFD arrived. Every path now
  re-waits after exactly 3 clocks.
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
