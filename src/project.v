/*
 * Copyright (c) 2026 Jedd Fadhley
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// Milestone 0: repeatedly transmit a hard-coded message over UART TX
// (115200 8N1 at a 50 MHz clock) on uo_out[4], the Tiny Tapeout
// convention for UART TX.
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

  localparam MSG_LEN = 15;  // "Hello, world!\r\n"

  reg  [3:0] idx;
  reg  [7:0] char;
  wire       busy;
  wire       tx;
  reg        start;

  always @(*) begin
    case (idx)
      4'd0:    char = "H";
      4'd1:    char = "e";
      4'd2:    char = "l";
      4'd3:    char = "l";
      4'd4:    char = "o";
      4'd5:    char = ",";
      4'd6:    char = " ";
      4'd7:    char = "w";
      4'd8:    char = "o";
      4'd9:    char = "r";
      4'd10:   char = "l";
      4'd11:   char = "d";
      4'd12:   char = "!";
      4'd13:   char = 8'h0D;
      default: char = 8'h0A;
    endcase
  end

  // Issue one start pulse per character, then advance once the
  // transmitter has picked it up.
  always @(posedge clk) begin
    if (!rst_n) begin
      idx   <= 0;
      start <= 1'b0;
    end else if (start) begin
      start <= 1'b0;
      idx   <= (idx == MSG_LEN - 1) ? 4'd0 : idx + 1'b1;
    end else if (!busy) begin
      start <= 1'b1;
    end
  end

  uart_tx #(
      .CLKS_PER_BIT(434)
  ) u_tx (
      .clk  (clk),
      .rst_n(rst_n),
      .start(start),
      .data (char),
      .tx   (tx),
      .busy (busy)
  );

  // All output pins must be assigned. If not used, assign to 0.
  assign uo_out  = {3'b000, tx, 4'b0000};
  assign uio_out = 0;
  assign uio_oe  = 0;

  // List all unused inputs to prevent warnings
  wire _unused = &{ena, ui_in, uio_in, 1'b0};

endmodule
