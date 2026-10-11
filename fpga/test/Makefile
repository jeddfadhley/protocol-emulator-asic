# cocotb test of the FPGA wrapper: UART -> bridge -> SPI -> PEMU.
#   make            run the tests (Icarus Verilog)
# Needs cocotb (pip install -r ../../test/requirements.txt) and iverilog.

SIM ?= icarus
TOPLEVEL_LANG ?= verilog
FST ?= -fst

SRC_DIR = $(PWD)/../../src
RTL_DIR = $(PWD)/../rtl

VERILOG_SOURCES += $(addprefix $(SRC_DIR)/,project.v pe_spi.v pe_core.v pe_engine.v pe_fifo.v pe_dma.v)
VERILOG_SOURCES += $(RTL_DIR)/uart_spi_bridge.v $(RTL_DIR)/pemu_fpga_top.v
VERILOG_SOURCES += $(PWD)/tb_fpga.v
COMPILE_ARGS += -I$(SRC_DIR) -DPEMU_LUTRAM

TOPLEVEL = tb_fpga
COCOTB_TEST_MODULES = test_fpga

# The Python model, demos and board driver live in ../../sw
export PYTHONPATH := $(PWD)/../../sw:$(PWD)/../../sw/board:$(PYTHONPATH)

include $(shell cocotb-config --makefiles)/Makefile.sim
