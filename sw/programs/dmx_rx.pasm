.program dmx_rx
; DMX512 lighting receiver: a fixture that listens to M channels starting
; at address N (v4 firmware; it uses no v4 instructions).
; A frame is a break (at least 88 us low), a mark, then up to 513 slots of
; 250 kbit/s serial (start bit, 8 data bits LSB first, 2 stop bits). Slot
; 0 is the start code; slot k is channel k.
;
; Each received channel value is pushed (in bits 15-8). A break is found
; two ways: a low longer than TIMEOUT while idle (the trap), or a "slot"
; whose stop bit is low.
; One bit is 8 ticks: CLKDIV = 24 at 50 MHz (0.5 us ticks).
; Pins: DMX data (from an RS-485 receiver) = in_base = jmp_pin.
; Config: in_right, TIMEOUT = 100 (50 us: longer than a slot of zeros).
; The host writes N to the TX FIFO once, and sets the CRC register to M
; (CRC is unused here, so it holds the count).
.trap brk
    pull                        ; OSR = N, kept for every frame
idle:
    wait 0 pin 0                ; the line goes low ...
    wait 1 pin 0 timeout        ; ... and stays low past TIMEOUT: a break (trap)
    jmp idle
brk:
    wait 1 pin 0                ; the mark after the break
    mov x, osr                  ; slots to skip: the start code and channels 1..N-1
    mov y, crc                  ; channels to keep
slot:
    wait 0 pin 0                ; start bit
    nop             [10]
    in pins, 1      [7]         ; data bits, sampled mid-bit
    in pins, 1      [7]
    in pins, 1      [7]
    in pins, 1      [7]
    in pins, 1      [7]
    in pins, 1      [7]
    in pins, 1      [7]
    in pins, 1      [7]
    jmp pin, stop_ok            ; the first stop bit must be high ...
    jmp brk                     ; ... low: this was the next break
stop_ok:
    jmp !x, keep
    jmp x--, skip_slot          ; (always jumps: X is not 0)
skip_slot:
    mov isr, null
    jmp slot
keep:
    jmp !y, idle                ; all M channels read: wait for the next break
    jmp y--, store              ; (always jumps)
store:
    push noblock
    jmp slot
