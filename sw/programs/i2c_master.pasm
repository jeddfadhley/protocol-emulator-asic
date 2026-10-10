.program i2c_master
; I2C master with clock stretching and a bus timeout.
; Pins: SDA = in_base + 0 = out_base = set_base, SCL = in_base + 1 = side_base.
; Open drain: PIN_OUT stays 0 for both pins and the program drives the
; direction (1 = pull low). Config: set_count = 1, shift left, TIMEOUT set.
; One bit is >= 10 ticks: for 100 kHz at 50 MHz use CLKDIV = 49.
;
; Each TX word describes one byte (sent MSB first):
;   [15] START (repeated START if mid-transfer), [14:7] ~data,
;   [6] ~ack (1 = drive ACK low), [5] STOP afterwards
; For reads, send data 0xFF (all released) and set ~ack for every byte but
; the last. Each byte pushes a 9-bit word: [8:1] SDA data, [0] ACK (0 = ACK).
; If SCL is held low for longer than TIMEOUT, both lines are released,
; flag 1 is set and the engine returns to idle.
.side_set 1 pindirs              ; side 1 holds SCL low, side 0 releases it
.define BUS_ERR 1
.trap bus_error
idle:
    pull                 side 0      ; bus free
    out x, 1             side 0      ; x = START flag
    jmp x--, start_cond  side 0
    jmp byte             side 0
rstart:
    set pindirs, 0       side 1 [3]  ; SCL low, release SDA
    wait 1 pin 1 timeout side 0 [3]  ; release SCL (honours stretching)
start_cond:
    set pindirs, 1       side 0 [3]  ; SDA falls while SCL is high: START
byte:
    set y, 8             side 1      ; SCL falls; 9 bits including ACK
bit:
    out pindirs, 1       side 1 [3]  ; SDA changes one tick after SCL falls
    wait 1 pin 1 timeout side 0 [1]  ; release SCL, wait while stretched
    in pins, 1           side 0 [1]  ; sample SDA while SCL is high
    jmp y--, bit         side 1      ; SCL falls
    push                 side 1      ; data + ACK to the host
    out x, 1             side 1      ; x = STOP flag
    jmp !x, next         side 1
    set pindirs, 1       side 1 [3]  ; SDA low
    wait 1 pin 1 timeout side 0 [3]  ; release SCL
    set pindirs, 0       side 0 [3]  ; SDA rises while SCL is high: STOP
    jmp idle             side 0
next:
    pull                 side 1      ; hold SCL low until the next byte
    out x, 1             side 1      ; START flag
    jmp !x, byte         side 1
    jmp rstart           side 1
bus_error:
    set pindirs, 0       side 0      ; release both lines
    mov isr, null        side 0
    set flag, BUS_ERR    side 0
    jmp idle             side 0
