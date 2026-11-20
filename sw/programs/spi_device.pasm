.program spi_device
; SPI device (peripheral), mode 0, MSB first, 8-bit words: the chip plays
; the part a controller talks to, for example an emulated sensor (v4).
;
; Each byte from MOSI is pushed to the RX FIFO. Each byte sent on MISO
; comes from the TX FIFO (the byte in bits 15-8), or is X when the FIFO is
; empty (set X = 0xFF00 to answer 0xFF). MISO floats while deselected.
; `wake pin` (JMP_PIN = CS_N) lets every clock wait end early when the
; controller deselects, so a transfer cut short mid-byte is dropped and the
; next one starts aligned. Without wake each wait would need its own
; timeout, or a polling loop.
;
; Pins: MOSI = in_base, SCK = in_base + 1, CS_N = in_base + 2 = jmp_pin,
; MISO = out_base = set_base.
; Config: CLKDIV = 0, set_count = 1, autopush at 8, out_count = 1.
; SCK up to clk / 16 (the engine needs 4 clocks after each edge).
.wrap_target
idle:
    wait 0 pin 2                ; selected
    set pindirs, 1              ; drive MISO
    pull noblock                ; the first reply byte, or X
    set y, 7
    out pins, 1                 ; its first bit, before the first rising edge
bit:
    wake pin                    ; CS_N rising also ends the next wait
    wait 1 pin 1                ; SCK rises
    jmp pin, deselected
    in pins, 1                  ; sample MOSI (autopush every 8 bits)
    jmp y--, more
    pull noblock                ; after the 8th bit: the next reply byte
    set y, 7
more:
    wake pin
    wait 0 pin 1                ; SCK falls
    jmp pin, deselected
    out pins, 1                 ; the next MISO bit
    jmp bit
deselected:
    set pindirs, 0              ; release MISO
    mov isr, null               ; drop a partial byte
.wrap
