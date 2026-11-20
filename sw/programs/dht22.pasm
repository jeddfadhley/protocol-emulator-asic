.program dht22
; DHT22 / AM2302 temperature and humidity sensor (one open-drain wire) (v4).
; The sensor sends 40 bits as high pulses: about 27 us for a 0 and 70 us
; for a 1. The firmware times each pulse with TIME and the new add
; (`mov x, ~time` ... `mov x, x + time` = length - 1), then compares it with
; Y using skip. Exactly one of the two IN instructions runs, and the other
; still takes its tick, so every bit takes the same time.
;
; The host writes any word to the TX FIFO to start a reading and gets 5
; bytes: humidity (2), temperature (2), checksum. Nothing comes back if no
; sensor answers (the wait timeouts trap back to the start).
; One tick = 1 us: CLKDIV = 49 at 50 MHz.
; Pins: DATA = in_base = set_base, with a pull-up; PIN_OUT stays 0.
; Config: set_count = 1, autopush at 8, Y = 48 (us), TIMEOUT = 200.
.trap start
start:
    mov isr, null               ; drop a reading cut short
    pull                        ; wait for the host to ask
    mov osr, ~null              ; OSR bit 0 = 1, for `in osr, 1`
    set pindirs, 1              ; start signal: DATA low for 1 ms
    set x, 31
start_low:
    jmp x--, start_low [31]
    set pindirs, 0              ; release: the sensor answers 20-40 us later
    wait 0 pin 0 timeout        ; its response: 80 us low
    wait 1 pin 0 timeout        ; 80 us high
    wait 0 pin 0 timeout        ; the first bit's 50 us low
bit:
    wait 1 pin 0 timeout        ; the bit's high pulse starts
    mov x, ~time
    wait 0 pin 0 timeout        ; after the 40th bit the line stays high: trap
    mov x, x + time             ; X = pulse length - 1, in us
    skip x>=y                   ; a short pulse is a 0 ...
    in null, 1
    skip x<y                    ; ... a long one a 1
    in osr, 1
    jmp bit
