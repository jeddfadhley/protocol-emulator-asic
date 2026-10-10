.program uart_tx
; 8N1 UART transmitter, LSB first. One bit lasts 8 ticks:
;   CLKDIV = clk / (8 * baud) - 1
; Config: out_base = set_base = TX pin, set_count = 1, out_right.
; Host: drive TX high (PIN_SET) before starting; set X = 7.
; Each TX FIFO word sends its low byte.
.wrap_target
    pull                    ; wait for a byte (line idles high)
    set pins, 0      [7]    ; start bit
bitloop:
    out pins, 1             ; data bit
    jmp x--, bitloop [6]
    set pins, 1      [5]    ; stop bit (6 ticks here + 2 below = 8)
    set x, 7
.wrap
