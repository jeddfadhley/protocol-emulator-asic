# Verification

PEMU is verified in layers. Each layer catches a different kind of mistake,
and every layer runs in CI on each push: `.github/workflows/test.yaml` for the
simulations, proofs and mutation testing, `gds.yaml` for the layout and the
gate-level re-run. The timed gate-level simulation (section 6) is run
locally with `test/sdf.mk`.

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
                     mutation testing (59 planted bugs)
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

- **Constrained random:** each seed fills the instruction memories with random
  instructions (the same program in both, or a different one in each) (three opcode mixes: mixed, data-path heavy, control heavy),
  randomises all configuration, toggles input pins and issues random host
  traffic. That traffic includes FIFO pushes and pops, aborted reads, CTRL
  pulses for restart, step and flush, packet RAM reads and writes, and DMA
  transfers in both directions on both channels. The packet RAM contents
  are compared too. Every 16-bit word is a legal
  instruction, so random programs exercise the whole encoding space,
  reserved fields included, and jumps of both opcodes reach all 64 words.
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
- **On the FPGA:** `python3 -m pemu.fpga lockstep` (`sw/pemu/hwcheck.py`)
  does the same comparison against a Basys 3 board. The engines are
  single-stepped with `CTRL.STEP` in loopback, so the result does not depend
  on USB timing or on what is plugged in, and engine registers, pins and
  flags are read back after every tick. v4 passed 18,000 ticks (120 program
  pairs over three seeds, including the skip/wake/add programs above).

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
| `program_memory` | a 64-word program: far and near jumps across the halves, a trap into the upper half, PC bit 5 in STATUS; loading engine 1 leaves engine 0's memory alone; IMEM reads as 0 |
| `usb_ls_tx` | SETUP, DATA0 (GET_DESCRIPTOR), ACK, stuffed DATA1 at 1.515 Mbit/s (+1.0%; USB allows ±1.5%) |
| `eth_loopback` | a 10BASE-T frame from engine 0 to engine 1 on-chip, FCS intact |
| `eth_tx` | pins sampled twice per clock: every half bit exactly 50 ns, preamble, TP_IDL, link pulses |
| `eth_rx`, `eth_rx_drift` | frames from an independent clock: phase, +100 ppm, jitter, link pulses, oversize frame |
| `eth_ping` | a laptop model sends ARP and ping; the host code answers through the chip |
| `usb_ls_rx`, `_slow`, `_fast` | a USB device model (its own encoder, ±1.2% clock, keep-alives) sends 7 packets; the sniffer's ring buffer decodes them all |
| `usb_loopback` | engine 0 sends USB, engine 1 sniffs it on-chip |

The USB receiver also has a tolerance test (`sw/tests/test_usb.py`, on the
model): it decodes every packet with the sender's clock off by up to ±4%, at
six phases across a bit (USB allows ±1.5%), and the same test confirms that
it does lose packets at ±8%.

The Ethernet receiver also has a tolerance test: every sender clock phase,
±2000 ppm, ±4 ns of jitter on every edge, all frames must arrive intact
([ethernet.md](ethernet.md)).

## 4. Formal proofs (`formal/run.sh`)

The properties live in `ifdef FORMAL` blocks in `src/pe_engine.v`,
`src/pe_fifo.v` and `src/pe_dma.v`. They are checked with Yosys and yosys-smtbmc (Yices) by
bounded model checking from reset **and k-induction**, so they hold in every
reachable state, not just the first 16-20 cycles (16 in CI, 20 by default).

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
plants 59 realistic bugs, one at a time, and reruns the relevant tests. The
bugs include an off-by-one in a threshold, a missing decrement, a wrong CRC
tap, a missing synchronizer stage, broken engine priority, and an SPI pop on
the wrong clock edge (25 in all), plus 13 for v2: DMA priority and
address bugs, a ring buffer that runs out, RAM port bugs, a differential pin
that is not inverted, an any-edge wait compared with the wrong level; 7 for
the timing work (late pins that are not late, a mis-wired STEP pulse, a
fetch address that misses a restart...); and three for v3 (a TX transfer that takes the packet RAM's read port while the host
is reading, a latch-memory word that opens half a clock early, and a
packet RAM read that misses a write still waiting to be stored); and 11
for v4 (x<y computed as x<=y, negation ignored, the wrong flag tested, a
skip that never cancels or a cancelled instruction that still runs, a wake
that never wakes or stays armed, add that overwrites, an ignored reset
strap...). **All 59 are caught.**

The first v4 run caught 58: `x<y` computed as `x<=y` survived, because
random X and Y are almost never equal. A directed co-simulation program
that compares X and Y at equality and one apart now catches it.

