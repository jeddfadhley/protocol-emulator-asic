## PEMU on the Digilent Basys 3 (xc7a35tcpg236-1).
## Pin locations are from Digilent's Basys-3-Master.xdc (digilent-xdc repo).
## Used by both the Vivado flow (build_vivado.tcl) and the openXC7 flow
## (Makefile), so only plain set_property / create_clock commands are used.

## 100 MHz clock
set_property -dict { PACKAGE_PIN W5   IOSTANDARD LVCMOS33 } [get_ports clk]
create_clock -add -name sys_clk_pin -period 10.00 -waveform {0 5} [get_ports clk]

## Centre button = reset
set_property -dict { PACKAGE_PIN U18  IOSTANDARD LVCMOS33 } [get_ports btnC]

## USB-UART (FT2232 channel B)
set_property -dict { PACKAGE_PIN B18  IOSTANDARD LVCMOS33 } [get_ports RsRx]
set_property -dict { PACKAGE_PIN A18  IOSTANDARD LVCMOS33 } [get_ports RsTx]

## LEDs
set_property -dict { PACKAGE_PIN U16  IOSTANDARD LVCMOS33 } [get_ports {led[0]}]
set_property -dict { PACKAGE_PIN E19  IOSTANDARD LVCMOS33 } [get_ports {led[1]}]
set_property -dict { PACKAGE_PIN U19  IOSTANDARD LVCMOS33 } [get_ports {led[2]}]
set_property -dict { PACKAGE_PIN V19  IOSTANDARD LVCMOS33 } [get_ports {led[3]}]
set_property -dict { PACKAGE_PIN W18  IOSTANDARD LVCMOS33 } [get_ports {led[4]}]
set_property -dict { PACKAGE_PIN U15  IOSTANDARD LVCMOS33 } [get_ports {led[5]}]
set_property -dict { PACKAGE_PIN U14  IOSTANDARD LVCMOS33 } [get_ports {led[6]}]
set_property -dict { PACKAGE_PIN V14  IOSTANDARD LVCMOS33 } [get_ports {led[7]}]
set_property -dict { PACKAGE_PIN V13  IOSTANDARD LVCMOS33 } [get_ports {led[8]}]
set_property -dict { PACKAGE_PIN V3   IOSTANDARD LVCMOS33 } [get_ports {led[9]}]
set_property -dict { PACKAGE_PIN W3   IOSTANDARD LVCMOS33 } [get_ports {led[10]}]
set_property -dict { PACKAGE_PIN U3   IOSTANDARD LVCMOS33 } [get_ports {led[11]}]
set_property -dict { PACKAGE_PIN P3   IOSTANDARD LVCMOS33 } [get_ports {led[12]}]
set_property -dict { PACKAGE_PIN N3   IOSTANDARD LVCMOS33 } [get_ports {led[13]}]
set_property -dict { PACKAGE_PIN P1   IOSTANDARD LVCMOS33 } [get_ports {led[14]}]
set_property -dict { PACKAGE_PIN L1   IOSTANDARD LVCMOS33 } [get_ports {led[15]}]

