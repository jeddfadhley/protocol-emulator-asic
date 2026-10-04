/*
 * Copyright (c) 2026 Jedd Fadhley
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// 8N1 UART transmitter. Assert `start` for one cycle while `busy` is low to
// send `data`. `tx` idles high.
module uart_tx #(
    parameter CLKS_PER_BIT = 434  // 50 MHz / 115200 baud
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       start,
    input  wire [7:0] data,
    output reg        tx,
    output wire       busy
);

  localparam DIV_W = $clog2(CLKS_PER_BIT);

  reg [DIV_W-1:0] div;
  reg [3:0]       bit_idx;  // 0 = start bit, 1..8 = data, 9 = stop bit
  reg [7:0]       shift;
  reg             active;

  assign busy = active;

  always @(posedge clk) begin
    if (!rst_n) begin
      tx      <= 1'b1;
      div     <= 0;
      bit_idx <= 0;
      shift   <= 0;
      active  <= 1'b0;
    end else if (!active) begin
      tx <= 1'b1;
      if (start) begin
        active  <= 1'b1;
        shift   <= data;
        bit_idx <= 0;
        div     <= 0;
        tx      <= 1'b0;  // start bit
      end
    end else if (div != CLKS_PER_BIT[DIV_W-1:0] - 1'b1) begin
      div <= div + 1'b1;
    end else begin
      div <= 0;
      if (bit_idx == 4'd9) begin
        active <= 1'b0;
        tx     <= 1'b1;
      end else begin
        bit_idx <= bit_idx + 1'b1;
        if (bit_idx == 4'd8) begin
          tx <= 1'b1;  // stop bit
        end else begin
          tx    <= shift[0];  // LSB first
          shift <= {1'b0, shift[7:1]};
        end
      end
    end
  end

endmodule
