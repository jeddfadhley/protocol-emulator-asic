# Programming PEMU

This guide shows how to write a protocol for PEMU. The normative details are
in [isa.md](isa.md). Working examples are in [`sw/programs/`](../sw/programs).

## The model in one paragraph

An engine runs a short program, at most 32 instructions, in a loop. Each
instruction takes exactly `1 + delay` **ticks**, and a tick is `CLKDIV + 1`
clocks. You set protocol timing by counting ticks. Data arrives from the host
through the TX FIFO into the output shift register (OSR), and `OUT` moves bits
from the OSR to pins or registers. `IN` moves bits from pins into the input
shift register (ISR), which goes back to the host through the RX FIFO.
**Side-set** changes up to 3 pins (such as a clock line) in the same tick as
any other instruction.

## Assembler

```
python3 -m pemu.asm sw/programs/uart_tx.pasm          # hex words to stdout
python3 -m pemu.asm prog.pasm -o prog.hex
```

Run it from `sw/`, or put `sw/` on `PYTHONPATH`. From Python,
`pemu.assemble(text)` returns a `Program` with `.code`, `.wrap_bottom`,
`.wrap_top`, `.side_count` and `.trap`.

### Syntax

```
.program name           ; optional
.side_set 1             ; 0-3 side-set bits; add "pindirs" to drive directions
.define BAUD_TICKS 8    ; named constant
.trap handler           ; label to jump to when a WAIT ... timeout expires
.wrap_target            ; loop start (default: first instruction)
.wrap                   ; loop end (default: last instruction)
label:
    instr operands  [side v]  [delay]
.word 0x1234            ; raw instruction word
```

Comments start with `;` or `//`. With `.side_set n`, every instruction must
carry a `side` value, and the delay field shrinks to `5 - n` bits.

### Instructions

| Syntax | Effect |
|--------|--------|
| `jmp [cond,] target` | cond: `!x`, `x--`, `!y`, `y--`, `x!=y`, `pin`, `!osre` |
| `wait <0\|1> gpio <n> [timeout]` | wait for absolute pin n to reach the level |
| `wait <0\|1> pin <n> [timeout]` | wait for pin IN_BASE + n |
| `wait <0\|1> edge <n> [timeout]` | wait for a transition to the level on IN_BASE + n |
| `wait <0\|1> flag <n> [timeout]` | wait for event flag n (waiting for 1 also clears it) |
| `in <src>, <1-16>` | src: `pins x y null crc time isr osr` |
| `out <dst>, <1-16>` | dst: `pins x y null pindirs pc isr` |
| `push [iffull] [block\|noblock]` | ISR to the RX FIFO |
| `pull [ifempty] [block\|noblock]` | TX FIFO to the OSR |
| `mov <dst>, [~\|::]<src>` | dst: `pins x y crc pindirs pc isr osr`; `~` inverts, `::` bit-reverses |
| `set <dst>, <0-31>` | dst: `pins x y pindirs` |
| `set flag, <n>` / `clr flag, <n>` | raise or clear event flag n |
| `nop` | `mov y, y` |

## Worked example: UART transmitter

```
.wrap_target
    pull                    ; wait for a byte (line idles high)
    set pins, 0      [7]    ; start bit: 1 + 7 = 8 ticks
bitloop:
    out pins, 1             ; 1 tick
    jmp x--, bitloop [6]    ; 7 ticks -> 8 per data bit
    set pins, 1      [5]    ; stop bit: 6 ticks here ...
    set x, 7                ; ... + 1 here + 1 for the next pull = 8
.wrap
```

One bit is 8 ticks, so `CLKDIV = clk / (8 * baud) - 1`. The host configures
`out_base = set_base = TX pin`, `set_count = 1`, LSB-first (`out_right`), and
`X = 7`.

## Host side

The host writes registers over SPI (see the register map in
[isa.md](isa.md)). From Python, `pemu.host` builds the register writes:

```python
from pemu import assemble_file, EngineConfig, load_writes, config_writes, ctrl

prog = assemble_file("sw/programs/uart_tx.pasm")
cfg = EngineConfig(clkdiv=53, out_base=12, set_base=12, set_count=1, out_right=True, x=7)
writes = load_writes(prog, origin=0) + config_writes(0, prog, 0, cfg)
writes += [(0x66, 1 << 12),                       # PIN_SET: TX idles high
           (0x60, ctrl(enable=1, restart=1))]     # start engine 0
```

On the Tiny Tapeout demo board, `sw/board/pemu_board.py` does this over
bit-banged SPI from MicroPython.

## Tips

- **Count ticks per bit first**, then choose `CLKDIV`. Make every path
  through the loop take the same number of ticks; the formal properties
  guarantee that each instruction takes exactly `1 + delay`.
- **Wrap is free.** `.wrap` costs no cycles, so use it instead of a trailing
  `jmp`.
- **Branch on data** with `out x, 1` followed by `jmp !x, ...`. There is no
  ALU; host-side code does the arithmetic.
- **Open-drain** (I2C, 1-Wire): keep `PIN_OUT` at 0 and drive `pindirs`.
  Side-set can drive directions too (`.side_set 1 pindirs`).
- **Timeouts** make protocols robust: `wait 1 pin 1 timeout` with
  `.trap recover` handles a stuck bus without help from the host.
- **Two engines** can cooperate through event flags (`set flag` and
  `wait 1 flag`), for example one engine receiving while the other answers.
- **Loopback mode** (`CTRL.LOOPBACK`) connects the engines to each other's
  outputs, so you can test a protocol pair entirely on-chip.
- **Programs are relocatable.** Jump targets are relative to `ORIGIN`, so the
  same machine code works at any address. The two engines can share code.

## Example programs

| Program | What it shows |
|---------|---------------|
| `uart_tx.pasm`, `uart_rx.pasm` | basic timing, framing-error flag |
| `spi_master.pasm` | side-set clock, autopush/autopull, 2 instructions |
| `i2c_master.pasm` | open drain, clock stretching, repeated START, bus-timeout trap |
| `ws2812.pasm` | sub-microsecond pulse timing |
| `sd_cmd.pasm` | CRC unit generating SD-card CRC7 on the fly |
| `edge_stamp.pasm` | edge waits plus TIME capture (logic analyser) |
| `usb_ls_tx.pasm` | USB low-speed packets at exactly 1.5 Mbit/s |
