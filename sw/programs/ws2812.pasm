.program ws2812
; WS2812 / NeoPixel LED driver at 800 kbit/s. 10 ticks per bit:
;   CLKDIV = clk / 8 MHz - 1
; Config: side_base = data pin, autopull, shift left, pull_thresh 8
; (one GRB byte per word in bits [15:8]) or 16 (two bytes per word).
; The line stays low while the FIFO is empty, which latches the LEDs.
.side_set 1
.wrap_target
bitloop:
    out x, 1            side 0 [2]  ; low tail of the previous bit
    jmp !x, zero        side 1 [1]  ; high for 2 ticks
    jmp bitloop         side 1 [4]  ; a 1: stay high 5 more
zero:
    nop                 side 0 [4]  ; a 0: go low early
.wrap
