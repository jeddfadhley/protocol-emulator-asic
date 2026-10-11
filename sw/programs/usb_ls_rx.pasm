.program usb_ls_rx
; USB low-speed (1.5 Mbit/s) receiver and bus sniffer, at 50 MHz, CLKDIV 0.
; Records the D+/D- line state in the middle of every bit; the host decodes
; NRZI, bit stuffing, PIDs and CRCs (sw/pemu/usb.py: split_capture).
;
; A bit is 33.3 clocks. After every transition on D+ the engine re-syncs and
; samples 16 clocks later, in the middle of the bit. When a bit passes with no
; transition, the WAIT times out after one bit (TIMEOUT 29: 33 clocks from
; sample to sample) and the trap handler samples instead. USB bit stuffing
; guarantees a transition at least every 7 bits, so the error never grows
; past a few clocks.
;
; After 16 bit times with no transition (X counts them) the packet is over:
; that is enough to complete the word holding the end-of-packet; idle
; samples that do not fill a word are dropped, flag 1 is raised, and
; the engine waits for the next packet. Keep-alive EOPs (SE0 with no D+
; edge) are ignored.
;
; Config: IN_BASE = D+ (D- the next pin), IN_RIGHT, AUTOPUSH (threshold 16:
; 8 samples per word), TIMEOUT 29, Y = 15. RX DMA in ring mode moves the
; words to the packet RAM.
.trap no_edge
start:
    wait any edge 0               ; first transition of a packet (SYNC)
.wrap_target
edge:
    mov x, y                [14]  ; re-sync: 15 clocks
    in pins, 2                    ; sample D+ and D- 16 clocks after the edge
wait:
    wait any edge 0 timeout       ; next transition, or one bit time without one
.wrap
no_edge:                          ; trap: no transition for a whole bit
    jmp x--, sample
    mov isr, null                 ; packet over: drop the unfinished word
    set flag, 1
    jmp start
sample:
    in pins, 2
    jmp wait
