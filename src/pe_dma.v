/*
 * Copyright (c) 2026 Jedd Fadhley
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// Two DMA channels between the packet RAM and the engines' FIFOs. Channel c
// serves engine c: TX moves RAM words into the TX FIFO, RX moves RX FIFO
// words into the RAM. At most one transfer per clock, channel 0 first. A
// channel stands aside when the host uses the same FIFO (or, for RX, writes
// the RAM) in the same clock. See docs/isa.md section 3a.
module pe_dma (
    input  wire        clk,
    input  wire        rst_n,

    // FIFO levels at the start of the clock
    input  wire [2:0]  tx_level0,
    input  wire [2:0]  tx_level1,
    input  wire [2:0]  rx_level0,
    input  wire [2:0]  rx_level1,

    // Host activity in this clock
    input  wire        host_push0,    // write to engine 0's FIFO port
    input  wire        host_push1,
    input  wire        host_commit0,  // read commit at engine 0's FIFO port
    input  wire        host_commit1,
    input  wire        host_ram_we,   // write to RAM_DATA

    // Host writes to the DMA0/DMA1 registers
    input  wire        reg_we0,
    input  wire        reg_we1,
    input  wire [15:0] wdata,
    output reg  [15:0] dma0,
    output reg  [15:0] dma1,

    // Transfer for this clock
    output wire [6:0]  ram_addr,      // RAM word read (TX) or written (RX)
    output wire        ram_we,
    output wire        sel1,          // the transfer belongs to channel 1
    output wire        push0,
    output wire        push1,
    output wire        pop0,
    output wire        pop1
);

  wire       act0 = dma0[14:7] != 8'd0;
  wire       act1 = dma1[14:7] != 8'd0;
  wire       rx0  = dma0[15];
  wire       rx1  = dma1[15];

  wire ok0 = act0 && (rx0 ? (rx_level0 != 3'd0 && !host_commit0 && !host_ram_we)
                          : (tx_level0 != 3'd4 && !host_push0));
  wire ok1 = act1 && (rx1 ? (rx_level1 != 3'd0 && !host_commit1 && !host_ram_we)
                          : (tx_level1 != 3'd4 && !host_push1));

  wire go0 = ok0;
  wire go1 = ok1 && !ok0;

  assign sel1     = !ok0;
  assign ram_addr = sel1 ? dma1[6:0] : dma0[6:0];
  assign ram_we   = (go0 && rx0) || (go1 && rx1);
  assign push0    = go0 && !rx0;
  assign push1    = go1 && !rx1;
  assign pop0     = go0 && rx0;
  assign pop1     = go1 && rx1;

  // ADDR + 1 (mod 128), COUNT - 1 unless it is 255 (ring buffer), DIR kept
  function [15:0] advance(input [15:0] d);
    begin
      advance = {d[15], (d[14:7] == 8'd255) ? 8'd255 : d[14:7] - 8'd1, d[6:0] + 7'd1};
    end
  endfunction

  always @(posedge clk) begin
    if (!rst_n) begin
      dma0 <= 16'h0000;
      dma1 <= 16'h0000;
    end else begin
      if (reg_we0) dma0 <= wdata;
      else if (go0) dma0 <= advance(dma0);
      if (reg_we1) dma1 <= wdata;
      else if (go1) dma1 <= advance(dma1);
    end
  end

`ifdef FORMAL
  reg f_past_valid = 1'b0;
  always @(posedge clk) f_past_valid <= 1'b1;
  always @(*) if (!f_past_valid) assume (!rst_n);
  always @(*) begin
    assume (tx_level0 <= 3'd4 && tx_level1 <= 3'd4);
    assume (rx_level0 <= 3'd4 && rx_level1 <= 3'd4);
    assume (!(reg_we0 && reg_we1));

    // Never overflow or underflow a FIFO
    assert (!(push0 && tx_level0 == 3'd4) && !(push1 && tx_level1 == 3'd4));
    assert (!(pop0 && rx_level0 == 3'd0) && !(pop1 && rx_level1 == 3'd0));
    // Never collide with the host
    assert (!(push0 && host_push0) && !(push1 && host_push1));
    assert (!(pop0 && host_commit0) && !(pop1 && host_commit1));
    assert (!(ram_we && host_ram_we));
    // At most one transfer per clock, and only on an active channel
    assert ({1'b0, push0} + push1 + pop0 + pop1 <= 2'd1);
    if (push0 || pop0) assert (act0 && !sel1);
    if (push1 || pop1) assert (act1 && sel1);
    // A RAM write is exactly an RX transfer
    assert (ram_we == (pop0 || pop1));
    // Channel 0 has priority
    if (ok0) assert (!push1 && !pop1);
  end
  always @(posedge clk) begin
    if (f_past_valid && $past(rst_n) && rst_n) begin
      // Each transfer advances its channel by exactly one word
      if (!$past(reg_we0)) begin
        if ($past(push0 || pop0)) begin
          assert (dma0[14:7] == $past(dma0[14:7]) - {7'd0, $past(dma0[14:7]) != 8'd255});
          assert (dma0[6:0] == $past(dma0[6:0]) + 7'd1);
        end else begin
          assert (dma0 == $past(dma0));
        end
        assert (dma0[15] == $past(dma0[15]));
      end
      if (!$past(reg_we1)) begin
        if ($past(push1 || pop1)) begin
          assert (dma1[14:7] == $past(dma1[14:7]) - {7'd0, $past(dma1[14:7]) != 8'd255});
          assert (dma1[6:0] == $past(dma1[6:0]) + 7'd1);
        end else begin
          assert (dma1 == $past(dma1));
        end
        assert (dma1[15] == $past(dma1[15]));
      end
    end
  end
`endif

endmodule
