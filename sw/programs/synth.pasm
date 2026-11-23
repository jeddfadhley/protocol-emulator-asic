.program synth
; The MIDI synth's voice: a square wave over the whole MIDI range (8 Hz to
; 12.5 kHz), switched on flag 0. One word per note:
;   [15:12] M, [11:0] X: each half of the wave lasts (M + 1) * (X + 3) + 3
;   ticks. X = 0 is a rest (silence).
; CLKDIV = 49 gives 1 us ticks. High notes use M = 0 (1-tick steps, so the
; tuning is fine); low notes repeat the count M + 1 times, which reaches
; 65 ms half waves: lower than a piano's bottom A (27.5 Hz). The host picks
; the best M and X (music.synth_word); every MIDI note is within 15 cents,
; and A0-C8 (the piano) within 7 (a hundredth of a semitone is a cent).
; A new note starts at the end of a whole wave, so the wave never glitches.
; The host pushes the word, then sets flag 0. Registers: ISR = M, CRC = X.
.side_set 1
.define NEXT 0
next:
    wait 1 flag NEXT    side 0      ; the next note (clears the flag)
    pull                side 0
    out isr, 4          side 0      ; M: how many times to repeat the count
    out x, 12           side 0      ; X
    jmp !x, next        side 0      ; a rest: silent until the next note
    mov crc, x          side 0
.wrap_target
    mov y, isr          side 0 [2]  ; low half: 3 + (M + 1) * (X + 3) ticks
lo_rep:
    mov x, crc          side 0
lo:
    jmp x--, lo         side 0
    jmp y--, lo_rep     side 0
    mov y, isr          side 1      ; high half: 1 + (M + 1) * (X + 3) + 2
hi_rep:
    mov x, crc          side 1
hi:
    jmp x--, hi         side 1
    jmp y--, hi_rep     side 1
    skip !flag NEXT     side 1      ; flag 0: on to the next note
    jmp next            side 1
.wrap
