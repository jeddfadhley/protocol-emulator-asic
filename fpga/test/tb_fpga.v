`default_nettype none
`timescale 1ns / 1ps

// Testbench for pemu_fpga_top: 50 MHz clock, a fast UART (CLK_HZ / 8 baud)
// and a short transaction timeout so the tests run quickly. The uio pads
// are modelled with pull-ups, like the Basys 3 Pmod JA pins.
module tb_fpga ();

  initial begin
    $dumpfile("tb_fpga.fst");
    $dumpvars(0, tb_fpga);
    #1;
  end

  localparam integer CLK_HZ = 50_000_000;

  reg        clk;
  reg        rst_n;
  reg        uart_rx;
  reg  [7:0] ui_ext;
  wire       uart_tx;
  wire       uart_activity;
  wire [7:0] uo_out, uio_out, uio_oe;
  wire [7:0] uio_in;

  genvar i;
  generate
    for (i = 0; i < 8; i = i + 1) begin : g_pad
      assign uio_in[i] = uio_oe[i] ? uio_out[i] : 1'b1;
    end
  endgenerate

  // SPI signals inside the wrapper, for checks on CS_N
  wire spi_cs_n = dut.cs_n;
  wire spi_sck  = dut.sck;

  pemu_fpga_top #(
      .CLK_HZ      (CLK_HZ),
      .BAUD        (CLK_HZ / 8),
      .SCK_HALF    (5),
      .TIMEOUT_CLKS(20_000)
  ) dut (
      .clk          (clk),
      .rst_n_in     (rst_n),
      .uart_rx      (uart_rx),
      .uart_tx      (uart_tx),
      .uart_activity(uart_activity),
      .ui_ext       (ui_ext),
      .uo_out       (uo_out),
      .uio_in       (uio_in),
      .uio_out      (uio_out),
      .uio_oe       (uio_oe)
  );

endmodule
