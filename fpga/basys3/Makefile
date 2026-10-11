# Open-source bitstream build for the Basys 3 (openXC7: Yosys + nextpnr-xilinx
# + Project X-Ray). Runs in CI inside the regymm/openxc7 Docker image:
#
#   docker run --rm -v $PWD/../..:/work -w /work/fpga/basys3 regymm/openxc7 make
#
# Output: pemu_basys3.bit. Override the tool locations below for another
# install (e.g. the openXC7 snap or nix shell).
#
# nextpnr-xilinx fails the build if its timing estimate misses 50 MHz.
# -nocarry keeps adders and comparators in LUTs: in nextpnr-xilinx's timing
# model the CARRY4 chains cost more routing than they save (about 47 MHz
# with them, about 56 MHz without). Vivado (build_vivado.tcl) uses carry
# chains as usual and checks timing with the exact delay model.

PROJECT   := pemu_basys3
PART      := xc7a35tcpg236-1
FAMILY    := artix7
DBPART    := xc7a35t

NEXTPNR_XILINX_DIR ?= /nextpnr-xilinx
PRJXRAY_DIR        ?= /prjxray
PRJXRAY_DB_DIR     ?= $(NEXTPNR_XILINX_DIR)/xilinx/external/prjxray-db
CHIPDB_DIR         ?= chipdb
PYPY3              ?= pypy3
FASM2FRAMES        ?= $(PRJXRAY_DIR)/env/bin/python3 $(PRJXRAY_DIR)/utils/fasm2frames.py
XC7FRAMES2BIT      ?= xc7frames2bit
SYNTH_FLAGS        ?= -flatten -abc9 -nocarry
PNR_FLAGS          ?=
FREQ               ?= 50
YOSYS_PRE          ?=

SRC_DIR := ../../src
RTL_DIR := ../rtl
SOURCES := $(PROJECT).v \
           $(RTL_DIR)/pemu_fpga_top.v $(RTL_DIR)/uart_spi_bridge.v \
           $(addprefix $(SRC_DIR)/,project.v pe_spi.v pe_core.v pe_engine.v pe_fifo.v pe_dma.v)
XDC     := basys3.xdc
CHIPDB  := $(CHIPDB_DIR)/$(DBPART).bin

.DELETE_ON_ERROR:
.PHONY: all clean
all: $(PROJECT).bit

$(PROJECT).json: $(SOURCES)
	yosys -q -l yosys.log -p "$(YOSYS_PRE) read_verilog -DPEMU_LUTRAM $(SOURCES); synth_xilinx $(SYNTH_FLAGS) -arch xc7 -top $(PROJECT); write_json $@"

# The chip database is generated once per device (about 15 s).
$(CHIPDB):
	mkdir -p $(CHIPDB_DIR)
	$(PYPY3) $(NEXTPNR_XILINX_DIR)/xilinx/python/bbaexport.py --device $(PART) --bba $(CHIPDB_DIR)/$(DBPART).bba
	bbasm -l $(CHIPDB_DIR)/$(DBPART).bba $@
	rm -f $(CHIPDB_DIR)/$(DBPART).bba

$(PROJECT).fasm: $(PROJECT).json $(CHIPDB) $(XDC)
	nextpnr-xilinx --chipdb $(CHIPDB) --xdc $(XDC) --json $< --fasm $@ --freq $(FREQ) --log nextpnr.log $(PNR_FLAGS)

$(PROJECT).frames: $(PROJECT).fasm
	$(FASM2FRAMES) --part $(PART) --db-root $(PRJXRAY_DB_DIR)/$(FAMILY) $< > $@

$(PROJECT).bit: $(PROJECT).frames
	$(XC7FRAMES2BIT) --part_file $(PRJXRAY_DB_DIR)/$(FAMILY)/$(PART)/part.yaml \
	    --part_name $(PART) --frm_file $< --output_file $@

clean:
	rm -f $(PROJECT).json $(PROJECT).fasm $(PROJECT).frames $(PROJECT).bit *.log
