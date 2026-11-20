.program quadrature
; Rotary encoder (quadrature) decoder, counting every edge (x4) (v4).
; X is the position: the host reads (or zeroes) the engine's X register at
; any time. Each of the four states waits for its own input to change,
; and `wake` ends the wait if the other input changes first, so either
; edge is seen within a few clocks. `mov x, x + y` (Y = 1) and
; `mov x, x + osr` (OSR = 0xFFFF, -1) count up and down.
;
; Clockwise is A leading B: AB = 00, 10, 11, 01, 00 (+1 per step).
; Pins: A = in_base, B = jmp_pin. Config: CLKDIV = 0. Edges may come every
; 12 clocks or slower (about 4 MHz of edges at 50 MHz).
    mov osr, ~null              ; -1
    mov isr, null
    in pins, 1                  ; the starting state: A ...
    mov y, isr
    mov x, null                 ; position 0
    jmp !y, a_low
    set y, 1
    jmp pin, s11                ; ... and B
    jmp s10
a_low:
    set y, 1
    jmp pin, s01
    jmp w00
; Each state: wake on B, wait for A; then jmp pin tells which one changed.
s10_inc:
    mov x, x + y
s10:
    wake pin                    ; B rises: 10 -> 11
    wait 0 pin 0                ; A falls: 10 -> 00
    jmp pin, s10_b
    mov x, x + osr              ; A fell: counter-clockwise
w00:
    wake pin                    ; state 00: B rises -> 01
    wait 1 pin 0                ; A rises -> 10
    jmp pin, s00_b
    jmp s10_inc                 ; A rose: clockwise
s00_b:
    mov x, x + osr              ; B rose: counter-clockwise
    jmp s01
s10_b:
    mov x, x + y                ; B rose: clockwise
s11:
    wake !pin                   ; B falls: 11 -> 10
    wait 0 pin 0                ; A falls: 11 -> 01
    jmp pin, s11_a
    mov x, x + osr              ; B fell: counter-clockwise
    jmp s10
s11_a:
    mov x, x + y                ; A fell: clockwise
s01:
    wake !pin                   ; B falls: 01 -> 00
    wait 1 pin 0                ; A rises: 01 -> 11
    jmp pin, s01_a
    mov x, x + y                ; B fell: clockwise
    jmp w00
s01_a:
    mov x, x + osr              ; A rose: counter-clockwise
    jmp s11
