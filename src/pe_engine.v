/*
 * Copyright (c) 2026 Jedd Fadhley
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// One PEMU execution engine: configuration registers, architectural state,
// TX/RX FIFOs and the single-cycle instruction executor. See docs/isa.md.
// Behaviour must match sw/pemu/model.py (Engine) clock for clock.
module pe_engine (
    input  wire        clk,
    input  wire        rst_n,

    // Control pulses/levels from CTRL
    input  wire        en,
    input  wire        restart,
    input  wire        step,
    input  wire        flush,

    // Host register port (sel = this engine's block is addressed)
    input  wire        sel,
    input  wire        we,
    input  wire        re,
    input  wire        rc,
    input  wire [3:0]  off,
    input  wire [15:0] wdata,
    output reg  [15:0] rdata,

    // Instruction fetch
    output wire [5:0]  fetch_addr,
    input  wire [15:0] instr,

    // Shared state
    input  wire [15:0] pin_in,
    input  wire [7:0]  flags,
    output reg  [15:0] out_mask,
    output reg  [15:0] out_val,
    output reg  [15:0] oe_mask,
    output reg  [15:0] oe_val,
    output reg  [7:0]  flag_set,
    output reg  [7:0]  flag_clr
);

  // ---------------------------------------------------------------------
  // Helpers

  function [15:0] rotl16(input [15:0] v, input [3:0] s);
    reg [31:0] t;
    begin
      t      = {v, v} << s;
      rotl16 = t[31:16];
    end
  endfunction

  function [15:0] rotr16(input [15:0] v, input [3:0] s);
    reg [31:0] t;
    begin
      t      = {v, v} >> s;
      rotr16 = t[15:0];
    end
  endfunction

  // Mask of the low n bits, n = 0..16
  function [15:0] nmask(input [4:0] n);
    reg [16:0] t;
    begin
      t     = (17'd1 << n) - 17'd1;
      nmask = t[15:0];
    end
  endfunction

  function [15:0] bitrev16(input [15:0] v);
    integer k;
    begin
      for (k = 0; k < 16; k = k + 1) bitrev16[k] = v[15-k];
    end
  endfunction

  function [15:0] crc_step(input [15:0] c, input [15:0] poly, input b);
    begin
      crc_step = {c[14:0], 1'b0} ^ ((c[15] ^ b) ? poly : 16'h0000);
    end
  endfunction

  // 4-bit count field, 0 means 16
  function [4:0] nfield(input [3:0] f);
    begin
      nfield = (f == 4'd0) ? 5'd16 : {1'b0, f};
    end
  endfunction

  // ---------------------------------------------------------------------
  // Registers

  localparam R_CLKDIV = 4'h0, R_PROG = 4'h1, R_PINMAP = 4'h2, R_PINCFG = 4'h3,
             R_SHIFT = 4'h4, R_TIMEOUT = 4'h5, R_TRAP = 4'h6, R_CRCPOLY = 4'h7,
             R_CRC = 4'h8, R_X = 4'h9, R_Y = 4'hA, R_ISR = 4'hB, R_OSR = 4'hC,
             R_STATUS = 4'hD, R_TIME = 4'hE, R_FIFO = 4'hF;

  reg [15:0] clkdiv, prog, pinmap, timeout, crcpoly;
  reg [15:0] pincfg;  // only bits in 0xF3FF are stored
  reg [13:0] shiftcfg;
  reg [4:0]  trap;

  reg [15:0] crc, x, y, isr, osr, to_cnt, time_cnt, divcnt;
  reg [4:0]  pc, delay, isr_cnt, osr_cnt;
  reg        armed, stalled, timed_out, rx_ovf, tx_ovf;

  // Decoded configuration
  wire [5:0] origin      = prog[5:0];
  wire [4:0] wrap_bottom = prog[10:6];
  wire [4:0] wrap_top    = prog[15:11];
  wire [3:0] out_base    = pinmap[3:0];
  wire [3:0] set_base    = pinmap[7:4];
  wire [3:0] side_base   = pinmap[11:8];
  wire [3:0] in_base     = pinmap[15:12];
  wire [4:0] out_count   = nfield(pincfg[3:0]);
  wire [4:0] set_count   = (pincfg[6:4] > 3'd5) ? 5'd5 : {2'b00, pincfg[6:4]};
  wire [1:0] side_count  = pincfg[8:7];
  wire       side_pindir = pincfg[9];
  wire [3:0] jmp_pin     = pincfg[15:12];
  wire       in_right    = shiftcfg[0];
  wire       out_right   = shiftcfg[1];
  wire       autopush    = shiftcfg[2];
  wire       autopull    = shiftcfg[3];
  wire [4:0] push_thresh = nfield(shiftcfg[7:4]);
  wire [4:0] pull_thresh = nfield(shiftcfg[11:8]);
  wire       crc_in      = shiftcfg[12];
  wire       crc_out     = shiftcfg[13];

  // ---------------------------------------------------------------------
  // FIFOs

  wire [15:0] tx_head, rx_head;
  wire [2:0]  tx_count, rx_count;
  wire        tx_empty = (tx_count == 3'd0);
  wire        rx_full  = (rx_count == 3'd4);

  // The host pops the RX FIFO when a read of the port commits, provided the
  // FIFO held data when that read was loaded (so the word it saw is the head).
  reg  pop_armed;
  wire host_push   = sel && we && off == R_FIFO;
  wire host_load   = sel && re && off == R_FIFO;
  wire host_commit = sel && rc && off == R_FIFO;
  wire host_pop    = host_commit && pop_armed;
  reg         tx_pop;
  reg         rx_push;
  reg  [15:0] rx_data;

  pe_fifo u_txf (
      .clk  (clk),
      .rst_n(rst_n),
      .flush(flush),
      .push (host_push && tx_count != 3'd4),
      .din  (wdata),
      .pop  (tx_pop),
      .head (tx_head),
      .count(tx_count)
  );

  pe_fifo u_rxf (
      .clk  (clk),
      .rst_n(rst_n),
      .flush(flush),
      .push (rx_push),
      .din  (rx_data),
      .pop  (host_pop && rx_count != 3'd0),
      .head (rx_head),
      .count(rx_count)
  );

  // ---------------------------------------------------------------------
  // Tick generation

  wire tick = restart ? 1'b0 : (en ? (divcnt >= clkdiv) : step);

  // ---------------------------------------------------------------------
  // Execute

  assign fetch_addr = origin + {1'b0, pc};

  wire [2:0] op    = instr[15:13];
  wire [4:0] sd    = instr[12:8];
  wire [4:0] dmask = 5'b11111 >> side_count;
  wire [4:0] dly   = sd & dmask;
  wire [4:0] side_val = sd >> (3'd5 - {1'b0, side_count});
  wire [4:0] n_ins = nfield(instr[3:0]);
  wire [15:0] pins_rot = rotr16(pin_in, in_base);

  function [15:0] source(input [2:0] s);
    begin
      case (s)
        3'd0: source = pins_rot;
        3'd1: source = x;
        3'd2: source = y;
        3'd3: source = 16'h0000;
        3'd4: source = crc;
        3'd5: source = time_cnt;
        3'd6: source = isr;
        default: source = osr;
      endcase
    end
  endfunction

  // Next-state values computed by the executor
  reg [15:0] x_n, y_n, isr_n, osr_n, crc_n, to_cnt_n, time_n;
  reg [4:0]  pc_n, delay_n, isr_cnt_n, osr_cnt_n;
  reg        armed_n, stalled_n, timed_out_n, rx_ovf_n;

  // Scratch
  reg        done, trapped, jump;
  reg [4:0]  jump_addr;
  reg        cond;
  reg        p;
  reg [15:0] v, data, src, wmask;
  reg [31:0] wide;
  reg [4:0]  base_cnt;
  reg [5:0]  cnt_sum;
  reg [15:0] i_out_mask, i_out_val, i_oe_mask, i_oe_val;
  reg [15:0] smask, sval;

  always @(*) begin
    x_n = x;
    y_n = y;
    isr_n = isr;
    isr_cnt_n = isr_cnt;
    osr_n = osr;
    osr_cnt_n = osr_cnt;
    crc_n = crc;
    pc_n = pc;
    delay_n = delay;
    armed_n = armed;
    to_cnt_n = to_cnt;
    time_n = time_cnt;
    stalled_n = stalled;
    timed_out_n = timed_out;
    rx_ovf_n = rx_ovf;

    tx_pop = 1'b0;
    rx_push = 1'b0;
    rx_data = 16'h0000;
    flag_set = 8'h00;
    flag_clr = 8'h00;
    i_out_mask = 16'h0000;
    i_out_val = 16'h0000;
    i_oe_mask = 16'h0000;
    i_oe_val = 16'h0000;
    out_mask = 16'h0000;
    out_val = 16'h0000;
    oe_mask = 16'h0000;
    oe_val = 16'h0000;

    done = 1'b1;
    trapped = 1'b0;
    jump = 1'b0;
    jump_addr = 5'd0;
    cond = 1'b0;
    p = 1'b0;
    v = 16'h0000;
    data = 16'h0000;
    src = 16'h0000;
    wmask = 16'h0000;
    wide = 32'h0;
    base_cnt = 5'd0;
    cnt_sum = 6'd0;
    smask = 16'h0000;
    sval = 16'h0000;

    if (tick) begin
      time_n = time_cnt + 16'd1;
      if (delay != 5'd0) begin
        delay_n = delay - 5'd1;
        stalled_n = 1'b0;
      end else begin
        case (op)
          // ---------------- JMP
          3'd0: begin
            case (instr[7:5])
              3'd0: cond = 1'b1;
              3'd1: cond = (x == 16'h0000);
              3'd2: cond = (x != 16'h0000);
              3'd3: cond = (y == 16'h0000);
              3'd4: cond = (y != 16'h0000);
              3'd5: cond = (x != y);
              3'd6: cond = pin_in[jmp_pin];
              default: cond = (osr_cnt < pull_thresh);
            endcase
            if (instr[7:5] == 3'd2) x_n = x - 16'd1;
            if (instr[7:5] == 3'd4) y_n = y - 16'd1;
            jump = cond;
            jump_addr = instr[4:0];
          end

          // ---------------- WAIT
          3'd1: begin
            case (instr[6:5])
              2'd0: done = (pin_in[instr[3:0]] == instr[7]);
              2'd1: done = (pins_rot[instr[3:0]] == instr[7]);
              2'd2: begin
                done = (flags[instr[2:0]] == instr[7]);
                if (done && instr[7]) flag_clr[instr[2:0]] = 1'b1;
              end
              default: begin
                p = pins_rot[instr[3:0]];
                done = armed && (p == instr[7]);
                if (!done && p != instr[7]) armed_n = 1'b1;
              end
            endcase
            trapped = !done && instr[4] && (to_cnt == timeout);
          end

          // ---------------- IN
          3'd2: begin
            data = source(instr[7:5]) & nmask(n_ins);
            if (in_right) begin
              wide = ({16'h0000, isr} >> n_ins) | ({16'h0000, data} << (5'd16 - n_ins));
            end else begin
              wide = ({16'h0000, isr} << n_ins) | {16'h0000, data};
            end
            cnt_sum = {1'b0, isr_cnt} + {1'b0, n_ins};
            if (cnt_sum > 6'd16) cnt_sum = 6'd16;
            if (autopush && cnt_sum >= {1'b0, push_thresh}) begin
              if (rx_full) begin
                done = 1'b0;
              end else begin
                rx_push = 1'b1;
                rx_data = wide[15:0];
                isr_n = 16'h0000;
                isr_cnt_n = 5'd0;
              end
            end else begin
              isr_n = wide[15:0];
              isr_cnt_n = cnt_sum[4:0];
            end
            if (done && crc_in && n_ins == 5'd1) crc_n = crc_step(crc, crcpoly, data[0]);
          end

          // ---------------- OUT
          3'd3: begin
            if (autopull && osr_cnt >= pull_thresh) begin
              if (tx_empty) begin
                done = 1'b0;
              end else begin
                src = tx_head;
                base_cnt = 5'd0;
                tx_pop = 1'b1;
              end
            end else begin
              src = osr;
              base_cnt = osr_cnt;
            end
            if (done) begin
              if (out_right) begin
                data = src & nmask(n_ins);
                wide = {16'h0000, src} >> n_ins;
                osr_n = wide[15:0];
              end else begin
                wide = {16'h0000, src} << n_ins;
                data = wide[31:16];
                osr_n = wide[15:0];
              end
              cnt_sum = {1'b0, base_cnt} + {1'b0, n_ins};
              if (cnt_sum > 6'd16) cnt_sum = 6'd16;
              osr_cnt_n = cnt_sum[4:0];
              wmask = nmask(n_ins);
              case (instr[7:5])
                3'd0: begin
                  i_out_mask = rotl16(wmask, out_base);
                  i_out_val  = rotl16(data & wmask, out_base);
                end
                3'd1: x_n = data;
                3'd2: y_n = data;
                3'd4: begin
                  i_oe_mask = rotl16(wmask, out_base);
                  i_oe_val  = rotl16(data & wmask, out_base);
                end
                3'd5: begin
                  jump = 1'b1;
                  jump_addr = data[4:0];
                end
                3'd6: begin
                  isr_n = data;
                  isr_cnt_n = n_ins;
                end
                default: ;
              endcase
              if (crc_out && n_ins == 5'd1) crc_n = crc_step(crc, crcpoly, data[0]);
            end
          end

          // ---------------- PUSH / PULL
          3'd4: begin
            if (!instr[7]) begin
              if (instr[6] && isr_cnt < push_thresh) begin
                // if-full and not full: nothing to do
              end else if (rx_full) begin
                if (instr[5]) begin
                  done = 1'b0;
                end else begin
                  isr_n = 16'h0000;
                  isr_cnt_n = 5'd0;
                  rx_ovf_n = 1'b1;
                end
              end else begin
                rx_push = 1'b1;
                rx_data = isr;
                isr_n = 16'h0000;
                isr_cnt_n = 5'd0;
              end
            end else begin
              if (instr[6] && osr_cnt < pull_thresh) begin
                // if-empty and not empty: nothing to do
              end else if (tx_empty) begin
                if (instr[5]) begin
                  done = 1'b0;
                end else begin
                  osr_n = x;
                  osr_cnt_n = 5'd0;
                end
              end else begin
                osr_n = tx_head;
                osr_cnt_n = 5'd0;
                tx_pop = 1'b1;
              end
            end
          end

          // ---------------- MOV
          3'd5: begin
            v = source(instr[2:0]);
            if (instr[4:3] == 2'd1) v = ~v;
            else if (instr[4:3] == 2'd2) v = bitrev16(v);
            wmask = nmask(out_count);
            case (instr[7:5])
              3'd0: begin
                i_out_mask = rotl16(wmask, out_base);
                i_out_val  = rotl16(v & wmask, out_base);
              end
              3'd1: x_n = v;
              3'd2: y_n = v;
              3'd3: crc_n = v;
              3'd4: begin
                i_oe_mask = rotl16(wmask, out_base);
                i_oe_val  = rotl16(v & wmask, out_base);
              end
              3'd5: begin
                jump = 1'b1;
                jump_addr = v[4:0];
              end
              3'd6: begin
                isr_n = v;
                isr_cnt_n = 5'd0;
              end
              default: begin
                osr_n = v;
                osr_cnt_n = 5'd0;
              end
            endcase
          end

          // ---------------- SET
          3'd7: begin
            wmask = nmask(set_count);
            case (instr[7:5])
              3'd0: begin
                i_out_mask = rotl16(wmask, set_base);
                i_out_val  = rotl16({11'd0, instr[4:0]} & wmask, set_base);
              end
              3'd1: x_n = {11'd0, instr[4:0]};
              3'd2: y_n = {11'd0, instr[4:0]};
              3'd3: begin
                if (instr[4]) flag_clr[instr[2:0]] = 1'b1;
                else flag_set[instr[2:0]] = 1'b1;
              end
              3'd4: begin
                i_oe_mask = rotl16(wmask, set_base);
                i_oe_val  = rotl16({11'd0, instr[4:0]} & wmask, set_base);
              end
              default: ;
            endcase
          end

          // ---------------- 3'd6: reserved, NOP
          default: ;
        endcase

        // Side-set wins over the instruction's own pin write
        smask = rotl16(nmask({3'b000, side_count}), side_base);
        sval  = rotl16({11'd0, side_val} & nmask({3'b000, side_count}), side_base);
        if (side_pindir) begin
          out_mask = i_out_mask;
          out_val  = i_out_val;
          oe_mask  = i_oe_mask | smask;
          oe_val   = (i_oe_val & ~smask) | sval;
        end else begin
          out_mask = i_out_mask | smask;
          out_val  = (i_out_val & ~smask) | sval;
          oe_mask  = i_oe_mask;
          oe_val   = i_oe_val;
        end

        if (done) begin
          if (jump) pc_n = jump_addr;
          else if (pc == wrap_top) pc_n = wrap_bottom;
          else pc_n = pc + 5'd1;
          delay_n = dly;
          to_cnt_n = 16'h0000;
          armed_n = 1'b0;
          stalled_n = 1'b0;
        end else if (trapped) begin
          pc_n = trap;
          delay_n = 5'd0;
          to_cnt_n = 16'h0000;
          armed_n = 1'b0;
          timed_out_n = 1'b1;
          stalled_n = 1'b1;
        end else begin
          to_cnt_n = to_cnt + 16'd1;
          stalled_n = 1'b1;
        end
      end
    end
  end

  // ---------------------------------------------------------------------
  // State update. Host writes come last so they win.

  wire host_w = sel && we;

  always @(posedge clk) begin
    if (!rst_n) begin
      clkdiv <= 16'h0000;
      prog <= 16'hF800;
      pinmap <= 16'h0000;
      pincfg <= 16'h0000;
      shiftcfg <= 14'h0000;
      timeout <= 16'hFFFF;
      trap <= 5'd0;
      crcpoly <= 16'h0000;
      crc <= 16'h0000;
      x <= 16'h0000;
      y <= 16'h0000;
      pc <= 5'd0;
      delay <= 5'd0;
      isr <= 16'h0000;
      isr_cnt <= 5'd0;
      osr <= 16'h0000;
      osr_cnt <= 5'd16;
      armed <= 1'b0;
      to_cnt <= 16'h0000;
      time_cnt <= 16'h0000;
      divcnt <= 16'h0000;
      stalled <= 1'b0;
      timed_out <= 1'b0;
      rx_ovf <= 1'b0;
      tx_ovf <= 1'b0;
      pop_armed <= 1'b0;
    end else begin
      if (host_load) pop_armed <= (rx_count != 3'd0);
      else if (host_commit) pop_armed <= 1'b0;

      if (restart) begin
        pc <= 5'd0;
        delay <= 5'd0;
        isr <= 16'h0000;
        isr_cnt <= 5'd0;
        osr <= 16'h0000;
        osr_cnt <= 5'd16;
        armed <= 1'b0;
        to_cnt <= 16'h0000;
        time_cnt <= 16'h0000;
        stalled <= 1'b0;
        divcnt <= 16'h0000;
      end else begin
        x <= x_n;
        y <= y_n;
        crc <= crc_n;
        pc <= pc_n;
        delay <= delay_n;
        isr <= isr_n;
        isr_cnt <= isr_cnt_n;
        osr <= osr_n;
        osr_cnt <= osr_cnt_n;
        armed <= armed_n;
        to_cnt <= to_cnt_n;
        time_cnt <= time_n;
        stalled <= stalled_n;
        timed_out <= timed_out_n;
        rx_ovf <= rx_ovf_n;
        if (!en) divcnt <= 16'h0000;
        else if (tick) divcnt <= 16'h0000;
        else divcnt <= divcnt + 16'd1;
      end

      if (host_push && tx_count == 3'd4) tx_ovf <= 1'b1;

      if (host_w) begin
        case (off)
          R_CLKDIV:  clkdiv <= wdata;
          R_PROG:    prog <= wdata;
          R_PINMAP:  pinmap <= wdata;
          R_PINCFG:  pincfg <= wdata & 16'hF3FF;
          R_SHIFT:   shiftcfg <= wdata[13:0];
          R_TIMEOUT: timeout <= wdata;
          R_TRAP:    trap <= wdata[4:0];
          R_CRCPOLY: crcpoly <= wdata;
          R_CRC:     crc <= wdata;
          R_X:       x <= wdata;
          R_Y:       y <= wdata;
          R_STATUS: begin
            if (wdata[12]) timed_out <= 1'b0;
            if (wdata[13]) rx_ovf <= 1'b0;
            if (wdata[14]) tx_ovf <= 1'b0;
          end
          default: ;
        endcase
      end
    end
  end

  // ---------------------------------------------------------------------
  // Host read port

  always @(*) begin
    case (off)
      R_CLKDIV:  rdata = clkdiv;
      R_PROG:    rdata = prog;
      R_PINMAP:  rdata = pinmap;
      R_PINCFG:  rdata = pincfg;
      R_SHIFT:   rdata = {2'b00, shiftcfg};
      R_TIMEOUT: rdata = timeout;
      R_TRAP:    rdata = {11'd0, trap};
      R_CRCPOLY: rdata = crcpoly;
      R_CRC:     rdata = crc;
      R_X:       rdata = x;
      R_Y:       rdata = y;
      R_ISR:     rdata = isr;
      R_OSR:     rdata = osr;
      R_STATUS:  rdata = {1'b0, tx_ovf, rx_ovf, timed_out, stalled, rx_count, tx_count, pc};
      R_TIME:    rdata = time_cnt;
      default:   rdata = (rx_count == 3'd0) ? 16'h0000 : rx_head;
    endcase
  end

  // ---------------------------------------------------------------------
  // Formal properties (formal/run.sh proves these with BMC and k-induction)

`ifdef FORMAL
  reg f_past_valid = 1'b0;
  always @(posedge clk) f_past_valid <= 1'b1;
  always @(*) if (!f_past_valid) assume (!rst_n);

  wire f_host_status_w = sel && we && off == R_STATUS;

  always @(*) begin
    if (f_past_valid) begin
      // Shift counts and FIFO levels stay in range
      assert (isr_cnt <= 5'd16);
      assert (osr_cnt <= 5'd16);
      assert (tx_count <= 3'd4);
      assert (rx_count <= 3'd4);
    end

    // FIFO discipline: never push a full FIFO or pop an empty one
    assert (!(rx_push && rx_full));
    assert (!(tx_pop && tx_empty));

    // Between ticks nothing in the engine moves and nothing leaves it
    if (!tick) begin
      assert (out_mask == 16'h0 && oe_mask == 16'h0);
      assert (flag_set == 8'h0 && flag_clr == 8'h0);
      assert (!tx_pop && !rx_push);
      assert (pc_n == pc && delay_n == delay && time_n == time_cnt);
      assert (x_n == x && y_n == y && crc_n == crc);
      assert (isr_n == isr && osr_n == osr && to_cnt_n == to_cnt);
    end

    // Delay ticks are inert: only the delay counter and TIME advance
    if (tick && delay != 5'd0) begin
      assert (out_mask == 16'h0 && oe_mask == 16'h0);
      assert (flag_set == 8'h0 && flag_clr == 8'h0);
      assert (!tx_pop && !rx_push);
      assert (pc_n == pc && delay_n == delay - 5'd1);
      assert (x_n == x && y_n == y && crc_n == crc && isr_n == isr && osr_n == osr);
    end

    // Cycle exactness: a completing instruction loads exactly its delay
    // field, so it occupies 1 + delay ticks
    if (tick && delay == 5'd0 && done) assert (delay_n == dly);

    // A stalled instruction is retried: PC holds, no delay is started
    if (tick && delay == 5'd0 && !done && !trapped) begin
      assert (pc_n == pc && delay_n == 5'd0);
      assert (!tx_pop && !rx_push);
    end

    // Only a WAIT with the T bit can trap
    if (trapped) assert (op == 3'd1 && instr[4]);
  end

  always @(posedge clk) begin
    if (f_past_valid && $past(rst_n) && rst_n && !$past(restart)) begin
      // TIME counts ticks exactly
      assert (time_cnt == $past(time_cnt) + {15'd0, $past(tick)});
      // A trap lands on the TRAP address and raises TIMED_OUT
      if ($past(trapped) && $past(tick)) begin
        assert (pc == $past(trap));
        assert (delay == 5'd0);
        if (!$past(f_host_status_w && wdata[12])) assert (timed_out);
      end
      // With the divider running, consecutive ticks are CLKDIV + 1 clocks apart
      if ($past(en) && en && $past(tick)) assert (divcnt == 16'h0);
    end
    if (f_past_valid && $past(rst_n) && rst_n && $past(en) && !$past(restart)
        && !$past(tick) && $past(clkdiv) == clkdiv) begin
      assert (divcnt == $past(divcnt) + 16'd1);
    end
  end
`endif

endmodule
