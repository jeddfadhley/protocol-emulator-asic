/*
 * Copyright (c) 2026 Jedd Fadhley
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// UART to SPI-master bridge for driving PEMU's host port from a PC.
//
// UART: 8N1, CLK_HZ / BAUD clocks per bit (rounded). PC -> FPGA commands:
//
//   'S' len_hi len_lo  b0 .. b(N-1)   N = 1..65535
//       CS_N goes low, each byte is exchanged over SPI as it arrives
//       (mode 0, MSB first) and the byte read on MISO is sent back over the
//       UART. CS_N rises after byte N, so one 'S' command is one SPI
//       transaction. CS_N stays low while waiting for the next byte, however
//       slow the UART is. If no byte arrives for TIMEOUT_CLKS clocks the
//       transaction is abandoned (CS_N rises) so a crashed host can't wedge
//       the bridge; 0 disables the timeout.
//   'R'  pulse pemu_rst_n low for 32 clocks, then reply 'R'.
//   'I'  reply "PEMU".
//   Any other byte outside a command is ignored.
//
// SPI: SCK runs at clk / (2 * SCK_HALF). PEMU needs SCK <= clk / 8, so
// SCK_HALF must be at least 4; the default 5 gives clk / 10. MISO is sampled
// on the clock that raises SCK, SCK_HALF clocks after the falling edge that
// shifted it out (PEMU updates MISO 3 clocks after that edge).
//
// Portable Verilog-2005, no vendor primitives.
module uart_spi_bridge #(
    parameter integer CLK_HZ       = 50_000_000,
    parameter integer BAUD         = 1_000_000,
    parameter integer SCK_HALF     = 5,
    parameter integer TIMEOUT_CLKS = CLK_HZ / 2
) (
    input  wire clk,
    input  wire rst_n,

    input  wire uart_rx,
    output wire uart_tx,

    output reg  spi_cs_n,
    output reg  spi_sck,
    output reg  spi_mosi,
    input  wire spi_miso,

    output reg  pemu_rst_n,
    output wire activity      // one-clock pulse per UART byte in or out
);

  localparam integer BIT_CLKS = (CLK_HZ + BAUD / 2) / BAUD;

  // ------------------------------------------------------------------
  // UART receiver and transmitter
  // ------------------------------------------------------------------
  wire       rx_valid;
  wire [7:0] rx_data;
  wire       tx_busy;
  reg        tx_start;
  reg  [7:0] tx_data;

  bridge_uart_rx #(.BIT_CLKS(BIT_CLKS)) u_rx (
      .clk(clk), .rst_n(rst_n), .rx(uart_rx), .valid(rx_valid), .data(rx_data));

  bridge_uart_tx #(.BIT_CLKS(BIT_CLKS)) u_tx (
      .clk(clk), .rst_n(rst_n), .start(tx_start), .data(tx_data),
      .busy(tx_busy), .tx(uart_tx));

  assign activity = rx_valid | tx_start;

  // ------------------------------------------------------------------
  // FIFOs: received bytes waiting for the command engine, and reply bytes
  // waiting for the transmitter.
  // ------------------------------------------------------------------
  wire       rxf_empty, rxf_full;
  wire [7:0] rxf_q;
  reg        rxf_pop;

  bridge_fifo #(.AW(4)) u_rxf (
      .clk(clk), .rst_n(rst_n),
      .push(rx_valid), .din(rx_data), .full(rxf_full),
      .pop(rxf_pop), .dout(rxf_q), .empty(rxf_empty));

  wire       txf_empty, txf_full;
  wire [7:0] txf_q;
  reg        txf_push;
  reg  [7:0] txf_din;

  bridge_fifo #(.AW(4)) u_txf (
      .clk(clk), .rst_n(rst_n),
      .push(txf_push), .din(txf_din), .full(txf_full),
      .pop(tx_start), .dout(txf_q), .empty(txf_empty));

  always @(*) begin
    tx_start = !txf_empty && !tx_busy;
    tx_data  = txf_q;
  end

  // ------------------------------------------------------------------
  // Command engine and SPI master
  // ------------------------------------------------------------------
  localparam [3:0] S_IDLE  = 4'd0,
                   S_LENH  = 4'd1,
                   S_LENL  = 4'd2,
                   S_CSLOW = 4'd3,   // CS_N low, setup time before the first SCK
                   S_DATA  = 4'd4,   // waiting for the next byte (CS_N stays low)
                   S_LOW   = 4'd5,   // SCK low half: MOSI set up
                   S_HIGH  = 4'd6,   // SCK high half: MISO sampled on entry
                   S_CSEND = 4'd7,   // hold after the last SCK fall, then CS_N high
                   S_CSGAP = 4'd8,   // minimum CS_N high time
                   S_RST   = 4'd9,
                   S_REPLY = 4'd10;

  localparam integer TW = 26;   // idle counter: up to 67M clocks
  localparam [7:0]   HALF = SCK_HALF;

  reg [3:0]    state;
  reg [15:0]   remaining;
  reg [7:0]    shift_out;
  reg [7:0]    shift_in;
  reg [2:0]    bitn;
  reg [7:0]    wait_cnt;
  reg [TW-1:0] idle_cnt;
  reg [2:0]    reply_idx;   // 'I': 0..3 -> "PEMU"; 'R': 4

  reg [7:0] reply_byte;
  always @(*) begin
    case (reply_idx)
      3'd0:    reply_byte = "P";
      3'd1:    reply_byte = "E";
      3'd2:    reply_byte = "M";
      3'd3:    reply_byte = "U";
      default: reply_byte = "R";
    endcase
  end

  wire have_byte = !rxf_empty;
  wire timed_out = (TIMEOUT_CLKS != 0) && (idle_cnt >= TIMEOUT_CLKS);

  always @(posedge clk) begin
    rxf_pop  <= 1'b0;
    txf_push <= 1'b0;
    if (!rst_n) begin
      state      <= S_IDLE;
      spi_cs_n   <= 1'b1;
      spi_sck    <= 1'b0;
      spi_mosi   <= 1'b0;
      pemu_rst_n <= 1'b1;
      remaining  <= 16'd0;
      shift_out  <= 8'd0;
      shift_in   <= 8'd0;
      bitn       <= 3'd0;
      wait_cnt   <= 8'd0;
      idle_cnt   <= {TW{1'b0}};
      reply_idx  <= 3'd0;
    end else begin
      if (wait_cnt != 0) wait_cnt <= wait_cnt - 8'd1;

      // Time spent waiting for the PC in the middle of a command.
      if ((state == S_LENH || state == S_LENL || state == S_DATA) && !have_byte)
        idle_cnt <= idle_cnt + 1'b1;
      else
        idle_cnt <= {TW{1'b0}};

      case (state)
        S_IDLE: begin
          // rxf_pop takes effect next clock, so only look at a byte the
          // clock after a pop has completed.
          if (have_byte && !rxf_pop) begin
            rxf_pop <= 1'b1;
            case (rxf_q)
              8'h53: state <= S_LENH;                  // 'S'
              8'h52: begin                             // 'R'
                state      <= S_RST;
                pemu_rst_n <= 1'b0;
                wait_cnt   <= 8'd32;
              end
              8'h49: begin                             // 'I'
                state     <= S_REPLY;
                reply_idx <= 3'd0;
              end
              default: ;                               // ignore
            endcase
          end
        end

        S_LENH: begin
          if (timed_out) state <= S_IDLE;
          else if (have_byte && !rxf_pop) begin
            rxf_pop         <= 1'b1;
            remaining[15:8] <= rxf_q;
            state           <= S_LENL;
          end
        end

        S_LENL: begin
          if (timed_out) state <= S_IDLE;
          else if (have_byte && !rxf_pop) begin
            rxf_pop        <= 1'b1;
            remaining[7:0] <= rxf_q;
            if (remaining[15:8] == 8'd0 && rxf_q == 8'd0) begin
              state <= S_IDLE;                         // N = 0: nothing to do
            end else begin
              state    <= S_CSLOW;
              spi_cs_n <= 1'b0;
              wait_cnt <= HALF;
            end
          end
        end

        S_CSLOW: if (wait_cnt == 0) state <= S_DATA;

        S_DATA: begin
          if (timed_out) begin
            state    <= S_CSEND;
            wait_cnt <= HALF;
          end else if (have_byte && !rxf_pop && !txf_full) begin
            rxf_pop   <= 1'b1;
            shift_out <= {rxf_q[6:0], 1'b0};
            spi_mosi  <= rxf_q[7];
            bitn      <= 3'd0;
            wait_cnt  <= HALF - 8'd1;
            state     <= S_LOW;
          end
        end

        S_LOW: begin
          if (wait_cnt == 0) begin
            spi_sck  <= 1'b1;
            shift_in <= {shift_in[6:0], spi_miso};
            wait_cnt <= HALF - 8'd1;
            state    <= S_HIGH;
          end
        end

        S_HIGH: begin
          if (wait_cnt == 0) begin
            spi_sck <= 1'b0;
            if (bitn == 3'd7) begin
              // Byte done: queue the MISO byte for the PC.
              txf_push  <= 1'b1;
              txf_din   <= shift_in;
              remaining <= remaining - 16'd1;
              if (remaining == 16'd1) begin
                state    <= S_CSEND;
                wait_cnt <= HALF;
              end else begin
                state <= S_DATA;
              end
            end else begin
              bitn      <= bitn + 3'd1;
              spi_mosi  <= shift_out[7];
              shift_out <= {shift_out[6:0], 1'b0};
              wait_cnt  <= HALF - 8'd1;
              state     <= S_LOW;
            end
          end
        end

        S_CSEND: begin
          if (wait_cnt == 0) begin
            spi_cs_n <= 1'b1;
            spi_mosi <= 1'b0;
            wait_cnt <= 8'd4 * HALF;
            state    <= S_CSGAP;
          end
        end

        S_CSGAP: if (wait_cnt == 0) state <= S_IDLE;

        S_RST: begin
          if (wait_cnt == 0) begin
            pemu_rst_n <= 1'b1;
            reply_idx  <= 3'd4;
            state      <= S_REPLY;
          end
        end

        S_REPLY: begin
          // Queue reply bytes one per clock while there is room.
          if (!txf_full && !txf_push) begin
            txf_push <= 1'b1;
            txf_din  <= reply_byte;
            if (reply_idx == 3'd3 || reply_idx == 3'd4) state <= S_IDLE;
            else reply_idx <= reply_idx + 3'd1;
          end
        end

        default: state <= S_IDLE;
      endcase
    end
  end

  wire _unused = &{rxf_full, 1'b0};

endmodule


// 8N1 receiver: 2-flop synchronizer, start-bit check at mid-bit, data
// sampled at the middle of each bit, frames with a bad stop bit dropped.
module bridge_uart_rx #(
    parameter integer BIT_CLKS = 50
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       rx,
    output reg        valid,
    output reg  [7:0] data
);
  localparam integer CW = 16;

  reg [1:0]    sync;
  reg [CW-1:0] cnt;
  reg [3:0]    bitn;
  reg          busy;
  reg [7:0]    sh;

  wire line = sync[1];

  always @(posedge clk) begin
    valid <= 1'b0;
    if (!rst_n) begin
      sync <= 2'b11;
      cnt  <= {CW{1'b0}};
      bitn <= 4'd0;
      busy <= 1'b0;
      sh   <= 8'd0;
      data <= 8'd0;
    end else begin
      sync <= {sync[0], rx};
      if (!busy) begin
        if (!line) begin
          busy <= 1'b1;
          bitn <= 4'd0;
          cnt  <= BIT_CLKS / 2 - 1;
        end
      end else if (cnt != 0) begin
        cnt <= cnt - 1'b1;
      end else begin
        cnt <= BIT_CLKS - 1;
        if (bitn == 4'd0) begin
          if (line) busy <= 1'b0;          // glitch, not a start bit
          bitn <= 4'd1;
        end else if (bitn == 4'd9) begin
          busy <= 1'b0;
          if (line) begin
            valid <= 1'b1;
            data  <= sh;
          end
        end else begin
          sh   <= {line, sh[7:1]};         // LSB first
          bitn <= bitn + 4'd1;
        end
      end
    end
  end
endmodule


// 8N1 transmitter.
module bridge_uart_tx #(
    parameter integer BIT_CLKS = 50
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       start,
    input  wire [7:0] data,
    output wire       busy,
    output reg        tx
);
  localparam integer CW = 16;

  reg [CW-1:0] cnt;
  reg [3:0]    bitn;   // 0 = idle, 1..10 = start, d0..d7, stop
  reg [8:0]    sh;

  assign busy = bitn != 4'd0;

  always @(posedge clk) begin
    if (!rst_n) begin
      tx   <= 1'b1;
      cnt  <= {CW{1'b0}};
      bitn <= 4'd0;
      sh   <= 9'h1FF;
    end else if (bitn == 4'd0) begin
      if (start) begin
        tx   <= 1'b0;
        sh   <= {1'b1, data};
        cnt  <= BIT_CLKS - 1;
        bitn <= 4'd1;
      end
    end else if (cnt != 0) begin
      cnt <= cnt - 1'b1;
    end else if (bitn == 4'd10) begin
      bitn <= 4'd0;                         // stop bit finished
    end else begin
      tx   <= sh[0];
      sh   <= {1'b1, sh[8:1]};
      cnt  <= BIT_CLKS - 1;
      bitn <= bitn + 4'd1;
    end
  end
endmodule


// Small synchronous FIFO (2**AW entries). A push when full is dropped; dout
// shows the oldest entry whenever !empty.
module bridge_fifo #(
    parameter integer AW = 4
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       push,
    input  wire [7:0] din,
    output wire       full,
    input  wire       pop,
    output wire [7:0] dout,
    output wire       empty
);
  reg [7:0]  mem [0:(1<<AW)-1];
  reg [AW:0] wp, rp;

  assign empty = wp == rp;
  assign full  = (wp[AW] != rp[AW]) && (wp[AW-1:0] == rp[AW-1:0]);
  assign dout  = mem[rp[AW-1:0]];

  always @(posedge clk) begin
    if (!rst_n) begin
      wp <= {(AW+1){1'b0}};
      rp <= {(AW+1){1'b0}};
    end else begin
      if (push && !full) begin
        mem[wp[AW-1:0]] <= din;
        wp <= wp + 1'b1;
      end
      if (pop && !empty) rp <= rp + 1'b1;
    end
  end
endmodule
