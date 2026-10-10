/*
 * Copyright (c) 2026 Jedd Fadhley
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// PEMU: a programmable protocol emulator. Two engines run firmware that
// drives and samples pins with cycle-exact timing. A host loads programs
// and moves data over SPI. See docs/isa.md.
//
// Pins:
//   uio[7:0]   engine pins 0-7 (bidirectional)
//   uo_out[6:0] engine pins 8-14 (outputs)
//   ui_in[7:0]  engine pins 8-15 (inputs)
//   ui_in[4] host CS_N, ui_in[5] host SCK, ui_in[6] host MOSI, uo_out[7] host MISO
module tt_um_jeddfadhley_protocol_emu (
    input  wire [7:0] ui_in,    // Dedicated inputs
    output wire [7:0] uo_out,   // Dedicated outputs
    input  wire [7:0] uio_in,   // IOs: Input path
    output wire [7:0] uio_out,  // IOs: Output path
    output wire [7:0] uio_oe,   // IOs: Enable path (active high: 0=input, 1=output)
    input  wire       ena,      // always 1 when the design is powered, so you can ignore it
    input  wire       clk,      // clock
    input  wire       rst_n     // reset_n - low to reset
);

  wire        bus_we, bus_re, bus_rc;
  wire [6:0]  bus_addr;
  wire [15:0] bus_wdata, bus_rdata;
  wire [15:0] pin_out, pin_oe;
  wire        miso;

  pe_spi u_spi (
      .clk      (clk),
      .rst_n    (rst_n),
      .cs_n_in  (ui_in[4]),
      .sck_in   (ui_in[5]),
      .mosi_in  (ui_in[6]),
      .miso     (miso),
      .bus_we   (bus_we),
      .bus_re   (bus_re),
      .bus_rc   (bus_rc),
      .bus_addr (bus_addr),
      .bus_wdata(bus_wdata),
      .bus_rdata(bus_rdata)
  );

  pe_core u_core (
      .clk      (clk),
      .rst_n    (rst_n),
      .pins_ext ({ui_in, uio_in}),
      .pin_out  (pin_out),
      .pin_oe   (pin_oe),
      .bus_we   (bus_we),
      .bus_re   (bus_re),
      .bus_rc   (bus_rc),
      .bus_addr (bus_addr),
      .bus_wdata(bus_wdata),
      .bus_rdata(bus_rdata)
  );

  assign uo_out  = {miso, pin_out[14:8]};
  assign uio_out = pin_out[7:0];
  assign uio_oe  = pin_oe[7:0];

  wire _unused = &{ena, pin_out[15], pin_oe[15:8], 1'b0};

endmodule
