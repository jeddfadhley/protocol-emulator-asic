.program can_tx
; CAN 2.0 transmitter with bitwise arbitration, through a transceiver.
; Written after the hardware was frozen: see docs/new-protocols.md.
;
; There is no ALU, so the host does the counting: it adds the CRC-15 and the
; stuff bits and gives the finished bit stream (sw/pemu/can.py), usually
; through the packet RAM and DMA. The firmware keeps the bit timing, reads
; the bus back at the sample point and gives way when it loses arbitration.
;
; One bit is 25 ticks, sampled at tick 19 (76%): CLKDIV = 1 gives 1 Mbit/s
; at 50 MHz, 3 gives 500 kbit/s, 7 250 kbit/s, 15 125 kbit/s.
; Pins: TXD = out_base = set_base (1 = recessive), RXD = in_base = jmp_pin.
; Config: out_count = 1, set_count = 1, out_right, in_right, autopull at 16,
; TIMEOUT = 275 (11 bits). A frame starts only after the bus has been idle
; (recessive) for 11 bits: the wait timeout is the idle detector, and its
; trap starts the frame.
; TXD must idle recessive (PIN_SET) before the engine starts.
;
; Words: the number of bits - 1, then 2 bits per CAN bit, LSB first:
; [level, monitor]. Monitor = 1 on recessive bits where reading dominant
; means another node won (or a bit error): every recessive bit except the
; ACK slot. When monitoring fails the engine stops driving at once, raises
; flag 2 (LOST), and keeps sampling the rest of the frame time.
; Either way one word is pushed at the end: the last 16 bus levels, the
; newest at bit 15. For a full frame, bit 7 is the ACK slot (0 = acknowledged).
.define LOST 2
.trap bit                       ; the bus has been idle for TIMEOUT ticks: send
.wrap_target
    pull                        ; a new word: drops the last frame's leftover bits
    out x, 16                   ; frame length - 1
wait_idle:
    wait 0 pin 0 timeout        ; traps after 11 recessive bits (TIMEOUT = 275) ...
    wait 1 pin 0                ; ... but another node is sending: let it finish
    jmp wait_idle
bit:
    out pins, 1                 ; t = 0: drive the bit
    out y, 1        [17]        ; monitor flag
    in pins, 1                  ; t = 19: sample point
    jmp pin, recessive          ; the bus is recessive: always fine
    jmp !y, next                ; dominant, but we sent dominant (or it is the ACK slot)
lost:
    set pins, 1                 ; t = 22: we sent recessive and lost; stop driving
    set flag, LOST
    jmp x--, listen             ; t = 24: the rest of the frame, without driving
    jmp done
recessive:
    nop                         ; t = 21: keeps both paths 25 ticks
next:
    jmp x--, bit    [2]         ; t = 22
done:
    push
.wrap
listen:
    out null, 2     [18]        ; t = 0
    in pins, 1                  ; t = 19
    jmp x--, listen [4]         ; t = 20
    jmp done
