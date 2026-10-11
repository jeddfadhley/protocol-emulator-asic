/*
 * Copyright (c) 2026 Jedd Fadhley
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// PEMU core: 64-word instruction memory, two engines, pin registers, event
// flags, the packet RAM with its DMA channels and the host register bus.
// Matches sw/pemu/model.py (Core).
module pe_core (
    input  wire        clk,
    input  wire        rst_n,

    // External pins: [7:0] = uio_in, [15:8] = ui_in
    input  wire [15:0] pins_ext,
    output reg  [15:0] pin_out,   // architectural PIN_OUT
    output wire [15:0] pin_pad,   // what the pads drive (late pins half a clock later)
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
             A_PIN_OE = 7'h64, A_ID = 7'h65, A_PIN_SET = 7'h66, A_PIN_CLR = 7'h67,
             A_RAM_ADDR = 7'h68, A_RAM_DATA = 7'h69, A_DMA0 = 7'h6A, A_DMA1 = 7'h6B,
             A_FLAG_SET = 7'h6C, A_FLAG_CLR = 7'h6D, A_VERSION = 7'h6E, A_PIN_LATE = 7'h6F;
  localparam [15:0] CHIP_ID = 16'h5045, VERSION = 16'h0002;

  reg [15:0] imem[0:63];
  reg [15:0] pram[0:127];  // packet RAM
  reg [6:0]  ram_addr;     // host port address
  reg [2:0]  ctrl;  // [1:0] engine enables, [2] loopback
  reg [7:0]  flags;
  reg [15:0] sync1, sync2;
  reg [15:0] pin_late;  // pins whose last write was "late"
  reg [15:0] pin_neg;   // PIN_OUT captured on the falling clock edge

  // Half-clock pin timing: a pin written late shows its new value from the
  // falling edge after the write. The switch between pin_out and pin_neg
  // happens when both hold the same value, so it never glitches.
  always @(negedge clk) pin_neg <= pin_out;
  assign pin_pad = (pin_late & pin_neg) | (~pin_late & pin_out);

  wire loopback = ctrl[2];

  // What the engines see on their inputs
  wire [7:0]  lb_low = (pin_out[7:0] & pin_oe[7:0]) | ~pin_oe[7:0];
  wire [15:0] pin_in = loopback ? {pin_out[15:8], lb_low} : sync2;

  // Control pulses from a CTRL write act on the next clock. Registering
  // them keeps the host bus decode out of the engines' timing paths.
  wire       ctrl_w  = bus_we && bus_addr == A_CTRL;
  reg  [1:0] restart, step, flush;
  always @(posedge clk) begin
    if (!rst_n) begin
      restart <= 2'b00;
      step    <= 2'b00;
      flush   <= 2'b00;
    end else begin
      restart <= ctrl_w ? bus_wdata[5:4] : 2'b00;
      step    <= ctrl_w ? bus_wdata[7:6] : 2'b00;
      flush   <= ctrl_w ? bus_wdata[9:8] : 2'b00;
    end
  end

  // ---------------------------------------------------------------------
  // Engines

  wire        eng_blk = bus_addr[6:5] == 2'b10;  // 0x40-0x5F
  wire [1:0]  esel = {eng_blk && bus_addr[4], eng_blk && !bus_addr[4]};

  wire [5:0]  fetch0, fetch1;
  wire [15:0] rdata0, rdata1;
  wire [15:0] om0, ov0, em0, ev0, om1, ov1, em1, ev1, lv0, lv1;
  wire [7:0]  fs0, fc0, fs1, fc1;
  wire [2:0]  txl0, txl1, rxl0, rxl1;
  wire [15:0] rxw0, rxw1;

  // ---------------------------------------------------------------------
  // DMA

  wire        host_push0   = bus_we && bus_addr == 7'h4F;
  wire        host_push1   = bus_we && bus_addr == 7'h5F;
  wire        host_commit0 = bus_rc && bus_addr == 7'h4F;
  wire        host_commit1 = bus_rc && bus_addr == 7'h5F;
  wire        host_ram_we  = bus_we && bus_addr == A_RAM_DATA;
  wire [15:0] dma0, dma1;
  wire [6:0]  dma_addr;
  wire        dma_ram_we, dma_sel1, dpush0, dpush1, dpop0, dpop1;
  wire [15:0] dma_rdata = pram[dma_addr];

  pe_dma u_dma (
      .clk         (clk),
      .rst_n       (rst_n),
      .tx_level0   (txl0),
      .tx_level1   (txl1),
      .rx_level0   (rxl0),
      .rx_level1   (rxl1),
      .host_push0  (host_push0),
      .host_push1  (host_push1),
      .host_commit0(host_commit0),
      .host_commit1(host_commit1),
      .host_ram_we (host_ram_we),
      .reg_we0     (bus_we && bus_addr == A_DMA0),
      .reg_we1     (bus_we && bus_addr == A_DMA1),
      .wdata       (bus_wdata),
      .dma0        (dma0),
      .dma1        (dma1),
      .ram_addr    (dma_addr),
      .ram_we      (dma_ram_we),
      .sel1        (dma_sel1),
      .push0       (dpush0),
      .push1       (dpush1),
      .pop0        (dpop0),
      .pop1        (dpop1)
  );

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
      .late_val  (lv0),
      .oe_mask   (em0),
      .oe_val    (ev0),
      .flag_set  (fs0),
      .flag_clr  (fc0),
      .dma_push  (dpush0),
      .dma_wdata (dma_rdata),
      .dma_pop   (dpop0),
      .tx_level  (txl0),
      .rx_level  (rxl0),
      .rx_word   (rxw0)
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
      .late_val  (lv1),
      .oe_mask   (em1),
      .oe_val    (ev1),
      .flag_set  (fs1),
      .flag_clr  (fc1),
      .dma_push  (dpush1),
      .dma_wdata (dma_rdata),
      .dma_pop   (dpop1),
      .tx_level  (txl1),
      .rx_level  (rxl1),
      .rx_word   (rxw1)
  );

  // ---------------------------------------------------------------------
  // Shared state: engine 0, then engine 1, then the host

  wire [15:0] out_e0 = (pin_out & ~om0) | (ov0 & om0);
  wire [15:0] out_e1 = (out_e0 & ~om1) | (ov1 & om1);
  wire [15:0] oe_e0  = (pin_oe & ~em0) | (ev0 & em0);
  wire [15:0] oe_e1  = (oe_e0 & ~em1) | (ev1 & em1);
  wire [15:0] lt_e0  = (pin_late & ~om0) | (lv0 & om0);
  wire [15:0] lt_e1  = (lt_e0 & ~om1) | (lv1 & om1);
  wire [7:0]  fl_e0  = (flags & ~fc0) | fs0;
  wire [7:0]  fl_e1  = (fl_e0 & ~fc1) | fs1;

  always @(posedge clk) begin
    if (!rst_n) begin
      ctrl    <= 3'b000;
      flags   <= 8'h00;
      pin_out <= 16'h0000;
      pin_oe  <= 16'h0000;
      pin_late <= 16'h0000;
      sync1   <= 16'h0000;
      sync2   <= 16'h0000;
      ram_addr <= 7'd0;
    end else begin
      sync1   <= pins_ext;
      sync2   <= sync1;
      pin_out <= out_e1;
      pin_late <= lt_e1;
      pin_oe  <= oe_e1;
      flags   <= fl_e1;
      if (bus_we) begin
        case (bus_addr)
          A_CTRL:    ctrl <= bus_wdata[2:0];
          A_FLAGS:   flags <= bus_wdata[7:0];
          A_PIN_OUT: begin
            pin_out  <= bus_wdata;
            pin_late <= 16'h0000;
          end
          A_PIN_OE:  pin_oe <= bus_wdata;
          A_PIN_SET: begin
            pin_out  <= out_e1 | bus_wdata;
            pin_late <= lt_e1 & ~bus_wdata;
          end
          A_PIN_CLR: begin
            pin_out  <= out_e1 & ~bus_wdata;
            pin_late <= lt_e1 & ~bus_wdata;
          end
          A_FLAG_SET: flags <= fl_e1 | bus_wdata[7:0];
          A_FLAG_CLR: flags <= fl_e1 & ~bus_wdata[7:0];
          A_RAM_ADDR: ram_addr <= bus_wdata[6:0];
          A_RAM_DATA: ram_addr <= ram_addr + 7'd1;
          default: ;
        endcase
      end
      if (bus_rc && bus_addr == A_RAM_DATA) ram_addr <= ram_addr + 7'd1;
    end
  end

  // Memories. The instruction memory is written by the host; the packet
  // RAM by the host port or an RX transfer (pe_dma never does both in the
  // same clock). On the chip both are flip-flops cleared by reset. FPGA
  // builds define PEMU_LUTRAM: no reset, so they map to LUT RAM, and they
  // start at zero from the bitstream.
`ifdef PEMU_LUTRAM
  integer k;
  initial begin
    for (k = 0; k < 64; k = k + 1) imem[k] = 16'h0000;
    for (k = 0; k < 128; k = k + 1) pram[k] = 16'h0000;
  end
  always @(posedge clk) begin
    if (rst_n && bus_we && !bus_addr[6]) imem[bus_addr[5:0]] <= bus_wdata;
  end
  always @(posedge clk) begin
    if (rst_n && host_ram_we) pram[ram_addr] <= bus_wdata;
    else if (rst_n && dma_ram_we) pram[dma_addr] <= dma_sel1 ? rxw1 : rxw0;
  end
`else
  integer ki, kp;  // one loop variable per always block (Yosys check)
  always @(posedge clk) begin
    if (!rst_n) begin
      for (ki = 0; ki < 64; ki = ki + 1) imem[ki] <= 16'h0000;
    end else if (bus_we && !bus_addr[6]) begin
      imem[bus_addr[5:0]] <= bus_wdata;
    end
  end
  always @(posedge clk) begin
    if (!rst_n) begin
      for (kp = 0; kp < 128; kp = kp + 1) pram[kp] <= 16'h0000;
    end else if (host_ram_we) begin
      pram[ram_addr] <= bus_wdata;
    end else if (dma_ram_we) begin
      pram[dma_addr] <= dma_sel1 ? rxw1 : rxw0;
    end
  end
`endif

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
        A_RAM_ADDR: bus_rdata = {9'd0, ram_addr};
        A_RAM_DATA: bus_rdata = pram[ram_addr];
        A_DMA0:    bus_rdata = dma0;
        A_DMA1:    bus_rdata = dma1;
        A_VERSION: bus_rdata = VERSION;
        A_PIN_LATE: bus_rdata = pin_late;
        default:   bus_rdata = 16'h0000;
      endcase
    end
  end

endmodule
