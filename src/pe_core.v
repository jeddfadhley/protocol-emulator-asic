/*
 * Copyright (c) 2026 Jedd Fadhley
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// PEMU core: 64-word instruction memory, two engines, pin registers, event
// flags and the host register bus. Matches sw/pemu/model.py (Core).
module pe_core (
    input  wire        clk,
    input  wire        rst_n,

    // External pins: [7:0] = uio_in, [15:8] = ui_in
    input  wire [15:0] pins_ext,
    output reg  [15:0] pin_out,
    output reg  [15:0] pin_oe,

    // Host register bus: at most one write, read load (re) or read commit
    // (rc) per clock
    input  wire        bus_we,
    input  wire        bus_re,
    input  wire        bus_rc,
    input  wire [6:0]  bus_addr,
    input  wire [15:0] bus_wdata,
    output reg  [15:0] bus_rdata
);

  localparam A_CTRL = 7'h60, A_FLAGS = 7'h61, A_PIN_IN = 7'h62, A_PIN_OUT = 7'h63,
             A_PIN_OE = 7'h64, A_ID = 7'h65, A_PIN_SET = 7'h66, A_PIN_CLR = 7'h67;
  localparam [15:0] CHIP_ID = 16'h5045;

  reg [15:0] imem[0:63];
  reg [2:0]  ctrl;  // [1:0] engine enables, [2] loopback
  reg [7:0]  flags;
  reg [15:0] sync1, sync2;

  wire loopback = ctrl[2];

  // What the engines see on their inputs
  wire [7:0]  lb_low = (pin_out[7:0] & pin_oe[7:0]) | ~pin_oe[7:0];
  wire [15:0] pin_in = loopback ? {pin_out[15:8], lb_low} : sync2;

  // Control pulses from a CTRL write
  wire       ctrl_w  = bus_we && bus_addr == A_CTRL;
  wire [1:0] restart = ctrl_w ? bus_wdata[5:4] : 2'b00;
  wire [1:0] step    = ctrl_w ? bus_wdata[7:6] : 2'b00;
  wire [1:0] flush   = ctrl_w ? bus_wdata[9:8] : 2'b00;

  // ---------------------------------------------------------------------
  // Engines

  wire        eng_blk = bus_addr[6:5] == 2'b10;  // 0x40-0x5F
  wire [1:0]  esel = {eng_blk && bus_addr[4], eng_blk && !bus_addr[4]};

  wire [5:0]  fetch0, fetch1;
  wire [15:0] rdata0, rdata1;
  wire [15:0] om0, ov0, em0, ev0, om1, ov1, em1, ev1;
  wire [7:0]  fs0, fc0, fs1, fc1;

  pe_engine u_e0 (
      .clk       (clk),
      .rst_n     (rst_n),
      .en        (ctrl[0]),
      .restart   (restart[0]),
      .step      (step[0]),
      .flush     (flush[0]),
      .sel       (esel[0]),
      .we        (bus_we),
      .re        (bus_re),
      .rc        (bus_rc),
      .off       (bus_addr[3:0]),
      .wdata     (bus_wdata),
      .rdata     (rdata0),
      .fetch_addr(fetch0),
      .instr     (imem[fetch0]),
      .pin_in    (pin_in),
      .flags     (flags),
      .out_mask  (om0),
      .out_val   (ov0),
      .oe_mask   (em0),
      .oe_val    (ev0),
      .flag_set  (fs0),
      .flag_clr  (fc0)
  );

  pe_engine u_e1 (
      .clk       (clk),
      .rst_n     (rst_n),
      .en        (ctrl[1]),
      .restart   (restart[1]),
      .step      (step[1]),
      .flush     (flush[1]),
      .sel       (esel[1]),
      .we        (bus_we),
      .re        (bus_re),
      .rc        (bus_rc),
      .off       (bus_addr[3:0]),
      .wdata     (bus_wdata),
      .rdata     (rdata1),
      .fetch_addr(fetch1),
      .instr     (imem[fetch1]),
      .pin_in    (pin_in),
      .flags     (flags),
      .out_mask  (om1),
      .out_val   (ov1),
      .oe_mask   (em1),
      .oe_val    (ev1),
      .flag_set  (fs1),
      .flag_clr  (fc1)
  );

  // ---------------------------------------------------------------------
  // Shared state: engine 0, then engine 1, then the host

  wire [15:0] out_e0 = (pin_out & ~om0) | (ov0 & om0);
  wire [15:0] out_e1 = (out_e0 & ~om1) | (ov1 & om1);
  wire [15:0] oe_e0  = (pin_oe & ~em0) | (ev0 & em0);
  wire [15:0] oe_e1  = (oe_e0 & ~em1) | (ev1 & em1);
  wire [7:0]  fl_e0  = (flags & ~fc0) | fs0;
  wire [7:0]  fl_e1  = (fl_e0 & ~fc1) | fs1;

  integer i;
  always @(posedge clk) begin
    if (!rst_n) begin
      ctrl    <= 3'b000;
      flags   <= 8'h00;
      pin_out <= 16'h0000;
      pin_oe  <= 16'h0000;
      sync1   <= 16'h0000;
      sync2   <= 16'h0000;
      for (i = 0; i < 64; i = i + 1) imem[i] <= 16'h0000;
    end else begin
      sync1   <= pins_ext;
      sync2   <= sync1;
      pin_out <= out_e1;
      pin_oe  <= oe_e1;
      flags   <= fl_e1;
      if (bus_we) begin
        if (!bus_addr[6]) imem[bus_addr[5:0]] <= bus_wdata;
        case (bus_addr)
          A_CTRL:    ctrl <= bus_wdata[2:0];
          A_FLAGS:   flags <= bus_wdata[7:0];
          A_PIN_OUT: pin_out <= bus_wdata;
          A_PIN_OE:  pin_oe <= bus_wdata;
          A_PIN_SET: pin_out <= out_e1 | bus_wdata;
          A_PIN_CLR: pin_out <= out_e1 & ~bus_wdata;
          default: ;
        endcase
      end
    end
  end

  // ---------------------------------------------------------------------
  // Host reads

  always @(*) begin
    if (!bus_addr[6]) begin
      bus_rdata = imem[bus_addr[5:0]];
    end else if (eng_blk) begin
      bus_rdata = bus_addr[4] ? rdata1 : rdata0;
    end else begin
      case (bus_addr)
        A_CTRL:    bus_rdata = {13'd0, ctrl};
        A_FLAGS:   bus_rdata = {8'd0, flags};
        A_PIN_IN:  bus_rdata = pin_in;
        A_PIN_OUT: bus_rdata = pin_out;
        A_PIN_OE:  bus_rdata = pin_oe;
        A_ID:      bus_rdata = CHIP_ID;
        default:   bus_rdata = 16'h0000;
      endcase
    end
  end

endmodule
