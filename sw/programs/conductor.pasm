.program conductor
; The music box's timekeeper. It reads the same note words as `tone` (both
; engines' DMA channels stream the same packet RAM), waits for each note's
; length and then sets flag 0, which moves `tone` on to the next note.
;   [15:12] length - 1, in steps (1-16 steps: a 16th note to a whole note)
; One step lasts (CRC + 3) ticks: CLKDIV and CRC set the tempo. With
; CLKDIV = 499 (10 us ticks), CRC = 12497 is 125 ms, a 16th note at 120 bpm.
; The host sets flag 0 when it starts both engines, so the first note
; plays at once.
.define NEXT 0
.wrap_target
    pull                    ; this note's word
    out y, 4                ; its length - 1, in steps
step:
    mov x, crc
tick:
    jmp x--, tick
    jmp y--, step
    set flag, NEXT
.wrap
