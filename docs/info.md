<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

## How it works

This is milestone 0 of a programmable protocol emulator. For now the design repeatedly sends the hard-coded
message `Hello, world!\r\n` out of a UART transmitter on `uo_out[4]`.

The UART runs at 115200 baud, 8 data bits, no parity, 1 stop bit (8N1), derived from a 50 MHz clock
(434 clocks per bit, about 0.01% baud error). The TX line idles high, including during reset.

## How to test

1. Supply a 50 MHz clock and release reset.
2. Connect a serial terminal at 115200 8N1 to `uo_out[4]`. On the Tiny Tapeout demo board this is the pin
   wired to the RP2040's UART RX.
3. The message `Hello, world!` should print continuously.

## External hardware

None beyond a USB-serial adapter or the demo board's RP2040.
