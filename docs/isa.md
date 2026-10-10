# PEMU architecture and ISA reference (v1)

PEMU is a programmable protocol emulator. Two small **engines** run programs
from a shared 64-word instruction memory and drive or sample up to 16 pins
with cycle-exact timing. A host (for example the RP2040 on the Tiny Tapeout
demo board) loads programs and moves data over an SPI slave interface.

This document is the normative spec. The Python model (`sw/pemu/model.py`) is
its executable form, and the RTL is checked against that model cycle by cycle.

---

## 1. Pins

| Pin index | Output                     | Input (normal mode)   |
|-----------|----------------------------|-----------------------|
| 0-7       | `uio_out[i]`, enable `uio_oe[i]` | `uio_in[i]`     |
| 8-14      | `uo_out[i-8]` (always driven)    | `ui_in[i-8]`    |
| 15        | not connected              | `ui_in[7]`            |

`uo_out[7]` is the host SPI `MISO`. `ui_in[4]`, `ui_in[5]` and `ui_in[6]` are
the host SPI `CS_N`, `SCK` and `MOSI` (also readable as pins 12-14, which is
harmless).

The chip holds two 16-bit pin registers: `PIN_OUT` (value) and `PIN_OE`
(direction, 1 = drive). Both reset to 0. Open-drain behaviour (I2C) is done by
keeping `PIN_OUT` at 0 and toggling `PIN_OE`.

Inputs pass through a 2-flop synchronizer, so engines see a pin change two
clocks after it happens.

**Loopback mode** (`CTRL.LOOPBACK`) replaces the input of every pin with what
the chip itself is driving: pins 0-7 read `PIN_OE ? PIN_OUT : 1` (a released
pin reads as pulled up), and pins 8-15 read `PIN_OUT`. There is no
synchronizer delay in loopback. One engine can then talk to the other with no
external hardware, which makes on-chip self test possible.

## 2. Engines

Each engine has:

| State      | Width | Notes |
|------------|-------|-------|
| `PC`       | 5     | relative to `ORIGIN` |
| `X`, `Y`   | 16    | scratch registers and loop counters |
| `ISR`      | 16    | input shift register, with a bit count of 0-16 |
| `OSR`      | 16    | output shift register, with a count of 0-16 (16 = empty) |
| `CRC`      | 16    | left-justified CRC shift register |
| `TIME`     | 16    | counts engine ticks, wraps |
| TX FIFO    | 4x16  | host to engine |
| RX FIFO    | 4x16  | engine to host |

### 2.1 Ticks and timing

An engine advances on a **tick**. With `CLKDIV = N`, a tick happens every
N+1 clocks. On each tick:

1. If the delay counter is non-zero it decrements, and nothing else happens.
2. Otherwise the instruction at `(ORIGIN + PC) mod 64` executes. It either
   **completes**, or **stalls** (a WAIT whose condition is false, a blocking
   PUSH/PULL, or autopush/autopull that cannot proceed). A stalled
   instruction runs again on the next tick.
3. When it completes, `PC` moves on and the delay counter loads the
   instruction's delay field.

So every instruction takes exactly `1 + delay` ticks, plus any stall ticks.
`TIME` increments on every tick. An instruction that reads `TIME` sees the
value from before its own tick's increment.

**Side-set** pins are written on every tick on which the instruction is
evaluated, including stalled ticks. They are not written during delay ticks.

**Next PC:** if the instruction does not jump and `PC == WRAP_TOP`, the next PC
is `WRAP_BOTTOM`. Otherwise it is `PC + 1` (mod 32). Wrapping costs no cycles.

### 2.2 Wait timeouts (trap)

A WAIT with the `T` bit set gives up after a bounded time. While it is
stalled, an internal counter counts stall ticks. On a stall tick where the
counter equals `TIMEOUT`, the engine jumps to `TRAP` (relative address) with no
delay, and sets the sticky `TIMED_OUT` status bit. The trap therefore fires on
evaluation number `TIMEOUT + 1`. The counter and the edge-armed bit clear
whenever any instruction completes.

### 2.3 CRC unit

`CRC` is a left-justified Galois LFSR. For a CRC of width w, set `CRCPOLY` to
`poly << (16 - w)` and preload `CRC` with `init << (16 - w)`. For each bit b:

```
fb  = CRC[15] ^ b
CRC = (CRC << 1) ^ (fb ? CRCPOLY : 0)
```

When `SHIFT.CRC_IN` is set, every completed `IN src, 1` feeds its bit. When
`SHIFT.CRC_OUT` is set, every completed `OUT dst, 1` feeds its bit. Reading
`CRC` (`MOV`/`IN`) returns the left-justified value, so `MOV OSR, CRC`
followed by MSB-first `OUT` sends the CRC most significant bit first.

