.program spi_master
; SPI master, mode 0 (CPOL = 0, CPHA = 0), MSB first. SCK period = 4 ticks.
; Config: out_base = MOSI, in_base = MISO, side_base = SCK,
; autopull + autopush with both thresholds = bits per word, shift left.
; TX words are left-aligned (an 8-bit byte goes in bits [15:8]);
; RX words are right-aligned. Chip select is driven by the host.
.side_set 1
.wrap_target
    out pins, 1     side 0 [1]   ; MOSI changes while SCK is low
    in pins, 1      side 1 [1]   ; sample MISO on the rising edge
.wrap
