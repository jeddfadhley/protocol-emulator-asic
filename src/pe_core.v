/*
 * Copyright (c) 2026 Jedd Fadhley
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// PEMU core: two engines, each with its own 64-word instruction memory, pin
// registers, event flags, the packet RAM with its DMA channels and the host
// register bus.
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
             A_FLAG_SET = 7'h6C, A_FLAG_CLR = 7'h6D, A_VERSION = 7'h6E, A_PIN_LATE = 7'h6F,
             A_IMEM_SEL = 7'h70;
  localparam [15:0] CHIP_ID = 16'h5045, VERSION = 16'h0004;

  // One instruction memory per engine, each read only by its engine's fetch.
  // The host writes them through the 0x00-0x3F window (IMEM_SEL picks which)
  // and cannot read them back: a host read port would be a third
  // 64-to-1 multiplexer, and on this process wiring is the scarce resource.
  // The memories themselves are at the end of this module.
  wire [15:0] instr0, instr1;  // each engine's fetched instruction
  reg [6:0]  ram_addr;     // host port address
  reg [2:0]  ctrl;  // [1:0] engine enables, [2] loopback
  reg [1:0]  imem_sel;  // which memories a host write reaches: [0] engine 0, [1] engine 1
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
  // The packet RAM has one read port. A host read of RAM_DATA takes it for
  // that clock and a TX transfer stands aside (pe_dma, host_ram_re).
  wire        host_ram_re  = bus_re && bus_addr == A_RAM_DATA;
  wire [15:0] pram_rdata;
  wire [15:0] dma_rdata    = pram_rdata;

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
      .host_ram_re (host_ram_re),
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
      .instr     (instr0),
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
      .instr     (instr1),
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
      imem_sel <= 2'b11;
      flags   <= 8'h00;
      // Strap (v4): ui_in[7] high during reset starts the dedicated
      // outputs uo_out[6:0] (pins 8-14) high, the idle level of most
      // buses (UART, CAN TXD, chip selects). Low keeps them at 0.
      pin_out <= {1'b0, {7{pins_ext[15]}}, 8'h00};
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
          A_IMEM_SEL: imem_sel <= bus_wdata[1:0];
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

  // Memories. The instruction memories are written by the host; the packet
  // RAM by the host port or an RX transfer (pe_dma never does both in the
  // same clock). Each memory has one read port: an engine's fetch, or for
  // the packet RAM the host (a RAM_DATA read) or else the DMA.
  //
  // On the chip they are latches (pe_ram below), cleared by reset. FPGA
  // builds define PEMU_LUTRAM: plain arrays with no reset, so they map to LUT
  // RAM, and they start at zero from the bitstream. Both behave the same
  // clock by clock: a write in one clock is seen by reads in the next.
  wire        imem_we   = bus_we && !bus_addr[6];
  wire        pram_we   = host_ram_we || dma_ram_we;
  wire [6:0]  pram_wa   = host_ram_we ? ram_addr : dma_addr;
  wire [15:0] pram_wd   = host_ram_we ? bus_wdata : dma_sel1 ? rxw1 : rxw0;
  wire [6:0]  pram_ra   = host_ram_re ? ram_addr : dma_addr;
`ifdef PEMU_LUTRAM
  reg [15:0] imem0[0:63];
  reg [15:0] imem1[0:63];
  reg [15:0] pram[0:127];
  integer k;
  initial begin
    for (k = 0; k < 64; k = k + 1) imem0[k] = 16'h0000;
    for (k = 0; k < 64; k = k + 1) imem1[k] = 16'h0000;
    for (k = 0; k < 128; k = k + 1) pram[k] = 16'h0000;
  end
  always @(posedge clk) begin
    if (rst_n && imem_we && imem_sel[0]) imem0[bus_addr[5:0]] <= bus_wdata;
  end
  always @(posedge clk) begin
    if (rst_n && imem_we && imem_sel[1]) imem1[bus_addr[5:0]] <= bus_wdata;
  end
  always @(posedge clk) begin
    if (rst_n && pram_we) pram[pram_wa] <= pram_wd;
  end
  assign instr0     = imem0[fetch0];
  assign instr1     = imem1[fetch1];
  assign pram_rdata = pram[pram_ra];
`else
  pe_ram #(.AW(6)) u_imem0 (
      .clk(clk), .rst_n(rst_n), .we(imem_we && imem_sel[0]),
      .waddr(bus_addr[5:0]), .wdata(bus_wdata), .raddr(fetch0), .rdata(instr0));
  pe_ram #(.AW(6)) u_imem1 (
      .clk(clk), .rst_n(rst_n), .we(imem_we && imem_sel[1]),
      .waddr(bus_addr[5:0]), .wdata(bus_wdata), .raddr(fetch1), .rdata(instr1));
  // The packet RAM's write request depends on the DMA's choice of channel,
  // which is too slow for the half clock pe_ram allows (-3.6 ns at the slow
  // corner). So the request is registered first, and stored a clock later.
  // Until then a read of that word is answered from the register, so reads
  // still see a write on the next clock.
  reg        pq_we;
  reg [6:0]  pq_wa;
  reg [15:0] pq_wd;
  wire [15:0] pram_lat;
  always @(posedge clk) begin
    pq_we <= pram_we && rst_n;
    pq_wa <= pram_wa;
    pq_wd <= pram_wd;
  end
  pe_ram #(.AW(7)) u_pram (
      .clk(clk), .rst_n(rst_n), .we(pq_we),
      .waddr(pq_wa), .wdata(pq_wd), .raddr(pram_ra), .rdata(pram_lat));
  assign pram_rdata = (pq_we && pq_wa == pram_ra) ? pq_wd : pram_lat;
`endif

  // ---------------------------------------------------------------------
  // Host reads

  always @(*) begin
    if (!bus_addr[6]) begin
      bus_rdata = 16'h0000;  // instruction memory is write-only
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
        A_RAM_DATA: bus_rdata = pram_rdata;
        A_DMA0:    bus_rdata = dma0;
        A_DMA1:    bus_rdata = dma1;
        A_VERSION: bus_rdata = VERSION;
        A_PIN_LATE: bus_rdata = pin_late;
        A_IMEM_SEL: bus_rdata = {14'd0, imem_sel};
        default:   bus_rdata = 16'h0000;
      endcase
    end
  end

endmodule


// Latch memory: 2^AW words of 16 bits with one write port and one read port.
//
// A latch is about half the size of a flip-flop and needs no logic to hold
// its value, so this cuts the memories' share of the chip by more than a
// third, and with it the wiring that made the layout too slow to route.
//
// A write requested in clock t (we, waddr, wdata come from rising-edge
// registers and are steady through the clock):
//   - the falling edge in clock t sets the word's flag sel_n;
//   - the rising edge that ends clock t copies it to sel_p and captures the
//     data in wd_p;
//   - the word's latches are open while sel_p AND sel_n: the first (high)
//     half of clock t+1. They close at its falling edge, half a clock before
//     wd_p can change;
//   - reads in clock t+1 see the new value, as with flip-flops.
// The gate is an AND of two flip-flop outputs that change half a clock
// apart, so it cannot glitch, and no clock passes through logic. This is
// the scheme of Michael Bell's TT06 latch RAM, with a flag pair per word so
// that writes may come in back-to-back clocks (the RX DMA does that).
module pe_ram #(
    parameter AW = 6
) (
    input  wire          clk,
    input  wire          rst_n,  // clears every word while low
    input  wire          we,
    input  wire [AW-1:0] waddr,
    input  wire [15:0]   wdata,
    input  wire [AW-1:0] raddr,
    output wire [15:0]   rdata
);
  localparam WORDS = 1 << AW;

  reg [WORDS-1:0] sel_n, sel_p;
  reg [15:0]      wd_p;
  always @(negedge clk) begin
    sel_n <= (we && rst_n) ? {{(WORDS - 1) {1'b0}}, 1'b1} << waddr : {WORDS{1'b0}};
  end
  always @(posedge clk) begin
    sel_p <= sel_n;
    wd_p  <= wdata;
  end

  wire [16*WORDS-1:0] words;
  genvar w;
  generate
    for (w = 0; w < WORDS; w = w + 1) begin : word
      wire       open_w = sel_p[w] && sel_n[w];
      reg [15:0] q;
      // A latch on purpose (Verilog-2005 has no always_latch to say so).
      /* verilator lint_off LATCH */
      always @(*) begin
        if (!rst_n) q = 16'h0000;
        else if (open_w) q = wd_p;
      end
      /* verilator lint_on LATCH */
      assign words[16*w+:16] = q;
    end
  endgenerate

  assign rdata = words[16*raddr+:16];
endmodule
