/*
 * Copyright (c) 2026 Jedd Fadhley
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// 4-entry, 16-bit FIFO with a combinational head. The caller must not push
// when full or pop when empty; flush wins over both.
module pe_fifo (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        flush,
    input  wire        push,
    input  wire [15:0] din,
    input  wire        pop,
    output wire [15:0] head,
    output reg  [2:0]  count
);

  reg [15:0] mem[0:3];
  reg [1:0]  rptr;
  reg [1:0]  wptr;

  assign head = mem[rptr];

  always @(posedge clk) begin
    if (!rst_n || flush) begin
      rptr  <= 2'd0;
      wptr  <= 2'd0;
      count <= 3'd0;
    end else begin
      if (push) begin
        wptr <= wptr + 2'd1;
      end
      if (pop) begin
        rptr <= rptr + 2'd1;
      end
      count <= count + {2'b00, push} - {2'b00, pop};
    end
  end

  // Storage is reset too, so gate-level simulation never sees X data.
  integer i;
  always @(posedge clk) begin
    if (!rst_n) begin
      for (i = 0; i < 4; i = i + 1) mem[i] <= 16'h0000;
    end else if (push && !flush) begin
      mem[wptr] <= din;
    end
  end

`ifdef FORMAL
  reg f_past_valid = 1'b0;
  always @(posedge clk) f_past_valid <= 1'b1;
  always @(*) if (!f_past_valid) assume (!rst_n);
  // The caller never overflows or underflows us: assumed when this module is
  // checked alone, proven when it is checked inside pe_engine.
`ifdef FORMAL_FIFO_TOP
  always @(*) assume (!(push && count == 3'd4));
  always @(*) assume (!(pop && count == 3'd0));
`else
  always @(*) assert (!(push && count == 3'd4));
  always @(*) assert (!(pop && count == 3'd0));
`endif
  always @(*) begin
    if (f_past_valid) begin
      assert (count <= 3'd4);
      assert (wptr - rptr == count[1:0]);
    end
  end
  // Data comes out in the order it went in: track one word through
  (* anyconst *) reg [15:0] f_word;
  reg       f_tracking;
  reg [2:0] f_ahead;  // words in front of the tracked one
  initial f_tracking = 1'b0;
  always @(posedge clk) begin
    if (!rst_n || flush) begin
      f_tracking <= 1'b0;
    end else if (!f_tracking && push && din == f_word) begin
      f_tracking <= 1'b1;
      f_ahead <= count - {2'b00, pop};
    end else if (f_tracking && pop) begin
      if (f_ahead == 3'd0) f_tracking <= 1'b0;
      else f_ahead <= f_ahead - 3'd1;
    end
  end
  always @(*) begin
    if (f_past_valid && f_tracking) begin
      assert (f_ahead < count);
      assert (mem[rptr + f_ahead[1:0]] == f_word);  // induction strengthening
      if (f_ahead == 3'd0) assert (head == f_word);
    end
  end
`endif

endmodule
