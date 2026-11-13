.program swd
; Half-duplex clocked shifter for ARM Serial Wire Debug (also fits 3-wire
; SPI and other one-data-line buses).
; Written after the hardware was frozen: see docs/new-protocols.md.
;
; SWCLK idles low. The host changes SWDIO before a rising edge and the
; target samples on it; the target changes SWDIO after a rising edge and the
; host samples it at the next rising edge, a full period later (so the
; target's output delay and the chip's 2-clock input synchronizer fit).
; Each bit is 2 x (1 + 1) = 4 ticks: CLKDIV = 1 gives SWCLK = 6.25 MHz at
; 50 MHz (CLKDIV = 0, 12.5 MHz, also works in simulation).
; Pins: SWDIO = out_base = set_base = in_base, SWCLK = side_base.
; Config: set_count = 1, out_right, in_right, autopull and autopush at 16.
;
; A command word is [5:0] n - 1 (1 to 64 bits), [6] 1 = read.
;   write: the next ceil(n / 16) TX words hold the bits, LSB first.
;   read:  SWDIO is released and n bits are sampled. The RX FIFO gets
;          floor(n / 16) + 1 words; the last holds the final n mod 16 bits
;          in its top bits (it is 0 when n is a multiple of 16).
; The host builds SWD packets from these: request (write 8), turnaround and
; ACK (read 4), data and parity (read or write 33), turnaround. See
; sw/pemu/swdhost.py.
.side_set 1
.wrap_target
cmd:
    pull                side 0
    out x, 6            side 0      ; x = n - 1
    out y, 1            side 0      ; y = read
    out null, 9         side 0      ; the rest of the word: the next OUT autopulls
    jmp !y, write       side 0
    set pindirs, 0      side 0      ; release SWDIO
read_bit:
    in pins, 1          side 1 [1]  ; rising edge: sample the bit the target set a period ago
    jmp x--, read_bit   side 0 [1]
    push                side 0      ; the last, partial word
    jmp cmd             side 0
write:
    set pindirs, 1      side 0
write_bit:
    out pins, 1         side 0 [1]  ; SWDIO changes while SWCLK is low
    jmp x--, write_bit  side 1 [1]  ; the target samples on the rising edge
.wrap
