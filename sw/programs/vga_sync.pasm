.program vga_sync
; VGA 640x480 at 60 Hz: sync timing, background and line signals for
; vga_pixels on the other engine. A 50 MHz clock gives a 25 MHz pixel clock
; (2 clocks per pixel), which every monitor accepts for this mode.
;
; CLKDIV = 15: one tick is 16 clocks = 8 pixels, and a line is 100 ticks:
;   hsync 12 | back porch 6 | visible 80 | front porch 2
; A frame is 525 lines: visible 480 | front porch 10 | vsync 2 | back porch 33
;
; Config: set_base = VSYNC pin, set_count = 5, so one `set pins` writes
;   bit 0 VSYNC, bits 1-3 the low red/green/blue bits, bit 4 HSYNC
; (the TinyVGA PMOD order on uio[3:7]). Both syncs are active low.
; Host: CRC = 478, X = 14, flag 1 set before starting (see vga_pixels).
;
; Flag 0 tells vga_pixels that a visible line starts. Flag 1 (every 15th
; line) tells it to load the next row of the picture. X counts the 15 lines.
; The values for `set pins` (no expressions in the assembler):
.define HSYNC_ON 1      ; hsync pulse, vsync idle
.define IDLE 17         ; both idle (high), colour off
.define VISIBLE 25      ; both idle, background on (8 = low blue bit)
.define VSYNC_HS 0      ; vsync pulse and hsync pulse
.define VSYNC 16        ; vsync pulse, hsync idle
.define LINE 0
.define ROW 1

.wrap_target
; ---- first visible line: also loads Y with the line count (CRC = 478) ----
    set pins, HSYNC_ON        [11]    ; tick 0: hsync
    set pins, IDLE     [2]     ; tick 12: back porch
    set flag, LINE      [2]     ; tick 15
    set pins, VISIBLE          ; tick 18: visible area, background on
    jmp x--, row0               ; tick 19: count lines in this picture row
row0:
    skip !xneg                  ; X was 0: last line of the row ...
    set flag, ROW               ; ... ask for the next row
    skip !xneg
    set x, 14           [31]    ; tick 23
    mov y, crc          [31]    ; tick 55
    nop                 [10]    ; tick 87
    set pins, IDLE             ; tick 98: front porch
    nop                         ; tick 99
; ---- visible lines 1-479, the same with a loop ----
visible:
    set pins, HSYNC_ON        [11]
    set pins, IDLE     [2]
    set flag, LINE      [2]
    set pins, VISIBLE
    jmp x--, row1
row1:
    skip !xneg
    set flag, ROW
    skip !xneg
    set x, 14           [31]
    nop                 [31]
    nop                 [10]
    set pins, IDLE
    jmp y--, visible
; ---- 10 lines of front porch ----
    set pins, HSYNC_ON        [11]
    set pins, IDLE     [31]
    set y, 8            [31]
    nop                 [23]
fporch:
    set pins, HSYNC_ON        [11]
    set pins, IDLE     [31]
    nop                 [31]
    jmp y--, fporch     [23]
; ---- 2 lines of vertical sync ----
    set pins, VSYNC_HS  [11]
    set pins, VSYNC     [31]
    set y, 0            [31]
    nop                 [23]
vsync:
    set pins, VSYNC_HS  [11]
    set pins, VSYNC     [31]
    nop                 [31]
    jmp y--, vsync      [23]
; ---- 33 lines of back porch ----
    set pins, HSYNC_ON        [11]
    set pins, IDLE     [31]
    set y, 31           [31]
    nop                 [23]
bporch:
    set pins, HSYNC_ON        [11]
    set pins, IDLE     [31]
    nop                 [31]
    jmp y--, bporch     [23]
.wrap
