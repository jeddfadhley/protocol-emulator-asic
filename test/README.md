# PEMU testbenches

cocotb testbenches for the PEMU chip. See [../docs/verification.md](../docs/verification.md)
for the overall strategy.

| File | What it does |
|------|--------------|
| `test.py` + `rtl_bench.py` | chip-level tests through the SPI host pins: register access plus every protocol scenario from `../sw/pemu/scenarios.py` |
| `test_cosim.py` + `tb_core.v` | RTL core vs the Python reference model, compared every clock (`make TB=core`) |
| `mutate.py` | mutation testing: plants bugs in the RTL and checks the tests catch them |

Options for the co-simulation: `COSIM_SEEDS`, `COSIM_CYCLES`, `COSIM_SEED0`.

cocotb's Makefile cannot cope with spaces in the project path; run from a path
without spaces (a symlink works).

## How to run

To run the RTL simulation:

```sh
make -B
```

To run gatelevel simulation, first harden your project and copy `../runs/wokwi/results/final/verilog/gl/{your_module_name}.v` to `gate_level_netlist.v`.

Then run:

```sh
make -B GATES=yes
```

If you wish to save the waveform in VCD format instead of FST format, edit tb.v to use `$dumpfile("tb.vcd");` and then run:

```sh
make -B FST=
```

This will generate `tb.vcd` instead of `tb.fst`.

## How to view the waveform file

Using GTKWave

```sh
gtkwave tb.fst tb.gtkw
```

Using Surfer

```sh
surfer tb.fst
```