## Pmod JA: PEMU pins 0-7 (uio), bidirectional with pull-ups for open-drain
## buses. Fast, strong drivers for the Ethernet TX pair on JA1/JA2.
set_property -dict { PACKAGE_PIN J1   IOSTANDARD LVCMOS33 DRIVE 16 SLEW FAST PULLTYPE PULLUP } [get_ports {JA[0]}]
set_property -dict { PACKAGE_PIN L2   IOSTANDARD LVCMOS33 DRIVE 16 SLEW FAST PULLTYPE PULLUP } [get_ports {JA[1]}]
set_property -dict { PACKAGE_PIN J2   IOSTANDARD LVCMOS33 DRIVE 16 SLEW FAST PULLTYPE PULLUP } [get_ports {JA[2]}]
set_property -dict { PACKAGE_PIN G2   IOSTANDARD LVCMOS33 DRIVE 16 SLEW FAST PULLTYPE PULLUP } [get_ports {JA[3]}]
set_property -dict { PACKAGE_PIN H1   IOSTANDARD LVCMOS33 DRIVE 16 SLEW FAST PULLTYPE PULLUP } [get_ports {JA[4]}]
set_property -dict { PACKAGE_PIN K2   IOSTANDARD LVCMOS33 DRIVE 16 SLEW FAST PULLTYPE PULLUP } [get_ports {JA[5]}]
set_property -dict { PACKAGE_PIN H2   IOSTANDARD LVCMOS33 DRIVE 16 SLEW FAST PULLTYPE PULLUP } [get_ports {JA[6]}]
set_property -dict { PACKAGE_PIN G3   IOSTANDARD LVCMOS33 DRIVE 16 SLEW FAST PULLTYPE PULLUP } [get_ports {JA[7]}]

## Pmod JB: PEMU pins 8-14 (uo_out[0..6]); JB10 unused
set_property -dict { PACKAGE_PIN A14  IOSTANDARD LVCMOS33 } [get_ports {JB[0]}]
set_property -dict { PACKAGE_PIN A16  IOSTANDARD LVCMOS33 } [get_ports {JB[1]}]
set_property -dict { PACKAGE_PIN B15  IOSTANDARD LVCMOS33 } [get_ports {JB[2]}]
set_property -dict { PACKAGE_PIN B16  IOSTANDARD LVCMOS33 } [get_ports {JB[3]}]
set_property -dict { PACKAGE_PIN A15  IOSTANDARD LVCMOS33 } [get_ports {JB[4]}]
set_property -dict { PACKAGE_PIN A17  IOSTANDARD LVCMOS33 } [get_ports {JB[5]}]
set_property -dict { PACKAGE_PIN C15  IOSTANDARD LVCMOS33 } [get_ports {JB[6]}]

## Pmod JC1-4: PEMU inputs ui_in[1], ui_in[2], ui_in[3], ui_in[7]
## (pull-ups so an unconnected input reads as an idle UART line)
set_property -dict { PACKAGE_PIN K17  IOSTANDARD LVCMOS33 PULLTYPE PULLUP } [get_ports {JC[0]}]
set_property -dict { PACKAGE_PIN M18  IOSTANDARD LVCMOS33 PULLTYPE PULLUP } [get_ports {JC[1]}]
set_property -dict { PACKAGE_PIN N17  IOSTANDARD LVCMOS33 PULLTYPE PULLUP } [get_ports {JC[2]}]
set_property -dict { PACKAGE_PIN P18  IOSTANDARD LVCMOS33 PULLTYPE PULLUP } [get_ports {JC[3]}]

## Ethernet receive (PEMU ui_in[0]): differential input on the JXADC pair
## XA1_P (JXADC1, J3) / XA1_N (JXADC7, K3), bank 35 (3.3 V). LVDS_25 inputs
## are allowed in a 3.3 V bank when the internal termination is off, so the
## 100 ohm termination and the ~1.2 V common-mode bias are external
## (see docs/fpga.md).
set_property -dict { PACKAGE_PIN J3   IOSTANDARD LVDS_25 DIFF_TERM FALSE } [get_ports eth_rx_p]
set_property -dict { PACKAGE_PIN K3   IOSTANDARD LVDS_25 DIFF_TERM FALSE } [get_ports eth_rx_n]

## Ethernet transmit (copies of PEMU pins 0 and 1) on JXADC2 (XA2_P, L3) and
## JXADC8 (XA2_N, M3): the JXADC header has no 200 ohm series resistors.
set_property -dict { PACKAGE_PIN L3   IOSTANDARD LVCMOS33 DRIVE 16 SLEW FAST } [get_ports eth_td_p]
set_property -dict { PACKAGE_PIN M3   IOSTANDARD LVCMOS33 DRIVE 16 SLEW FAST } [get_ports eth_td_n]
