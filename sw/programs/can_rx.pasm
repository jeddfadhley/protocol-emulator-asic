.program can_rx
; CAN 2.0A receiver that acknowledges frames, checked on the chip (v4).
; In v3 this did not fit: finding the ACK slot means counting the bits of a
; frame whose length is only known halfway through (docs/new-protocols.md).
; v4's add and skip, and the OSR count used as a second counter, make it fit.
;
; Per bit the engine resynchronizes on recessive-to-dominant edges, removes
; stuff bits (a run of 5 equal bits is followed by one opposite bit),
; feeds the data bits to the CRC unit, and counts them. From the DLC it
; works out where the CRC ends, checks it (the CRC register is 0 when the
; frame is good), checks the CRC delimiter, and if both are right drives
; the ACK slot dominant. Other nodes see the frame acknowledged; the host
; gets every bit of it.
;
; One bit is 50 ticks: CLKDIV = 0 gives 1 Mbit/s at 50 MHz, 1 500 kbit/s,
; 3 250 kbit/s, 7 125 kbit/s. Bits are sampled 37 ticks after their start (74%).
; Pins: RXD = in_base = jmp_pin; TXD = set_base (1 = recessive).
; Config: set_count = 1, PUSH_THRESH = 14, PULL_THRESH = 4, CRC_IN,
; CRCPOLY = 0x8B32 (CRC-15 << 1), TIMEOUT = 19, event flag 3 free for the
; engine. TXD should idle recessive before the engine starts.
;
; Registers: X = data bits left in this part of the frame; Y = non-zero in
; the header, 0 after it; OSR count = the current run of equal bits - 1 (the
; program never shifts real data through the OSR); ISR = received bits.
;
; Words pushed per frame (bits right-aligned, oldest first):
;   14 bits: ID[10:0], RTR, IDE, r0
;    4 bits: DLC
;   14 bits at a time: the data bytes and the CRC (8 x bytes + 15 bits),
;   then the remainder (0-13 bits, possibly an empty word)
;   a status word with bit 15 set: 0xFFFF = acknowledged, 0xFFFE = an
;   error (CRC, CRC delimiter or stuffing). A stuff error ends the frame
;   early, so its status word comes after fewer words.
; Only standard data frames are acknowledged. Remote and extended frames
; are a different length: their CRC check fails, so they are reported as
; CRC errors and never acknowledged (other nodes acknowledge them).
.define F 3
.trap p1                        ; no falling edge after a recessive bit: the next bit is recessive

start:
    set pins, 1                 ; release TXD
    set y, 20                   ; 21 x 24 ticks: 10 recessive bits mean the bus is between frames
idle:
    jmp pin, idle_ok
    jmp start                   ; dominant: count again
idle_ok:
    jmp y--, idle   [22]        ; leaves Y = 0xFFFF: "in the header"
sof:
    wait 0 pin 0                ; start of frame: hard sync. Its sample point would be 37 ticks on
    mov osr, null               ; a run of 1 (count 0)
    mov crc, null   [31]
    set x, 18       [6]         ; header: ID (11), RTR, IDE, r0, DLC (4)

; ---- after a dominant bit, sampled at S. The next sample is at S + 50.
p0:
    push iffull noblock [20]    ; S + 4
    jmp !osre, p0_data          ; S + 25: fewer than 5 equal bits so far
    nop             [23]        ; a stuff bit comes next: it must be recessive
    jmp pin, p0_1   [1]         ; S + 50
    jmp error
p0_data:
    jmp !x, pe0                 ; S + 26: this part of the frame is complete
    jmp x--, p0_s   [22]        ; S + 27

; ---- end of a part of the frame, at S - 23 (S = the next sample point)
pe1:
    set flag, F     [1]         ; the last bit was recessive
pe:
    jmp !y, crc_end             ; S - 21: the data and CRC are complete
    ; End of the header. ISR holds the DLC. Data and CRC: 8 x min(DLC, 8) + 15 bits.
    mov x, isr                  ; DLC
    push                        ; word 2: the DLC
    set y, 8
    skip x<y
    mov x, y                    ; DLC 9-15 also mean 8 bytes
    mov x, x + x
    mov x, x + x
    mov x, x + x                ; 8 x bytes
    set y, 14
    mov x, x + y                ; + 15 CRC bits, - 1 for the bit sampled next
    set y, 0        [7]         ; from now on: data and CRC
    skip !flag F                ; S - 2: was the last header bit recessive?
    jmp p1_s                    ; yes: sample as after a recessive bit
p0_s:
    in pins, 1                  ; S: a data bit (CRC_IN feeds it to the CRC)
    jmp pin, p0_1
    out null, 1                 ; dominant after dominant: the run grows
    jmp p0
p0_1:
    mov osr, null               ; recessive after dominant, or the stuff bit: a new run
    jmp a1

; ---- after a recessive bit, sampled at S
p1_1:
    out null, 1     [1]         ; recessive after recessive: the run grows
a1:
    wait 0 pin 0 timeout [10]   ; S + 4: a falling edge starts a dominant bit: resync on it.
                                ; None by S + 23: the next bit is recessive (trap to p1).
p1:                             ; S' - 26, S' = the next sample point
    push iffull noblock
    jmp !osre, p1_data
    nop             [23]        ; a stuff bit comes next: it must be dominant
    jmp pin, error              ; S'
    jmp p1_0
p1_data:
    jmp !x, pe1
    jmp x--, p1_s   [22]
p1_s:
    in pins, 1                  ; S'
    jmp pin, p1_1
p1_0:
    mov osr, null               ; dominant after recessive: a new run
    jmp p0

pe0:
    clr flag, F                 ; S - 23: the last bit was dominant
    jmp pe

; ---- the CRC has been received (at S - 20, S = the CRC delimiter's sample point)
crc_end:
    push                        ; the last 0-13 bits
    mov y, crc      [18]        ; 0 when the frame is intact
    skip pin                    ; S: the CRC delimiter must be recessive ...
    mov y, ~null                ; ... if it is not, the frame is bad
    jmp !y, ack     [7]
error:
    set y, 1
report:                         ; Y = status
    mov isr, ~y
    push
    jmp start                   ; (which releases TXD: at the end of the ACK slot)
ack:
    set pins, 0     [31]        ; S + 10: the ACK slot starts on the bus at S + 11
    set y, 0        [13]
    jmp report
