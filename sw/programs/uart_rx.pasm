.program uart_rx
; 8N1 UART receiver, LSB first, 8 ticks per bit (same CLKDIV as uart_tx).
; Config: in_base = jmp_pin = RX pin, in_right.
; Each received byte is pushed with the data in bits [7:0]. If the RX FIFO
; is full the byte is dropped and RX_OVERFLOW is set. A framing error (stop
; bit low) sets flag 0 and discards the byte.
.define FRAME_ERR 0
start:
    wait 0 pin 0            ; falling edge of the start bit
    set x, 7         [10]   ; 12 ticks later we are mid data bit 0
bitloop:
    in pins, 1
    jmp x--, bitloop [6]
    jmp pin, good           ; stop bit should be high
    set flag, FRAME_ERR
    mov isr, null
    wait 1 pin 0            ; wait for the line to go idle
    jmp start
good:
    in null, 8              ; move the byte down to bits [7:0]
    push noblock
