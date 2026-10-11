#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Mutation testing: does the verification catch deliberately planted bugs?

Each mutation edits one line of RTL in a scratch copy of the repo, then runs
the lockstep co-simulation (and, for SPI mutations, the SPI-level tests). A
mutation is "killed" if some test fails. Survivors point at holes in the
verification.

    python3 test/mutate.py            # all mutations
    python3 test/mutate.py 3 7        # selected ones
"""

import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (file, original text, mutated text, description)
MUTATIONS = [
    ("pe_engine.v", "3'd5: cond = (x != y);", "3'd5: cond = (x == y);", "JMP x!=y inverted"),
    ("pe_engine.v", "if (instr[7:5] == 3'd2) x_n = x - 16'd1;", "", "JMP x-- forgets to decrement"),
    ("pe_engine.v", "3'd7: cond = (osr_cnt < pull_thresh);".replace("3'd7", "default"),
     "default: cond = (osr_cnt <= pull_thresh);", "!OSRE off by one"),
    ("pe_engine.v", "if (autopush && cnt_sum >= {1'b0, push_thresh}) begin",
     "if (autopush && cnt_sum > {1'b0, push_thresh}) begin", "autopush threshold off by one"),
    ("pe_engine.v", "if (!done && p != instr[7]) armed_n = 1'b1;", "", "edge wait never arms"),
    ("pe_engine.v", "          pc_n = trap;", "          pc_n = pc;", "trap does not jump"),
    ("pe_engine.v", "trapped = !done && instr[4] && (to_cnt == timeout);",
     "trapped = !done && (to_cnt == timeout);", "every wait can trap"),
    ("pe_engine.v", "crc_step = {c[14:0], 1'b0} ^ ((c[15] ^ b) ? poly : 16'h0000);",
     "crc_step = {c[14:0], 1'b0} ^ ((c[14] ^ b) ? poly : 16'h0000);", "CRC feedback tap wrong"),
    ("pe_engine.v", "          delay_n = dly;", "          delay_n = dly - (dly != 0);", "delay one tick short"),
    ("pe_engine.v", "wire tick = restart ? 1'b0 : (en ? (divcnt >= clkdiv) : step);",
     "wire tick = restart ? 1'b0 : (en ? (divcnt > clkdiv) : step);", "clock divider off by one"),
    ("pe_engine.v", "          else if (pc == wrap_top) pc_n = wrap_bottom;",
     "          else if (pc == wrap_top) pc_n = wrap_bottom + 5'd1;", "wrap lands one late"),
    ("pe_engine.v", "                  osr_n = x;\n", "                  osr_n = y;\n", "non-blocking PULL copies Y"),
    ("pe_engine.v", "          oe_val   = (i_oe_val & ~smask) | sval;", "          oe_val   = i_oe_val | sval;",
     "side-set pindirs do not override"),
    ("pe_engine.v", "if (done && instr[7]) flag_clr[instr[2:0]] = 1'b1;", "", "WAIT flag does not auto-clear"),
    ("pe_engine.v", "3'd5: source = time_cnt;", "3'd5: source = time_n;", "TIME read after increment"),
    ("pe_engine.v", "      if (host_load) pop_armed <= (rx_count != 3'd0);", "      if (host_load) pop_armed <= 1'b1;",
     "FIFO read pops a word it never showed"),
    ("pe_engine.v", "              if (instr[6] && isr_cnt < push_thresh) begin",
     "              if (instr[6] && isr_cnt <= push_thresh) begin", "PUSH iffull off by one"),
    ("pe_engine.v", "                  isr_cnt_n = n_ins;", "                  isr_cnt_n = 5'd0;", "OUT ISR count wrong"),
    ("pe_core.v", "  wire [15:0] out_e1 = (out_e0 & ~om1) | (ov1 & om1);",
     "  wire [15:0] out_e1 = (out_e0 & ~om1) | (ov1 & om1) | (pin_out & om0);", "engine priority broken"),
    ("pe_core.v", "  wire [7:0]  lb_low = (pin_out[7:0] & pin_oe[7:0]) | ~pin_oe[7:0];",
     "  wire [7:0]  lb_low = pin_out[7:0];", "loopback ignores pull-ups"),
    ("pe_core.v", "      sync2   <= sync1;", "      sync2   <= pins_ext;", "one synchronizer stage missing"),
    ("pe_core.v", "          A_PIN_CLR: pin_out <= out_e1 & ~bus_wdata;", "          A_PIN_CLR: pin_out <= pin_out & ~bus_wdata;",
     "PIN_CLR drops engine writes"),
    ("pe_fifo.v", "      count <= count + {2'b00, push} - {2'b00, pop};", "      count <= count + {2'b00, push};",
     "FIFO count ignores pops"),
    ("pe_spi.v", "      if (bus_we && !is_port) bus_addr <= bus_addr + 7'd1;",
     "      if (bus_we) bus_addr <= bus_addr + 7'd1;", "SPI bursts walk off the FIFO port"),
    ("pe_spi.v", "  assign bus_rc = !cs_n && data_phase && rw && sck_rise && bitcnt == 4'd15;",
     "  assign bus_rc = !cs_n && data_phase && rw && sck_fall && bitcnt == 4'd0;", "SPI pops on the trailing edge"),
]


def run(cmd, cwd, env):
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def failed(test_dir):
    path = os.path.join(test_dir, "results.xml")
    if not os.path.exists(path):
        return True  # did not even build/run: counts as caught
    return "<failure" in open(path).read()


def main(selected):
    killed, survived = [], []
    for i, (fname, orig, mutant, desc) in enumerate(MUTATIONS):
        if selected and i not in selected:
            continue
        work = tempfile.mkdtemp(prefix="pemu_mut_")
        try:
            for d in ("src", "test", "sw"):
                shutil.copytree(os.path.join(ROOT, d), os.path.join(work, d),
                                ignore=shutil.ignore_patterns("sim_build*", "*.fst", "results.xml", "__pycache__"))
            path = os.path.join(work, "src", fname)
            text = open(path).read()
            if text.count(orig) != 1:
                print(f"[{i:2}] SKIP  {desc}: original text not found once")
                continue
            open(path, "w").write(text.replace(orig, mutant))
            env = dict(os.environ, COSIM_SEEDS="12", COSIM_CYCLES="1500")
            test_dir = os.path.join(work, "test")
            if fname == "pe_spi.v":
                run(["make", "-s", "COCOTB_TEST_FILTER=test_spi_registers|test_fifo_port_burst|test_uart_rx"],
                    test_dir, env)
            else:
                run(["make", "-s", "TB=core"], test_dir, env)
            if failed(test_dir):
                killed.append(desc)
                print(f"[{i:2}] KILLED   {desc}")
            else:
                survived.append(desc)
                print(f"[{i:2}] SURVIVED {desc}")
        finally:
            shutil.rmtree(work, ignore_errors=True)
    total = len(killed) + len(survived)
    print(f"\n{len(killed)}/{total} mutations killed")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main({int(a) for a in sys.argv[1:]}))
