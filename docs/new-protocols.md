# Protocols added after the hardware freeze

The competition's main question is flexibility: can the chip be reprogrammed
for protocols it was not designed for? To test that honestly, we froze the
hardware (the RTL as of commit `f8aba2f`, the 8x4 layout) and then added five
protocols that played no part in the design, using only firmware, host code
and test models. No RTL file changed. This page records what fitted, what
needed a workaround, and what did not fit.

| Protocol | Program | Instructions | Uses | Host's share |
|----------|---------|-------------:|------|--------------|
| 1-Wire (DS18B20 sensors) | [`onewire.pasm`](../sw/programs/onewire.pasm) | 18 | open drain, µs timing | CRC-8 check |
| NEC infrared remotes | [`nec_ir.pasm`](../sw/programs/nec_ir.pasm) | 21 | wait timeout + trap, `jmp pin` | none |
| PS/2 keyboard, both directions | [`ps2_host.pasm`](../sw/programs/ps2_host.pasm) | 29 | device-driven clock, timeout + trap, `pull noblock` | parity |
| ARM Serial Wire Debug | [`swd.pasm`](../sw/programs/swd.pasm) | 13 | side-set clock, autopush/autopull, turnaround | packets, parity ([`swdhost.py`](../sw/pemu/swdhost.py)) |
| CAN 2.0A transmit with arbitration | [`can_tx.pasm`](../sw/programs/can_tx.pasm) | 21 | bus read-back, timeout as idle detector, packet RAM + DMA, event flag | CRC-15, bit stuffing ([`can.py`](../sw/pemu/can.py)) |

Every program fits in one engine's 32-instruction half of the program memory,
so any two of them can run at once.

## How they are tested

Each protocol has a device model that behaves like the real part and checks
the protocol's timing rules: a DS18B20 with ROM codes and a scratchpad, an IR
receiver module fed by a remote, a PS/2 keyboard that answers commands, an
ARM debug port with a memory port, and CAN nodes that destuff, check the CRC,
acknowledge and arbitrate. They are in
[`sw/pemu/periph_ext.py`](../sw/pemu/periph_ext.py) and the scenarios in
[`sw/pemu/scenarios_ext.py`](../sw/pemu/scenarios_ext.py).

The scenarios run on the Python model (`cd sw && python -m pytest tests`)
and on the RTL through the chip's SPI host port (`cd test && make`), like the
original protocols, and CI also runs them on the gate-level netlist.

To check that the tests can fail, we broke each program on purpose (wrong
pulse lengths, a sample point moved, arbitration ignored, a bit time 4% off,
the timeout disabled) and confirmed the scenarios caught every one.

Running on the RTL found two bugs the Python model had missed. That was not
because the RTL behaves differently (the two are checked against each other
cycle by cycle). On the RTL every register access goes over SPI and takes
about 250 clocks, so the host is as slow as a real one, and devices kept
talking while the chip was being set up. Both bugs are described below, and
each now has a scenario that fails on the old firmware.

| Scenario | What it shows |
|----------|---------------|
| `onewire` | READ ROM, SKIP ROM + CONVERT T, MATCH ROM + READ SCRATCHPAD, temperature and both CRC-8s, a ROM code no device matches |
| `onewire_absent` | the presence bit on an empty bus |
| `nec_ir`, `nec_ir_tolerance`, `nec_ir_fast` | frames and repeat codes, noise ignored, a cut-off frame dropped by the timeout trap; remote clock 8% slow and fast |
| `nec_ir_midframe` | the receiver starts in the middle of a frame: no false leader or repeat |
| `ps2` | scan codes in; set LEDs and identify out, each acknowledged |
| `ps2_busy` | a command queued mid-frame waits for the frame to finish; nothing lost |
| `ps2_glitch` | a keyboard unplugged mid-byte: the timeout drops the half frame |
| `swd` | line reset, IDCODE, power-up handshake, AP IDR, memory write and read back through the MEM-AP |
| `swd_errors` | no answer before IDCODE is read; a bad-parity write makes later requests FAULT until ABORT clears it |
| `can_tx` | three frames at 1 Mbit/s, acknowledged by another node, including one full of stuff bits |
| `can_arbitration` | a higher-priority node starts at the same moment: the chip backs off at once, the winner's frame is intact, the retry succeeds |
| `can_busy` | the bus is busy when the chip gets a frame: it waits for the other frame and 11 idle bits |

## What we learned, protocol by protocol

**1-Wire fitted directly.** A read slot is a write-1 slot whose level the
master samples, so one 6-instruction slot loop both reads and writes. The
host writes `0xFF` to read a byte.

**NEC infrared needed a different way to measure time.** There is no timer
compare, so the firmware samples the line 1.4 units after each burst ends: low
means the next burst has started (a 0), high means a long space (a 1).

