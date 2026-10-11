.program usb_ls_tx
; USB low-speed (1.5 Mbit/s) packet transmitter. The host does the line
; coding (SYNC, bit stuffing, NRZI, CRC, EOP: see sw/pemu/usb.py); the engine
; plays the line states out with exact timing on a D+/D- pair.
; Each TX word holds 8 states, 2 bits each, first state in bits [15:14]:
;   J = 2, K = 1, SE0 = 0, and 3 = end of packet (release the bus).
; 3 ticks per bit. At 50 MHz (the clock Ethernet needs too), CLKDIV = 10
; gives 33 clocks per bit: 1.515 Mbit/s, +1.0% (USB low speed allows 1.5%).
; Config: out_base = set_base = D+ (D- is the next pin), out_count = 2,
; set_count = 2, autopull (threshold 16), shift left, Y = 3.
    out x, 2                ; first state of a packet (J); stalls when idle
    mov pins, x             ; put it on the pins before driving them
    set pindirs, 3          ; drive D+ and D-
.wrap_target
bit:
    out x, 2
    jmp x!=y, drive
    set pindirs, 0          ; end marker: release the bus
    jmp 0
drive:
    mov pins, x             ; each state lasts exactly 3 ticks
.wrap
