.program nec_ir
; NEC infrared remote-control receiver (the protocol most TV remotes use).
; Written after the hardware was frozen: see docs/new-protocols.md.
;
; The pin is the output of an IR receiver module (TSOP38238 etc.): low while
; a 38 kHz burst is seen. One NEC unit is 562.5 us, and one tick is 1/8 unit:
; CLKDIV = 3514 at 50 MHz. Config: in_base = jmp_pin = IR pin, in_right,
; autopush at 16, TIMEOUT = 80 ticks (5.6 ms).
;
; Frame: 9 ms burst, 4.5 ms space, then 32 bits LSB first. Each bit is a
; 1-unit burst then a 1-unit (0) or 3-unit (1) space. Repeat code (button
; held): 9 ms burst, 2.25 ms space, 1-unit burst.
; Output: a frame pushes two words, [address | ~address << 8] and
; [command | ~command << 8]. A repeat code pushes two zero words (a real
; second word is never 0, since it holds a byte and its inverse).
; A frame cut short (no edge for TIMEOUT ticks) traps and is dropped.
.trap restart
restart:
    mov isr, null               ; drop the partial frame
idle:
    wait 0 pin 0                ; a burst starts
    set y, 31
leader:                         ; it must stay low for 64 ticks (4.5 ms) to be a
    jmp pin, idle               ; 9 ms leader: data bursts and noise go high sooner
    jmp y--, leader
    wait 1 pin 0 timeout [27]   ; the leader ends; skip 3.5 units
    set y, 11
watch:                          ; for the next 3 units: does a burst start?
    jmp pin, quiet
    push                        ; yes: a repeat code (2.25 ms space). Two zero words.
    push
    jmp idle
quiet:
    jmp y--, watch
frame:                          ; no: a 4.5 ms space, a frame follows
    set y, 31
    wait 0 pin 0 timeout        ; first data burst
bit:
    wait 1 pin 0 timeout [10]   ; the burst ends
    in pins, 1                  ; ~1.4 units later: low = the next burst (0), high = 1
    jmp pin, one
next:
    jmp y--, bit                ; for a 0 we are inside the next burst already
    jmp idle
one:
    wait 0 pin 0 timeout        ; for a 1, wait for the next burst
    jmp next