One latch-memory bug cannot be caught this way: closing a word on the
rising edge instead of the falling edge. In a zero-delay simulation the
latch closes and the next data arrives at the same instant and the
simulator resolves it correctly, so every test passes. On silicon it would
be a race. The design avoids it by construction (the latches close half a
clock before the data can change), and the layout flow's timing checks
cover the rest.

The first run caught 24. The survivor, a PUSH `iffull` off by one, led to the
directed threshold sweep in section 2. For v2, the first run caught 10 of
the 12 bugs planted at the time. The survivors (an any-edge wait compared with the polarity bit, and a
differential pin whose direction was not mirrored) led to the directed
feature programs.

## 6. Physical implementation

The Tiny Tapeout flow (LibreLane 3.1 / OpenROAD on IHP SG13CMOS5L) hardens
the design in an 8x4-tile slot, and CI re-runs the chip tests on the routed
gate-level netlist. Results for v4 (commit `4a14c97`, CI run 44773207355):

| Metric | Value |
|--------|-------|
| DRC, LVS, antenna, TT precheck | clean (0 errors) |
| Gate-level chip tests | pass |
| Standard cells (placed, excluding fill) | 29,128 cells, 0.46 mm² (38% of the 1.21 mm² core) |
| Storage | 4,096 memory latches and 1,643 flip-flops |
| Setup slack at 50 MHz, typical (1.20 V, 25 °C) | +2.55 ns |
| Setup slack at 50 MHz, fast (1.32 V, -40 °C) | +3.76 ns |
| Setup slack at 50 MHz, slow (1.08 V, 125 °C) | +0.48 ns: 50 MHz at every corner |
| Hold slack (worst corner, fast) | +0.10 ns |
| Estimated power | 10 mW |
| Layout run time on CI | 2 h 52 min (the limit is 6 h) |

For comparison, v3 had 29,260 cells and reached +1.84 ns typical and
-0.79 ns (about 48 MHz) at the slow corner; v2 in the smaller 6x4 slot used
0.59 mm² of cells and 21 mW, and reached -7.86 ns (about 36 MHz) at the slow
corner. v4 adds about 14 flip-flops and some logic, yet came out slightly
smaller and faster: placement and routing differ from run to run, and this
run's worst paths happened to come out shorter. So the slow-corner margin is
small (+0.48 ns, 2% of the clock period), not a design guarantee.