Examples: CRC-7 (SD card) `0x1200`, CRC-5 (USB) `0x2800`, CRC-16/CCITT
`0x1021`, CRC-16 (USB) `0x8005`, CRC-15 (CAN) `0x8B32`.

### 2.4 FIFOs and shift registers

- `IN` shifts bits into `ISR`, by default MSB-first (shift left, new bits at
  bit 0). `SHIFT.IN_RIGHT` shifts right instead (new bits enter at bit 15).
- `OUT` takes bits from `OSR`, by default from bit 15 (shift left).
  `SHIFT.OUT_RIGHT` takes them from bit 0.
- **Autopush:** after an `IN` that brings the ISR count to `PUSH_THRESH` or
  more, the ISR is pushed and cleared in the same instruction. If the RX FIFO
  is full, the IN stalls instead.
- **Autopull:** before an `OUT`, if the OSR count is at least `PULL_THRESH`,
  the OSR is refilled from the TX FIFO in the same instruction. If the FIFO is
  empty, the OUT stalls.

A threshold field of 0 means 16.

## 3. Instruction encoding

Every instruction is 16 bits:

```
15 14 13 | 12 11 10 9 8 | 7 6 5 4 3 2 1 0
 opcode  | side / delay |    operands
```

The top `SIDE_COUNT` bits of [12:8] are the side-set value. The rest are the
delay (0-31 with no side-set). Bit counts written `n` below are encoded in
[3:0], with 0 meaning 16.

| Op  | Name      | Operands |
|-----|-----------|----------|
| 000 | JMP       | [7:5] cond, [4:0] addr |
| 001 | WAIT      | [7] pol, [6:5] src, [4] T, [3:0] index |
| 010 | IN        | [7:5] src, [3:0] n |
| 011 | OUT       | [7:5] dst, [3:0] n |
| 100 | PUSH/PULL | [7] 0 = push, 1 = pull; [6] if-full/if-empty; [5] block |
| 101 | MOV       | [7:5] dst, [4:3] op, [2:0] src |
| 110 | (reserved, executes as NOP) | |
| 111 | SET       | [7:5] dst, [4:0] imm |

### JMP conditions

| cond | Syntax  | Jump when |
|------|---------|-----------|
| 0    | (none)  | always |
| 1    | `!x`    | X == 0 |
| 2    | `x--`   | X != 0 (X is decremented either way) |
| 3    | `!y`    | Y == 0 |
| 4    | `y--`   | Y != 0 (Y is decremented either way) |
| 5    | `x!=y`  | X != Y |
| 6    | `pin`   | input pin `JMP_PIN` (absolute) is 1 |
| 7    | `!osre` | OSR count < `PULL_THRESH` |

### WAIT sources

| src | Syntax  | Condition |
|-----|---------|-----------|
| 0   | `gpio`  | absolute pin `index` == pol |
| 1   | `pin`   | pin `IN_BASE + index` == pol |
| 2   | `flag`  | event flag `index[2:0]` == pol. When pol = 1, the flag is cleared as the wait completes. |
| 3   | `edge`  | pin `IN_BASE + index` has been seen at `!pol` and is now `pol`, both during this WAIT |

With `T` = 1 (assembler suffix `timeout`) the wait can trap, see 2.2.

### Sources (IN [7:5] and MOV [2:0])

`0 PINS` (inputs rotated right by `IN_BASE`), `1 X`, `2 Y`, `3 NULL` (zeros),
`4 CRC`, `5 TIME`, `6 ISR`, `7 OSR`. `IN` uses the low n bits.

### OUT destinations

`0 PINS` (n bits at `OUT_BASE`), `1 X`, `2 Y`, `3 NULL`, `4 PINDIRS`,
`5 PC`, `6 ISR` (count becomes n), `7 NULL`.

### MOV destinations and ops

Destinations: `0 PINS` (`OUT_COUNT` bits at `OUT_BASE`), `1 X`, `2 Y`,
`3 CRC`, `4 PINDIRS`, `5 PC`, `6 ISR` (count = 0), `7 OSR` (count = 0).

Ops: `0` none, `1` invert (`~`), `2` bit-reverse (`::`), `3` reserved (none).

### SET destinations

`0 PINS` (`SET_COUNT` bits at `SET_BASE`), `1 X`, `2 Y`, `3 FLAG`
(imm[4] = 0 sets flag imm[2:0], imm[4] = 1 clears it), `4 PINDIRS`,
`5`-`7` reserved (NOP).

### PUSH / PULL

- `PUSH`: with if-full, does nothing unless the ISR count is at least
  `PUSH_THRESH`. If the RX FIFO is full it stalls when blocking; otherwise the
  data is dropped and `RX_OVERFLOW` is set. The ISR and its count are cleared.
