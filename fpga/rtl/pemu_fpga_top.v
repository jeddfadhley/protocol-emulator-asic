/*
 * Copyright (c) 2026 Jedd Fadhley
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// Portable FPGA wrapper: the unmodified PEMU chip plus a UART-to-SPI bridge,
// so a PC can drive PEMU's SPI host port over a serial cable.
//
//   PC --UART--> uart_spi_bridge --SPI (ui_in[4..6], uo_out[7])--> PEMU
//
// The board-specific top adds the clock, I/O buffers and LEDs.
module pemu_fpga_top #(
    parameter integer CLK_HZ       = 50_000_000,
    parameter integer BAUD         = 1_000_000,
    parameter integer SCK_HALF     = 5,
    parameter integer TIMEOUT_CLKS = CLK_HZ / 2
) (
    input  wire       clk,
    input  wire       rst_n_in,    // asynchronous, active-low board reset

    input  wire       uart_rx,     // from the PC
    output wire       uart_tx,     // to the PC
    output wire       uart_activity,

    // PEMU pins, as on the chip. ui_in[4..6] are driven by the bridge, so
    // only ui_ext[3:0] and ui_ext[7] are used.
    input  wire [7:0] ui_ext,
    output wire [7:0] uo_out,      // uo_out[7] is MISO
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe
);

  // Power-on reset: held for 256 clocks after configuration and whenever
  // rst_n_in is low (synchronized, released synchronously).
  reg [1:0] rst_sync = 2'b00;
  reg [7:0] por_cnt  = 8'd0;
  reg       rst_n    = 1'b0;

  always @(posedge clk or negedge rst_n_in) begin
    if (!rst_n_in) rst_sync <= 2'b00;
    else           rst_sync <= {rst_sync[0], 1'b1};
  end

  always @(posedge clk) begin
    if (!rst_sync[1]) begin
      por_cnt <= 8'd0;
      rst_n   <= 1'b0;
    end else if (por_cnt != 8'hFF) begin
      por_cnt <= por_cnt + 8'd1;
      rst_n   <= 1'b0;
    end else begin
      rst_n   <= 1'b1;
    end
  end

  wire cs_n, sck, mosi, bridge_rst_n;

  uart_spi_bridge #(
      .CLK_HZ      (CLK_HZ),
      .BAUD        (BAUD),
      .SCK_HALF    (SCK_HALF),
      .TIMEOUT_CLKS(TIMEOUT_CLKS)
  ) u_bridge (
      .clk       (clk),
      .rst_n     (rst_n),
      .uart_rx   (uart_rx),
      .uart_tx   (uart_tx),
      .spi_cs_n  (cs_n),
      .spi_sck   (sck),
      .spi_mosi  (mosi),
      .spi_miso  (uo_out[7]),
      .pemu_rst_n(bridge_rst_n),
      .activity  (uart_activity)
  );

  reg pemu_rst_n = 1'b0;
  always @(posedge clk) pemu_rst_n <= rst_n & bridge_rst_n;

  tt_um_jeddfadhley_protocol_emu u_pemu (
      .ui_in  ({ui_ext[7], mosi, sck, cs_n, ui_ext[3:0]}),
      .uo_out (uo_out),
      .uio_in (uio_in),
      .uio_out(uio_out),
      .uio_oe (uio_oe),
      .ena    (1'b1),
      .clk    (clk),
      .rst_n  (pemu_rst_n)
  );

  wire _unused = &{ui_ext[6:4], 1'b0};

endmodule
