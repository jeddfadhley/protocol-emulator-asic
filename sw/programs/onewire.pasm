.program onewire
; 1-Wire bus master (Maxim/Dallas DS18B20 and friends), standard speed.
; Written after the hardware was frozen: see docs/new-protocols.md.
;
; One tick = 1 us: CLKDIV = 49 at 50 MHz. The DQ pin is open drain with an
; external pull-up (4.7 kOhm): PIN_OUT stays 0 and the program drives the
; direction (1 = pull low).
; Config: out_base = set_base = in_base = DQ, set_count = 1, out_right,
; in_right, X = 0.
;
; Each TX word is one byte, sent and received LSB first in 70 us slots:
;   [0] RESET first (480 us low, then sample the presence pulse)
;   [8:1] ~data. To read a byte, write 0xFF (~data = 0): a read slot is a
;   write-1 slot whose level the master samples.
; Each TX word gives one RX word: [15:8] the byte read back from the bus,
; [7] presence (0 = a device answered; only meaningful with RESET).
.wrap_target
    pull
    out x, 1                ; x = RESET flag
    jmp !x, byte
    set pindirs, 1          ; reset pulse: 1 + 1 + 15 x 32 = 482 us low
    set y, 14
rst_low:
    jmp y--, rst_low [31]
    set pindirs, 0  [31]    ; release; devices answer 15-60 us later
    set y, 12       [31]
    in pins, 1              ; presence sampled 64 us after release
rst_high:
    jmp y--, rst_high [31]  ; 13 x 32: 481 us after release before the first slot
byte:
    set y, 7
bit:
    set pindirs, 1  [5]     ; t = 0: every slot starts with 6 us low
    out pindirs, 1  [8]     ; t = 6: release for a 1, keep low for a 0
    in pins, 1      [31]    ; t = 15: sample (a device sending 0 holds the line low)
    nop             [12]
    set pindirs, 0  [8]     ; t = 60: release (end of a write-0 pulse)
    jmp y--, bit            ; t = 69: 70 us slot, 1 us recovery
    push
.wrap
