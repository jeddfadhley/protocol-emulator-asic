# 10BASE-T Ethernet on PEMU

PEMU speaks real 10 Mbit/s Ethernet (10BASE-T) with no Ethernet hardware
on the chip. Two short programs do the line coding: `eth_tx`
(16 instructions) and `eth_rx` (15 instructions). The packet RAM and DMA
feed them at wire speed. Plug the chip into a laptop and the laptop sees a
link, can `ping` the chip, and receives the chip's UDP packets in Wireshark.

```
   laptop  <-- Cat5 cable -->  RJ45 jack with magnetics
                                 TD+/TD-  <- 2 x 33 ohm <-  GPIO0 / GPIO1 (eth_tx, PINCFG.DIFF)
                                 RD+/RD-  -> comparator  ->  IN8 = ui_in[0]  (eth_rx)
                                                  PEMU  <-- SPI -->  host (RP2040, or a PC via the FPGA bridge)
```

## What the chip does, and what the host does

| Layer | Where | How |
|-------|-------|-----|
| Manchester line code, 10 Mbit/s | engines | `eth_tx` / `eth_rx` firmware, cycle-exact at 50 MHz |
| Link pulses (so the other side shows "link up") | engine 0 | `eth_tx`, using a WAIT timeout as a 1.3 ms timer |
| Preamble/SFD detection, end of frame | engine 1 | `eth_rx`: "1, 1" ends the SFD; a WAIT timeout ends the frame |
| Moving frames to and from memory | DMA | packet RAM words 0-63 receive, 64-127 transmit |
| FCS (CRC-32), ARP, IPv4, ICMP, UDP | host | `sw/pemu/eth.py`, plain Python (also MicroPython) |

## How the firmware works

**Transmit.** 10BASE-T sends each bit as two halves: the inverse of the bit,
then the bit. Every bit therefore has a transition in its middle, and the
receiver locks onto those. At 50 MHz one bit is 5 clocks. The inner loop is
4 instructions and exactly 5 ticks:

```
bit:
    mov pins, ~x       side 1 [1]   ; first half: inverted bit (2.5 ticks)
    mov pins, x late   side 1       ; second half: the bit, half a tick late
    out x, 1           side 1       ; next bit from the OSR (refilled by autopull + DMA)
    jmp y--, bit       side 1
```

`PINCFG.DIFF` makes GPIO1 the inverse of GPIO0, so one `mov pins` drives
the differential pair. Side-set drives the pair's *direction*: between
frames both wires are released, which puts 0 V across the transformer
(10BASE-T idle). After the last bit the line stays positive for 300 ns
(TP_IDL). While no frame is waiting, the `wait 1 flag 0 timeout` that
waits for the host doubles as a timer: every 12th timeout sends a 100 ns
normal link pulse, one every 15.7 ms (the standard asks for 16 ± 8 ms).

A bit is 5 clocks, so each half has to be 2.5 clocks. The mid-bit write is
`mov pins, x late`: its pins change half a clock after the instruction, on
the falling clock edge. So both halves of every bit are exactly 50 ns, as
the standard specifies, from a 50 MHz clock. This half-clock timing is a
general PEMU feature (every pin has 10 ns resolution), not an Ethernet
special case.

**Receive.** After each mid-bit edge, the bit is the line level just after
it. The receiver waits for an edge (`wait any edge`), samples the line,
and starts waiting again exactly 3 clocks later. That skips the possible
transition at the bit boundary (2-3 clocks after the edge) and catches the
next mid-bit edge (4-6 clocks after). Because it re-locks on every bit, a
clock error between the two sides never builds up:

```
data:
    wait any edge 0 timeout   ; mid-bit edge
    in pins, 1                ; the bit is the level just after it
    jmp x--, data             ; back exactly 3 clocks after the edge
```

