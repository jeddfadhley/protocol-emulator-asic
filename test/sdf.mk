# Gate-level simulation with the routed delays (SDF) of one corner.
#
# The CI gate-level test runs the netlist with zero delays. This runs it with
# every cell's delay at one process corner, so races that zero-delay
# simulation hides (for example the latch memories closing while their data
# changes) would show up as wrong results. Get the files from a GDS run's
# GDS_logs artifact (runs/wokwi/final/...) and the IHP PDK:
#
#   make -f sdf.mk NETLIST=<final/nl/*.nl.v> SDF=<final/sdf/<corner>/*.sdf> \
#        CELLS=<sg13cmos5l_stdcell.v> UDP=<sg13cmos5l_udp.v> \
#        [CLK_NS=20] [COCOTB_TEST_FILTER=<regex>]
#
# Icarus applies the cells' path delays (IOPATH) and skips wire delays
# (INTERCONNECT), and it does not run setup/hold checks: those come from the
# layout flow's timing analysis. The cells' timing checks are removed here
# because Icarus 12 leaves their delayed_* nets undriven (see Makefile).

SIM ?= icarus
TOPLEVEL_LANG ?= verilog
SIM_BUILD = sim_build/sdf
TOPLEVEL = tb
COCOTB_TEST_MODULES = test
CLK_NS ?= 20

export PYTHONPATH := $(PWD)/../sw:$(PYTHONPATH)
export PEMU_CLK_NS := $(CLK_NS)

TIMED_CELLS = $(PWD)/sim_build/sg13cmos5l_stdcell_timed.v
VERILOG_SOURCES = $(TIMED_CELLS) $(UDP) $(NETLIST) $(PWD)/tb.v $(PWD)/sdf_annotate.v
COMPILE_ARGS += -gspecify -s sdf_annotate -DSDF_FILE=\"$(SDF)\" -DGL_TEST -DNO_DUMP
export COCOTB_RESULTS_FILE ?= $(SIM_BUILD)/results.xml

include $(shell cocotb-config --makefiles)/Makefile.sim

$(SIM_BUILD)/sim.vvp: $(TIMED_CELLS)

$(TIMED_CELLS): $(CELLS)
	mkdir -p $(dir $@)
	perl -0pe 's/^\s*\$$(setuphold|recrem|setup|hold|width|period|removal|recovery)\b[^;]*;\s*$$//gm; s/\bdelayed_(\w+)/$$1/g' $< > $@
