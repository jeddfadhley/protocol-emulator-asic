`default_nettype none
`timescale 1ns / 1ps

// Core-level testbench for lockstep co-simulation against the Python model
// (test_cosim.py). The host bus is driven directly, bypassing SPI.
module tb_core ();

  initial begin
    $dumpfile("tb_core.fst");
    $dumpvars(0, tb_core);
    #1;
  end

  reg         clk;
  reg         rst_n;
  reg  [15:0] pins_ext;
  reg         bus_we;
  reg         bus_re;
  reg         bus_rc;
  reg  [6:0]  bus_addr;
  reg  [15:0] bus_wdata;
  wire [15:0] bus_rdata;
  wire [15:0] pin_out;
  wire [15:0] pin_oe;

  pe_core dut (
      .clk      (clk),
      .rst_n    (rst_n),
      .pins_ext (pins_ext),
      .pin_out  (pin_out),
      .pin_oe   (pin_oe),
      .bus_we   (bus_we),
      .bus_re   (bus_re),
      .bus_rc   (bus_rc),
      .bus_addr (bus_addr),
      .bus_wdata(bus_wdata),
      .bus_rdata(bus_rdata)
  );

endmodule
