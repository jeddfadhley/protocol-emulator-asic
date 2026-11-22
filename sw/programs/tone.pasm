.program tone
; A square-wave instrument: one note per word, switched on flag 0.
;   [15:12] note length (for `conductor`; ignored here)
;   [11:0]  half period - 4, in ticks; 0 = rest (silence)
; Each half of the wave lasts exactly (value + 4) ticks. CLKDIV = 49 gives
; 1 us ticks: notes from 122 Hz (B2) up, tuned to within 2 cents to C6.
; Config: side_base = the speaker pin. CRC = the gap of silence before each
;   note, in 16-tick steps minus 1 (312 = 5 ms at 1 us), so repeated notes
;   stay separate.
; Words come from the TX FIFO: from the packet RAM by DMA (music box, with
; `conductor` on the other engine setting flag 0), or from the host (MIDI
; synth: the host pushes a word, then sets flag 0).
.side_set 1
.define NEXT 0
next:
    wait 1 flag NEXT    side 0      ; the next note (clears the flag)
    pull                side 0
    out null, 4         side 0      ; the length is the conductor's
    out x, 12           side 0      ; half period - 4
    mov y, crc          side 0
gap:
    jmp y--, gap        side 0 [15] ; a short silence between notes
    jmp !x, next        side 0      ; a rest: silent until the next note
.wrap_target
    mov y, x            side 0 [2]  ; low half: 3 + (X + 1) ticks
low:
    jmp y--, low        side 0
    mov y, x            side 1      ; high half: 1 + (X + 1) + 2 ticks
high:
    jmp y--, high       side 1
    skip !flag NEXT     side 1      ; flag 0: on to the next note
    jmp next            side 1
.wrap
