.program eth_rx
; 10BASE-T Ethernet receiver. Needs a 50 MHz clock and CLKDIV 0 (5 clocks
; per bit). The input is the receive pair after a comparator.
;
; Manchester decoding: every bit has a transition in its middle, and the bit
; is the line level just after it. After each mid-bit edge the engine samples
; the line, then waits for the next edge starting 3 clocks later, which skips
; the optional transition at the bit boundary (2-3 clocks after the edge) and
; catches the next mid-bit edge (4-6 clocks after). Waiting for the edge
; re-locks the timing on every bit, so clock differences never accumulate.
;
; The preamble is 1010...10 and the SFD ends in 11, so "1 then 1" marks the
; start of the frame. The frame ends when the line stops toggling: the
; data wait times out and the trap handler finishes the frame.
;
; Config: IN_BASE = JMP_PIN = RD, IN_RIGHT, AUTOPUSH (threshold 16),
; TIMEOUT 4, X = Y = bits that fit in the buffer - 1. RX DMA moves the words
; to the packet RAM. After each frame the last partial word and the number
; of unused bits (X) are pushed, and flag 1 is raised.
.trap frame_end
; Every path below restarts its wait exactly 3 clocks after the edge it
; caught (a falling-through jump and the free wrap cost nothing extra), so
; the engine stays locked to the mid-bit edges.
    jmp hunt0                     ; start in the "no 1 yet" state
hunt1:
    wait any edge 0               ; the previous bit was 1
    jmp pin, data           [1]   ; 1, 1: end of the SFD
hunt0:                            ; (a 0 falls through to here)
.wrap_target
    wait any edge 0               ; looking for a 1 bit
    jmp pin, hunt1          [1]   ; (a 0 wraps back for free)
.wrap
data:
    wait any edge 0 timeout       ; mid-bit edge
    in pins, 1                    ; the bit is the level just after it
    jmp x--, data                 ; back 3 clocks after the edge
drain:
    wait any edge 0 timeout       ; buffer full: skip to the end of the frame
    jmp drain
frame_end:
    push                          ; partial last word
    in x, 16                      ; unused bit count (autopush)
    mov x, y
    set flag, 1
    jmp hunt0
