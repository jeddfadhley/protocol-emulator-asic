# Vivado non-project build of PEMU for the Digilent Basys 3.
#
#   cd fpga/basys3
#   vivado -mode batch -source build_vivado.tcl
#
# Output: pemu_basys3.bit (and reports in build/). Tested flow: Vivado
# 2020.1 or later (the free ML Standard edition supports the xc7a35t).

set script_dir [file dirname [file normalize [info script]]]
set src_dir    [file normalize [file join $script_dir .. .. src]]
set rtl_dir    [file normalize [file join $script_dir .. rtl]]
set out_dir    [file join $script_dir build]
set part       xc7a35tcpg236-1
set top        pemu_basys3

file mkdir $out_dir

# ---------------------------------------------------------------- sources
foreach f {project.v pe_spi.v pe_core.v pe_engine.v pe_fifo.v pe_dma.v} {
    read_verilog [file join $src_dir $f]
}
read_verilog [file join $rtl_dir uart_spi_bridge.v]
read_verilog [file join $rtl_dir pemu_fpga_top.v]
read_verilog [file join $script_dir pemu_basys3.v]
read_xdc     [file join $script_dir basys3.xdc]

# ---------------------------------------------------------------- synthesis
synth_design -top $top -part $part -include_dirs $src_dir -verilog_define PEMU_LUTRAM
write_checkpoint -force [file join $out_dir post_synth.dcp]
report_utilization -file [file join $out_dir utilization_synth.rpt]

# Board configuration (Basys 3: 3.3 V configuration bank, quad-SPI flash)
set_property CFGBVS VCCO [current_design]
set_property CONFIG_VOLTAGE 3.3 [current_design]
set_property BITSTREAM.GENERAL.COMPRESS TRUE [current_design]
set_property BITSTREAM.CONFIG.SPI_BUSWIDTH 4 [current_design]
set_property BITSTREAM.CONFIG.CONFIGRATE 33 [current_design]

# Asynchronous inputs and slow outputs have no timing relationship with the
# 50 MHz clock (every input is synchronized inside the design).
set_false_path -from [get_ports {btnC RsRx JA[*] JC[*] eth_rx_p}]
set_false_path -to   [get_ports {RsTx JA[*] JB[*] led[*]}]

# ---------------------------------------------------------------- implementation
opt_design
place_design
phys_opt_design
route_design
write_checkpoint -force [file join $out_dir post_route.dcp]
report_timing_summary -file [file join $out_dir timing.rpt]
report_utilization    -file [file join $out_dir utilization.rpt]
report_drc            -file [file join $out_dir drc.rpt]

# Stop if timing is not met (WNS < 0)
set wns [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup]]
puts "Worst setup slack: $wns ns"
if {$wns < 0} {
    error "Timing not met (WNS = $wns ns); see build/timing.rpt"
}

# ---------------------------------------------------------------- bitstream
write_bitstream -force [file join $script_dir ${top}.bit]
# Configuration-memory image for the on-board flash (program with
# openFPGALoader -b basys3 -f pemu_basys3.bit, or Vivado's hardware manager)
write_cfgmem -force -format mcs -size 4 -interface SPIx4 \
    -loadbit "up 0x0 [file join $script_dir ${top}.bit]" \
    [file join $out_dir ${top}.mcs]
puts "Wrote [file join $script_dir ${top}.bit]"