- The first version went back to wait for the next burst after sampling a 0.
  With the remote's clock 8% fast, that burst had already ended, so a bit was
  lost. Branching on the sampled level instead fixed it.
- Repeat codes are now found by polling for a burst over 3 units, not by one
  sample in a 1-unit window.
- Found on the RTL: the leader check sampled the line once, 4.6 ms after a
  burst started. A chip that starts in the middle of a frame can land on a
  data burst that happens to be low at that moment. It then took data for a
  leader and reported repeat codes that never happened. Checking the line
  continuously for the whole 4.5 ms fixed it, with no extra instructions.
- The decoder works with the remote's clock anywhere from 12% fast to 12% slow
  (measured; real remotes are within a few percent).
- The wait timeout drops a frame that stops halfway. Without it, the next
  frame decodes as garbage (we checked by turning it off).

**PS/2 fitted, with polling.** The engine cannot wait for "the keyboard
starts a frame *or* the host has data", so the idle loop polls both: `jmp pin`
on the clock, and `pull noblock` with X = 0 as the "nothing to send" value.
The loop takes 4 µs, well inside the keyboard's 30-50 µs clock-low time.

- First bug: after the 11th bit the firmware went back to idle while the
  clock was still low and took that as a new frame. Waiting for the clock to
  rise fixed it.
- Because sends start only between frames, the host never interrupts the
  keyboard.

**SWD became a general shifter.** The firmware only clocks N bits out or in
and releases the data line for reads. The host builds requests, turnarounds,
ACKs and parity from those shifts, so the same 13 instructions also drive
3-wire SPI and similar buses.

- First bug: reads were sampled half a clock after the target's edge. That is
  too soon once the chip's 2-flop input synchronizer is counted. Sampling on
  the next rising edge, a full period later (as the Raspberry Pi debug probe
  does), fixed it.
- SWCLK runs at 6.25 MHz (CLKDIV = 1), or 12.5 MHz in simulation.

**CAN fitted only partly.** This is where the missing ALU shows.

- **Transmit works.** The host adds the CRC and the stuff bits and puts the
  frame in the packet RAM. DMA feeds the engine. The firmware keeps the bit
  timing, reads the bus back at the sample point, and on a lost arbitration
  stops driving within the same bit, raises a flag and keeps sampling.
- Found on the RTL: the first version started its frame at once, even with
  another node mid-frame. CAN requires waiting for 11 recessive bits. The fix
  reuses the wait timeout as an idle detector: `wait 0 pin 0 timeout` with
  TIMEOUT = 11 bits traps only when the bus has been quiet that long, and the
  trap handler is the start of the frame. Any dominant bit restarts the wait.
- **A full receiver does not fit.** A receiver must drive the ACK slot, whose
  position depends on the data length and on how many stuff bits arrived.
  Finding it needs counting and comparing in real time, which the engines
  cannot do, and the host is too far away to answer within one bit. Raw
  capture with host-side decoding (as the USB sniffer does) would work for
  listen-only use, but we have not written it.
- **A hardware finding:** the dedicated outputs (`uo_out`) reset to 0. On a
  CAN transceiver's TXD that means "dominant", which would block the bus from
  power-up until the chip is configured. The workaround is to put TXD on a
  bidirectional pin, which floats at reset (the transceiver's pull-up keeps
  the bus recessive), and to enable the output only after setting it high.

## What this says about the hardware

Four of the five protocols fitted with no workaround beyond ordinary firmware
design. That supports the main claim: the engines are general enough for
protocols nobody planned for. The gaps are specific, and they point to the
next hardware changes:

1. **A small add/compare unit** would let firmware count stuff bits and
   field lengths (a CAN receiver), check parity on the chip, and compare
   times.
2. **Waiting on either of two conditions** would remove the PS/2 polling
   loop and cut its reaction time to one tick.
3. **A configurable reset level for `uo_out`** would make every output pin
   safe for buses that must idle high.
4. **More program memory**: the PS/2 host already uses 29 of the 32 words one
   engine can have.

## Running them on real hardware

Divide the clock for real time: 1-Wire and PS/2 use 1 µs ticks
(CLKDIV = 49 at 50 MHz), NEC uses 70.3 µs ticks (CLKDIV = 3514), SWD CLKDIV =
1, and CAN CLKDIV = 1 / 3 / 7 / 15 for 1 Mbit/s / 500 / 250 / 125 kbit/s. The
tests run 1-Wire, NEC and PS/2 faster than real time (each program's timing
is in ticks, so this scales cleanly) to keep simulations short.

Open-drain buses (1-Wire, PS/2) need external pull-ups on the `uio` pins:
4.7 kΩ for 1-Wire, and 5 V-tolerant level shifting for PS/2. CAN needs a
3.3 V transceiver (e.g. SN65HVD230). An IR receiver module (TSOP38238) can
connect to `ui_in` directly. None of these has been tried on the FPGA board or
silicon yet.