The flow repairs timing at the typical corner, as is usual for Tiny
Tapeout, and checks setup at the typical corner only; the v4 layout also
meets 50 MHz at the slow corner, by the small margin above. All protocol firmware
runs at any clock by recomputing `CLKDIV`; USB low speed needs a clock that
gives about 1.5 Mbit/s (50 MHz gives 1.515 Mbit/s, within USB's ±1.5%).

### Timed gate-level simulation (`test/sdf.mk`)

CI's gate-level test runs the routed netlist with zero delays, so it cannot
see a race: two signals that change "at the same time" are resolved by the
simulator's event order. The latch memories close on one clock edge while
their data changes on the other, so a race there is exactly what we wanted
to rule out. So all 50 chip tests were also run on the routed v4 netlist
with the layout's own delays (SDF) loaded into every cell:

| Corner | Clock | Result |
|---|---|---|
| typical (1.20 V, 25 °C) | 50 MHz | 50 / 50 pass |
| fast (1.32 V, -40 °C) | 50 MHz | 50 / 50 pass |
| slow (1.08 V, 125 °C) | 50 MHz | 50 / 50 pass (also at 45 MHz) |

The first v4 run found one failure at every corner, in the test bench rather
than the chip: the reset-strap test released reset and the strap on the same
clock edge. The strap is read on the last clock of reset, and in the real
layout reset reaches the flip-flops later than `ui_in[7]` does, so the chip
saw "still in reset, strap off". Like any strap pin, `ui_in[7]` must be held
for a couple of clocks after reset is released; the test now does that, and
the rule is in [isa.md](isa.md) and [info.md](info.md). The zero-delay
gate-level test in CI could not see this.

Checks that the setup really applies delays: an output pin changes 2.01 ns
after the clock edge at the typical corner and 1.32 ns at the fast one (zero
without delays), and at the slow corner the program-memory test fails when
the clock is pushed to 8 ns.

What this does not cover: Icarus applies the cells' delays but not the
wires' (about 0.1 ns each here), and does not run setup and hold checks, and
the tests only exercise the paths they happen to use. The same test still
passes at the slow corner with a 14 ns clock, where timing analysis says
about 21 ns is needed. So this simulation finds races and gross timing
errors; the speed figures come from the layout flow's timing analysis
above, which checks every path.

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
- The fetch address (ORIGIN + PC) is registered, computed a clock early
  from the next PC, so the 6-bit add no longer sits in front of the
  instruction fetch. A formal assertion proves the register always equals
  ORIGIN + PC. This came from the v2 layout, where the worst typical-corner
  paths ran PC → add → fetch → execute → pin registers (-1.37 ns); the PC
  itself had 3.4 ns to spare.
- Two layout settings: OpenROAD now runs on 4 threads (`OPENROAD_THREADS`;
  left unset, LibreLane 3.1.0.dev3 fell back to one thread and routing ran
  past CI's 6-hour limit), and the all-corner resizer (`RSZ_CORNERS`) was
  dropped for the same reason.
- **Wiring, not cells, limits this design.** On the 8x4 slot, the v2 layout
  ran past CI's 6 hours twice. The log showed why: SG13CMOS5L gives signals
  only three metal layers (Metal2-4), and the one horizontal layer, Metal3,
  was asked for 95% of its capacity. Global routing spent 90 minutes and
  detailed routing four hours resolving the overflow. The biggest wiring
  load was the memories' read multiplexers: the instruction memory had three
  64-to-1 read ports (two fetches and the host) and the packet RAM two
  128-to-1 ports (DMA and host). v3 removes the host's read port from the
  instruction memory and gives each engine its own 64-word memory with one
  fetch port, and the packet RAM shares one read port between the DMA and
  the host (a TX transfer waits a clock when the host reads). In a generic
  Yosys synthesis the design shrinks from 30,766 to 25,648 cells while the
  program memory doubles.
- **v3 still ran out of time, by about half an hour.** Metal3 demand fell
  from 95% to 85%, and detailed routing did finish with zero violations, but
  it took 5 h 22 min (53 rounds, the last 30 spent on fewer than 100
  violations), and the six hours ran out in the antenna-repair pass after
  it. Synthesis with the real cell library showed where the area was: of
  476,000 µm² of cells, 253,000 were flip-flops, and 4,096 of the 5,157
  flip-flops were memory bits, each with its own hold-value logic.
- **So the memories became latches** (`pe_ram` in `src/pe_core.v`). A latch
  with reset (`dlhrq_1`, 27 µm²) is about half a flip-flop (`dfrbpq_1`,
  49 µm²) and needs no hold logic, because it only opens when written. Each
  word's latches open while two flags are set: one set on the falling edge
  after the write request, the other its copy on the next rising edge. So a
  word is open for the first half of the clock after the request and closes
  half a clock before its data register can change. The gate is an AND of
  two flip-flops that change half a clock apart, so it cannot glitch, and
  no clock passes through logic (the method of Michael Bell's TT06 latch
  RAM, with a flag pair per word so the RX DMA can write in back-to-back
  clocks). Reads see a write on the next clock, exactly as before, so the
  spec, the model and every test are unchanged. The cell count falls from
  27,800 to 20,400 and the area from 476,000 to 347,000 µm² (IHP cell
  library, local Yosys). The FPGA build keeps plain arrays, which map to
  LUT RAM.
- The co-simulation needed one change for this: it used to drive the bus
  exactly on the falling edge, the instant the latch memories capture a
  request, and the simulator then saw the old request. It now drives the
  bus 1 ns earlier. On the chip the bus comes from rising-edge registers and
  has been steady for half a clock by then.
- **The first latch layout routed in 2 h 38 min, inside the limit, with
  DRC, LVS and antenna clean**, but stopped on timing: LibreLane 3.1 fails
  a run on a setup violation at any corner, where v2's flow checked only
  the typical one. Typical was +1.31 ns and fast +3.04 ns. The slow corner
  (1.08 V, 125 °C) was -3.64 ns, better than v2's -7.86 ns, and its worst
  path was new: the packet RAM's word flags are set on the falling edge, so
  the DMA's choice of channel, the address and its decode had half a clock.
  That write request is now registered first and stored a clock later, and
  until then a read of that word is answered from the register, so reads
  still see a write on the next clock. The flow now signs off setup timing
  at the typical corner again (`TIMING_VIOLATION_CORNERS` in
  `src/config.json`); hold is checked at all corners. With both changes
  the layout passed in 1 h 45 min, and the slow corner came out at
  -0.79 ns (about 48 MHz), the closest it has been (section 6).

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
# each line from the repository root
(cd sw && python -m pytest -q tests)        # model, assembler, USB, board driver
(cd test && make TB=core)                   # lockstep co-simulation
(cd test && make)                           # chip-level scenarios over SPI
formal/run.sh                               # formal proofs (needs yices)
python3 test/mutate.py                      # mutation testing (~40 minutes)
```

Note: cocotb's Makefile cannot handle a space in the project path. Run from a
path without spaces, for example through a symlink.
