.program vga_pixels
; VGA pixels for vga_sync: a 64 x 32 one-bit picture, streamed from the
; packet RAM by DMA. Each picture pixel is 10 x 15 screen pixels, so the
; picture fills the 640 x 480 screen. 128 words = 32 rows of 4 words.
;
; CLKDIV = 1: one tick is one screen pixel (2 clocks).
; Config: out_base = set_base = the pixel pin, set_count = 1,
;   pull_thresh = 15 (so `jmp !osre` loops 15 times: see below).
; Host: picture in the packet RAM at 0, DMA ring on this engine's channel
;   (ADDR 0, COUNT 255), flag 1 set, both engines restarted together.
;
; A row's 4 words are kept in X, Y, CRC and ISR for its 15 lines.
; Each word is 16 pixels, most significant bit first, 10 ticks each:
;   mov (1) + 15 x [out (9) + jmp (1)] + out (9), and the next word's mov
; makes the 16th pixel 10 ticks too.
.define LINE 0
.define ROW 1

load:
    wait 1 flag ROW             ; next row: 4 words from the DMA
    pull
    mov x, osr
    pull
    mov y, osr
    pull
    mov crc, osr
    pull
    mov isr, osr
line:
    wait 1 flag LINE    [21]   ; the first pixel lines up with the background
    mov osr, x
w0: out pins, 1         [8]
    jmp !osre, w0
    out pins, 1         [8]
    mov osr, y
w1: out pins, 1         [8]
    jmp !osre, w1
    out pins, 1         [8]
    mov osr, crc
w2: out pins, 1         [8]
    jmp !osre, w2
    out pins, 1         [8]
    mov osr, isr
w3: out pins, 1         [8]
    jmp !osre, w3
    out pins, 1         [9]
    set pins, 0                 ; black outside the visible area
    skip !flag ROW
    jmp load
    jmp line