- `PULL`: with if-empty, does nothing unless the OSR count is at least
  `PULL_THRESH`. If the TX FIFO is empty it stalls when blocking; otherwise
  `OSR = X`. The OSR count is cleared.

### Pin writes

Writing n bits at base b sets pin `(b + k) mod 16` to bit k of the value.
Within one engine, side-set wins over the instruction's own pin write. Across
engines, engine 1 wins over engine 0, and a host write to `PIN_OUT`/`PIN_OE`
wins over both.

### Event flags

There are 8 shared flags. Engines change them with `SET FLAG` and `WAIT flag`
(auto-clear). Engine 0 applies first, then engine 1, then a host write to
`FLAGS` overrides both.

## 4. Host interface

SPI mode 0, MSB first, `SCK` at most `clk / 8`. A transaction:

```
CS_N low, command byte {R/W (1 = read), addr[6:0]}, then 16-bit words, CS_N high
```

Each word is read from or written to `addr`. The address then increments,
except for the FIFO ports (offset `0xF` in an engine block), so a burst can
stream data. For reads, `MISO` carries the word starting on the SCK falling
edge after the command byte. No dummy bytes are needed.

A read of a FIFO port pops the RX FIFO only when the 16th bit of the word
has been clocked, and only if the FIFO held data when the word started. A
read that is cut short leaves the data in the FIFO, and an empty FIFO reads
as 0 without losing a word that arrives during the read.

### Register map

| Addr        | Name     | Description |
|-------------|----------|-------------|
| 0x00-0x3F   | IMEM     | instruction memory |
| 0x40 + 16e  | engine e block, below | |
| 0x60        | CTRL     | [0] EN0, [1] EN1, [2] LOOPBACK. Write-only pulses: [4] RESTART0, [5] RESTART1, [6] STEP0, [7] STEP1, [8] FLUSH0, [9] FLUSH1 |
| 0x61        | FLAGS    | [7:0] event flags |
| 0x62        | PIN_IN   | read only: the inputs engines see |
| 0x63        | PIN_OUT  | |
| 0x64        | PIN_OE   | |
| 0x65        | ID       | read only: `0x5045` ("PE") |
| 0x66        | PIN_SET  | write only: `PIN_OUT \|= value` |
| 0x67        | PIN_CLR  | write only: `PIN_OUT &= ~value` |

Engine block offsets:

| Off | Name     | Reset  | Fields |
|-----|----------|--------|--------|
| 0x0 | CLKDIV   | 0      | tick every CLKDIV+1 clocks |
| 0x1 | PROG     | 0xF800 | [5:0] ORIGIN, [10:6] WRAP_BOTTOM, [15:11] WRAP_TOP |
| 0x2 | PINMAP   | 0      | [3:0] OUT_BASE, [7:4] SET_BASE, [11:8] SIDE_BASE, [15:12] IN_BASE |
| 0x3 | PINCFG   | 0      | [3:0] OUT_COUNT (0 = 16), [6:4] SET_COUNT (0-5), [8:7] SIDE_COUNT (0-3), [9] SIDE_PINDIR, [15:12] JMP_PIN |
| 0x4 | SHIFT    | 0      | [0] IN_RIGHT, [1] OUT_RIGHT, [2] AUTOPUSH, [3] AUTOPULL, [7:4] PUSH_THRESH, [11:8] PULL_THRESH, [12] CRC_IN, [13] CRC_OUT |
| 0x5 | TIMEOUT  | 0xFFFF | wait timeout, in ticks |
| 0x6 | TRAP     | 0      | [4:0] trap address |
| 0x7 | CRCPOLY  | 0      | |
| 0x8 | CRC      | 0      | read/write |
| 0x9 | X        | 0      | read/write |
| 0xA | Y        | 0      | read/write |
| 0xB | ISR      | -      | read only |
| 0xC | OSR      | -      | read only |
| 0xD | STATUS   | -      | [4:0] PC, [7:5] TX level, [10:8] RX level, [11] STALLED, [12] TIMED_OUT, [13] RX_OVERFLOW, [14] TX_OVERFLOW. Write 1 to bits 12-14 to clear them. |
| 0xE | TIME     | 0      | read only |
| 0xF | FIFO     | -      | write: push to the TX FIFO (dropped and `TX_OVERFLOW` set when full). Read: pop the RX FIFO (0 when empty). |

`SET_COUNT` values 6 and 7 act as 5.

**Restart** resets PC, the delay counter, ISR, OSR (count 16, empty), the
timeout counter, the edge-armed bit, TIME and the clock divider. It keeps X, Y,
CRC and the configuration. **Step** runs one tick of a disabled engine.
**Flush** empties both FIFOs of an engine.
