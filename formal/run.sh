#!/bin/sh
# Formal checks with Yosys + yosys-smtbmc (no SymbiYosys needed).
# For each module: bounded model check from reset, then k-induction for an
# unbounded proof. Usage: formal/run.sh [solver]   (default: yices)
set -e
cd "$(dirname "$0")"
SOLVER=${1:-yices}
DEPTH=${DEPTH:-20}
SRC=../src
mkdir -p build

check() {
  top=$1; defs=$2; shift 2
  yosys -q -p "read_verilog -formal -DFORMAL $defs $*; prep -top $top; async2sync; dffunmap; write_smt2 -wires build/$top.smt2"
  echo "== $top: BMC depth $DEPTH"
  yosys-smtbmc -s "$SOLVER" -t "$DEPTH" --presat build/$top.smt2 > build/$top.bmc.log || { tail -20 build/$top.bmc.log; exit 1; }
  tail -1 build/$top.bmc.log
  echo "== $top: k-induction depth $DEPTH"
  yosys-smtbmc -s "$SOLVER" -i -t "$DEPTH" --presat build/$top.smt2 > build/$top.ind.log || { tail -20 build/$top.ind.log; exit 1; }
  tail -1 build/$top.ind.log
}

check pe_fifo   -DFORMAL_FIFO_TOP $SRC/pe_fifo.v
check pe_engine ""                $SRC/pe_fifo.v $SRC/pe_engine.v
echo "All formal checks passed."
