.program eth_tx
; 10BASE-T Ethernet transmitter. Needs a 50 MHz clock and CLKDIV 0, so one
; bit is exactly 5 ticks (100 ns). Manchester code: each bit is sent as its
; inverse, then itself, so every bit has a transition in the middle; the
; receiving PHY locks onto those. Each half is exactly 2.5 ticks (50 ns): the
; mid-bit write is "late", so the pins change half a clock after it.
;
; Pins: OUT_BASE = SET_BASE = SIDE_BASE = TD+, with PINCFG.DIFF so TD- is
; always the inverse of TD+. Side-set drives the pair's direction: released
; (both wires idle, 0 V across the transformer) between frames.
;
; Frames come from the packet RAM by DMA: word 0 = number of bits - 1, then
; the bits (preamble, SFD, frame, FCS), LSB first. See sw/pemu/eth.py.
; Config: OUT_RIGHT, AUTOPULL (threshold 16), TIMEOUT = link pulse tick,
; CRC register = link pulse divider (it is not used as a CRC here).
.side_set 1 pindirs
.trap link_pulse
idle:
    wait 1 flag 0 timeout   side 0   ; host raises flag 0 when a frame is queued
    pull                    side 0   ; bit count - 1
    out y, 16               side 0
    out x, 1                side 0   ; first bit
bit:
    mov pins, ~x            side 1 [1]   ; first half: inverted bit (2.5 ticks)
    mov pins, x late        side 1       ; second half: the bit, from 2.5 ticks
    out x, 1                side 1
    jmp y--, bit            side 1
    set pins, 1             side 1 [14]  ; end of frame: 300 ns high (TP_IDL)
    mov y, crc              side 0       ; release the pair
    jmp idle                side 0
link_pulse:                              ; a wait timed out: no frame yet
    jmp y--, idle           side 0
    mov y, crc              side 0
    set pins, 1             side 0
    nop                     side 1 [4]   ; 100 ns normal link pulse (NLP)
    jmp idle                side 0
