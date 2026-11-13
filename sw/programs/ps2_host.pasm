.program ps2_host
; PS/2 host (keyboard or mouse port), both directions.
; Written after the hardware was frozen: see docs/new-protocols.md.
;
; The device drives the clock (10-16.7 kHz). Both lines are open drain with
; pull-ups: PIN_OUT stays 0 and the program drives directions.
; One tick = 1 us: CLKDIV = 49 at 50 MHz.
; Pins: DATA = in_base + 0 = out_base = set_base, CLK = DATA + 1 = jmp_pin.
; Config: set_count = 2, out_right, in_right, X = 0, TIMEOUT = 15000
; (a device must start clocking within 10 ms of a request to send).
;
; Device to host: each 11-bit frame pushes one word with the start bit at
; [5], the byte at [13:6], odd parity at [14] and the stop bit at [15].
; Host to device: write ~(byte | parity << 8 | 1 << 9) & 0x3FF | 0x8000
; (the bits inverted because 1 drives the line low; bit 15 marks a real
; word). When the device acknowledges, a word 0x0000 is pushed (0x8000 if
; it did not). Neither can be a received frame, whose bits [14:6] always
; hold a 1 because the parity is odd.
; A frame that stops halfway (no clock edge for TIMEOUT) traps and is dropped.
.trap resync
resync:
    set pindirs, 0              ; release both lines
    mov isr, null
idle:
    jmp pin, check_tx           ; CLK high: the bus is idle
    set y, 9                    ; CLK low: the device is sending a frame
    in pins, 1                  ; start bit (DATA is valid while CLK is low)
rx_bit:
    wait 1 pin 1 timeout
    wait 0 pin 1 timeout        ; CLK falls: the next bit is valid
    in pins, 1
    jmp y--, rx_bit
    wait 1 pin 1 timeout        ; the end of the stop bit's clock pulse
    push
    jmp idle
check_tx:
    pull noblock                ; OSR = a word to send, or X = 0
    mov y, osr
    jmp !y, idle
    set pindirs, 2  [31]        ; inhibit: CLK low for 1 + 32 + 4 x 32 = 161 us
    set y, 3
inhibit:
    jmp y--, inhibit [31]
    set pindirs, 3              ; DATA low as well (start bit) ...
    set pindirs, 1  [3]         ; ... then release CLK: request to send
    set y, 9
tx_bit:                         ; 8 data bits, parity, stop (released)
    wait 0 pin 1 timeout        ; the device pulls CLK low: present the bit
    out pindirs, 1
    wait 1 pin 1 timeout        ; the device samples on the rising edge
    jmp y--, tx_bit
    wait 0 pin 1 timeout        ; the 11th clock: the device's ACK
    in pins, 1
    wait 1 pin 1 timeout
    push
.wrap
