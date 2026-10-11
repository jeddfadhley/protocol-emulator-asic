/*
 * Copyright (c) 2026 Jedd Fadhley
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// PEMU on a Digilent Basys 3 (Artix-7 xc7a35tcpg236-1).
//
// The PC talks to PEMU through the board's USB-UART (1,000,000 baud 8N1)
// and a UART-to-SPI bridge; see docs/fpga.md.
//
//   PEMU pin  chip signal   Basys 3
//   0-7       uio[0..7]     Pmod JA1-4, JA7-10 (bidirectional, pull-ups)
//   8-14      uo_out[0..6]  Pmod JB1-4, JB7-9  (outputs)
//   in 8      ui_in[0]      JXADC1/JXADC7 pair (XA1_P J3 / XA1_N K3), differential input
//   0, 1      uio[0], uio[1] also on JXADC2 / JXADC8 (L3 / M3): Ethernet TD+/TD-
//                           without the 200 ohm Pmod series resistors
//   in 9-11   ui_in[1..3]   Pmod JC1-3
//   in 15     ui_in[7]      Pmod JC4
//   LED[7:0]  levels of PEMU pins 0-7, LED[14:8] = uo_out[6:0], LED[15] = UART activity
//   btnC      reset
module pemu_basys3 (
    input  wire        clk,       // 100 MHz, W5
    input  wire        btnC,      // reset, active high
    input  wire        RsRx,      // UART from the PC
    output wire        RsTx,      // UART to the PC
    inout  wire [7:0]  JA,
    output wire [6:0]  JB,
    input  wire [3:0]  JC,
    input  wire        eth_rx_p,  // XA1_P
    input  wire        eth_rx_n,  // XA1_N
    output wire        eth_td_p,  // XA2_P: copy of PEMU pin 0
    output wire        eth_td_n,  // XA2_N: copy of PEMU pin 1
    output wire [15:0] led
);

  // ---------------- 100 MHz -> 50 MHz ----------------
  wire clk_fb, clk50_unbuf, clk50, pll_locked;

  // VCO = 100 MHz * 10 = 1000 MHz; CLKOUT0 = 1000 / 20 = 50 MHz.
  PLLE2_ADV #(
      .BANDWIDTH         ("OPTIMIZED"),
      .COMPENSATION      ("ZHOLD"),
      .CLKIN1_PERIOD     (10.0),
      .DIVCLK_DIVIDE     (1),
      .CLKFBOUT_MULT     (10),
      .CLKFBOUT_PHASE    (0.0),
      .CLKOUT0_DIVIDE    (20),
      .CLKOUT0_PHASE     (0.0),
      .CLKOUT0_DUTY_CYCLE(0.5),
      .STARTUP_WAIT      ("FALSE")
  ) u_pll (
      .CLKIN1  (clk),
      .CLKIN2  (1'b0),
      .CLKINSEL(1'b1),
      .CLKFBIN (clk_fb),
      .CLKFBOUT(clk_fb),
      .CLKOUT0 (clk50_unbuf),
      .CLKOUT1 (),
      .CLKOUT2 (),
      .CLKOUT3 (),
      .CLKOUT4 (),
      .CLKOUT5 (),
      .LOCKED  (pll_locked),
      .PWRDWN  (1'b0),
      .RST     (1'b0),
      .DADDR   (7'd0),
      .DCLK    (1'b0),
      .DEN     (1'b0),
      .DI      (16'd0),
      .DWE     (1'b0),
      .DO      (),
      .DRDY    ()
  );

  BUFG u_bufg (.I(clk50_unbuf), .O(clk50));

  // ---------------- I/O buffers ----------------
  wire [7:0] uio_in, uio_out, uio_oe;
  wire [7:0] uo_out;
  wire       eth_rx;

  genvar i;
  generate
    for (i = 0; i < 8; i = i + 1) begin : g_ja
      IOBUF u_iobuf (.I(uio_out[i]), .T(~uio_oe[i]), .O(uio_in[i]), .IO(JA[i]));
    end
  endgenerate

  IBUFDS #(.DIFF_TERM("FALSE"), .IBUF_LOW_PWR("FALSE")) u_eth_rx (
      .I(eth_rx_p), .IB(eth_rx_n), .O(eth_rx));

  assign JB = uo_out[6:0];

  // Ethernet transmit pair: PEMU pins 0 and 1 again, released together with
  // them (released = 0 V across the transformer = 10BASE-T idle)
  OBUFT u_td_p (.I(uio_out[0]), .T(~uio_oe[0]), .O(eth_td_p));
  OBUFT u_td_n (.I(uio_out[1]), .T(~uio_oe[1]), .O(eth_td_n));

  // ---------------- PEMU + bridge ----------------
  wire uart_activity;

  pemu_fpga_top #(
      .CLK_HZ(50_000_000),
      .BAUD  (1_000_000)
  ) u_top (
      .clk          (clk50),
      .rst_n_in     (pll_locked & ~btnC),
      .uart_rx      (RsRx),
      .uart_tx      (RsTx),
      .uart_activity(uart_activity),
      .ui_ext       ({JC[3], 3'b111, JC[2:0], eth_rx}),
      .uo_out       (uo_out),
      .uio_in       (uio_in),
      .uio_out      (uio_out),
      .uio_oe       (uio_oe)
  );

  // ---------------- LEDs ----------------
  // LED15 stays lit for ~40 ms after each UART byte so traffic is visible.
  reg [20:0] blink = 21'd0;
  always @(posedge clk50) begin
    if (uart_activity)   blink <= 21'h1FFFFF;
    else if (blink != 0) blink <= blink - 21'd1;
  end

  assign led[7:0]  = uio_in;
  assign led[14:8] = uo_out[6:0];
  assign led[15]   = blink != 0;

endmodule
