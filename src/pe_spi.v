/*
 * Copyright (c) 2026 Jedd Fadhley
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// SPI mode 0 slave that turns host transactions into register bus cycles.
// All SPI inputs are oversampled by clk, so SCK must be at most clk / 8.
//
// Transaction: CS_N low, command byte {rw, addr[6:0]} (rw = 1 reads), then
// any number of 16-bit words, MSB first. The address increments after each
// word except at the FIFO ports (offset 0xF of an engine block).
module pe_spi (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        cs_n_in,
    input  wire        sck_in,
    input  wire        mosi_in,
    output wire        miso,

    output reg         bus_we,
    output wire        bus_re,
    output wire        bus_rc,
    output reg  [6:0]  bus_addr,
    output reg  [15:0] bus_wdata,
    input  wire [15:0] bus_rdata
);

  reg [1:0] cs_s;
  reg [2:0] sck_s;
  reg [1:0] mosi_s;

  wire cs_n     = cs_s[1];
  wire mosi     = mosi_s[1];
  wire sck_rise = sck_s[1] & ~sck_s[2];
  wire sck_fall = ~sck_s[1] & sck_s[2];

  reg        data_phase;
  reg        rw;
  reg [3:0]  bitcnt;
  reg [14:0] shift_in;
  reg [15:0] shift_out;

  wire is_port = bus_addr[6:5] == 2'b10 && bus_addr[3:0] == 4'hF;

  // Load each read word on the falling SCK edge that starts it (bus_re), and
  // commit it on the rising edge of its last bit (bus_rc). A FIFO port only
  // pops at commit, so the trailing SCK fall before CS_N rises, or an
  // aborted word, never consumes data.
  wire load = !cs_n && data_phase && rw && sck_fall && bitcnt == 4'd0;
  assign bus_re = load;
  assign bus_rc = !cs_n && data_phase && rw && sck_rise && bitcnt == 4'd15;
  assign miso = shift_out[15];

  always @(posedge clk) begin
    if (!rst_n) begin
      cs_s       <= 2'b11;
      sck_s      <= 3'b000;
      mosi_s     <= 2'b00;
      data_phase <= 1'b0;
      rw         <= 1'b0;
      bitcnt     <= 4'd0;
      shift_in   <= 15'd0;
      shift_out  <= 16'h0000;
      bus_we     <= 1'b0;
      bus_addr   <= 7'd0;
      bus_wdata  <= 16'h0000;
    end else begin
      cs_s   <= {cs_s[0], cs_n_in};
      sck_s  <= {sck_s[1:0], sck_in};
      mosi_s <= {mosi_s[0], mosi_in};

      // A write strobe lasts one clock; the address moves on as it commits.
      bus_we <= 1'b0;
      if (bus_we && !is_port) bus_addr <= bus_addr + 7'd1;

      if (cs_n) begin
        data_phase <= 1'b0;
        bitcnt     <= 4'd0;
        shift_out  <= 16'h0000;
      end else begin
        if (sck_rise) begin
          shift_in <= {shift_in[13:0], mosi};
          bitcnt   <= bitcnt + 4'd1;
          if (!data_phase) begin
            if (bitcnt == 4'd7) begin
              data_phase <= 1'b1;
              bitcnt     <= 4'd0;
              rw         <= shift_in[6];
              bus_addr   <= {shift_in[5:0], mosi};
            end
          end else if (bitcnt == 4'd15) begin
            if (rw) begin
              if (!is_port) bus_addr <= bus_addr + 7'd1;
            end else begin
              bus_we    <= 1'b1;
              bus_wdata <= {shift_in, mosi};
            end
          end
        end
        if (load) begin
          shift_out <= bus_rdata;
        end else if (sck_fall && data_phase && rw) begin
          shift_out <= {shift_out[14:0], 1'b0};
        end
      end
    end
  end

endmodule