To find the start of a frame, the receiver hunts for two 1 bits in a row:
the preamble is 1010...10 and the SFD ends in 11. Each hunting path re-waits
exactly 3 clocks after its edge, using free wraps and fall-throughs. When
the line stops toggling, the data WAIT times out. The trap handler then
pushes the last partial word and the number of unused bits, and raises
flag 1 for the host.

## Measured tolerance (receive)

The tolerance test (`sw/tests/test_eth.py`) sends frames from an independent
10 Mbit/s clock at every phase relative to PEMU's clock:

| Sender clock error | Edge jitter ±0-4 ns | ±6 ns | ±8 ns |
|--------------------|---------------------|-------|-------|
| 0 ppm | 100% | 100% | 60% |
| ±100 ppm (the standard's limit) | 100% | ~80% | ≤3% |
| ±2000 ppm | 100% | ~65% | 0% |

The ±4 ns column is part of CI. The limit comes from sampling at 50 MHz
(20 ns): over a short cable, jitter is a few nanoseconds.

The first version of `eth_rx` failed 1 frame in 30 at ±2 ns jitter. This
sweep found it: two hunting paths re-waited 4 clocks after an edge instead
of 3, so the receiver could slip onto boundary transitions just as the SFD
arrived. Restructuring the hunt so that every path re-waits after exactly
3 clocks fixed it.

## Using it

**Laptop side.** Give the laptop's Ethernet adapter a static address, for
example `192.168.7.1`, netmask `255.255.255.0`. USB-Ethernet adapters work.
The link comes up at 10 Mbit/s half duplex by parallel detection; nothing to
configure.

**Chip side**, from the FPGA host (see [fpga.md](fpga.md)) or the demo
board's MicroPython:

```python
chip.eth_start()                       # load eth_tx + eth_rx, arm the receive DMA
chip.eth_send(eth.udp_frame(b"hi"))    # broadcast UDP to port 7777, see it in Wireshark
chip.eth_serve()                       # answer ARP, ping and UDP echo as 192.168.7.2
```

Then, on the laptop, `ping 192.168.7.2`.

`pemu_board.eth_selftest()` runs the same firmware with no hardware at all:
engine 0 transmits, engine 1 receives through the on-chip loopback.

**Electrical.**
- **Transmit:** GPIO0 (TD+) and GPIO1 (TD-), each through a 33-47 Ω resistor,
  to the TD pins of an RJ45 jack with built-in magnetics (for example a
  HanRun HR911105A breakout).
- **Receive:** RD+/RD- go to a comparator with about 100 Ω across the pair
  and the common mode biased into the comparator's input range. On the
  Basys 3, the FPGA's differential input buffer is the comparator (see
  [fpga.md](fpga.md)). On the ASIC, use an external comparator into
  `ui_in[0]`.
- If nothing is received, swap RD+ and RD-: the receiver needs the
  standard polarity.

## Limits

- Frames up to 124 bytes are received whole (longer ones are captured up to
  124 bytes and the rest skipped). Frames up to 117 bytes, FCS included, can
  be sent. A default ping (98 bytes plus FCS) fits both ways.
- Half duplex, no collision detection. On a point-to-point cable to a laptop
  this does not matter.
- The host computes the FCS and handles every protocol above Ethernet. The
  chip moves bits at wire speed; the host only touches whole frames.
- The firmware's timing assumes a 50 MHz clock (5 clocks per bit).

## Verification

- `eth_loopback`: the transmitter and receiver against each other, on-chip.
- `eth_tx`: a link-partner model samples the pins twice per clock and
  checks every bit cell (two halves of exactly 50 ns), the preamble, TP_IDL length, link pulse width and spacing, and
  the FCS.
- `eth_rx`, `eth_rx_drift`: frames from an independent clock (phase offset,
  +100 ppm, ±3 ns jitter), interleaved link pulses, and a frame longer than
  the buffer.
- `eth_ping`: a laptop model sends ARP and ping; the chip, driven by the
  host code through its real SPI pins, answers both.

All of these run on the Python model, on the RTL through SPI, and on the
gate-level netlist in CI.
