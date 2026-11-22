# How AI was used

The competition allows AI tools if the write-up says how they were used.
This page is that account. In short: an AI wrote nearly everything, a person
directed it, and the project trusts no claim, from either of them, that a
test, a proof or a CI run has not checked.

## Who did what

**The AI** was Claude (Anthropic), working through Claude Code, a command-line
tool that lets it read and edit files, run simulations and drive git and
GitHub. Every commit except one merge carries its `Co-Authored-By: Claude
Opus 5.5` trailer. Claude wrote:

- the RTL (`src/`), the instruction set and its spec (`docs/isa.md`);
- the Python reference model, assembler, device models and protocol
  scenarios (`sw/pemu/`) and all firmware (`sw/programs/`);
- the test benches, co-simulation, formal checks and mutation testing
  (`test/`, `formal/`);
- the FPGA port (`fpga/`), the CI configuration and the layout settings;
- the documentation, including this page.

**The project owner** does not write code and did not edit any of these
files. Their part was direction and the things a person has to do:

- choosing to enter and setting the goal (a complete, verified entry);
- the decisions that shape the project: accepting the 8x4-tile allocation,
  asking for a blind test of protocols added after the hardware freeze,
  asking for the larger program memory, freezing the chip, and later
  reopening it for v4 (keeping the frozen v3 as a fallback);
- running the design on real hardware: flashing the Basys 3 FPGA board and
  running its self-tests, which no AI can do;
- the GitHub account, the competition correspondence and the submission.

Claude explained each step in plain language, and the owner decided what to
do next. Within that direction, Claude made the engineering choices (for
example how to build the memories, or at which corner to sign off timing)
and recorded the reasons in the commit messages and in
[verification.md](verification.md).

## How the AI's work was checked

An AI can write code that looks right and describe results it has not
produced. The verification was designed so that neither matters: every
result in these documents comes from a tool, not from the AI's say-so.

- **Two implementations.** The Python model and the RTL are separate
  programs written from the spec. They are run side by side and compared on
  every clock (about 700,000 clocks per CI run), so a slip has to be made
  twice, in the same way, to go unnoticed. Both were written by the same
  AI, though, so a misreading of the spec could be shared. The device
  models are also the AI's, but they are written from each protocol's own
  published timing rules rather than from PEMU's spec, so they check the
  chip against a different source. Real external equipment (a PC's Ethernet
  port, a sensor) has not been connected yet; that would be the fully
  independent check.
- **Proofs instead of examples** for the properties that matter most: cycle
  exactness, FIFO safety, trap behaviour (`formal/run.sh`).
- **Tests of the tests.** Mutation testing plants 59 realistic bugs in the
  RTL, one at a time; the suite must catch every one. This guards against
  tests that pass because they check too little, which is easy to write and
  hard to notice.
- **CI is the record.** Layout, precheck and gate-level tests run on GitHub,
  and the numbers in [verification.md](verification.md) are copied from CI
  reports, with the run ID.
- **Real hardware.** The same RTL runs on a Basys 3 FPGA board, where the
  UART, USB and Ethernet self-tests pass, 100 out of 100 Ethernet frames got
  through (v2), and v4 matches the Python model register for register over
  18,000 single-stepped ticks (`python3 -m pemu.fpga lockstep`).

## Where the AI was wrong, and what caught it

These are the AI's own mistakes during the project, in the order they
happened. Each was caught by a check, not by luck.

| Mistake | Caught by |
|---|---|
| The first SPI slave popped a FIFO word on SPI's trailing clock edge, losing one byte per read | the UART RX scenario run over the real SPI pins |
| The first Ethernet receiver lost about 1 frame in 30 under ±2 ns of jitter | a jitter sweep in the Ethernet tests |
| The model and RTL disagreed about whether an instruction sees the timestamp before or after its own tick | the lockstep co-simulation |
| Four of the five protocols added after the freeze had bugs: a PS/2 frame end taken as a new frame, a SWD sample point half a clock early, a CAN frame sent onto a busy bus, an IR decoder fooled when started mid-frame | device models that check the protocol's timing rules; two only showed up on the RTL, where the host is as slow as a real one ([new-protocols.md](new-protocols.md)) |
| A first design put the program-memory select bits in the CTRL register, which would have switched engines on and off whenever a program was loaded | review of the design before writing it |
| The first diagnosis of the failing 8x4 layout blamed the placement density; the real cause was wiring on the one horizontal metal layer | reading the router's per-layer report after a second failed run |
| "Remove the memories' reset" was expected to shrink the chip; measured with the real cell library, it saved 1% | synthesis with the IHP library before committing to it |
| A first latch-memory design gated the clock through logic, relying on how the layout tools build clock trees | checking the PDK (its clock-gate cells are excluded from layout) and a latch RAM proven in silicon, which avoids clock gating; the design was replaced before it was committed |
| The co-simulation drove the bus exactly on the falling edge, which the new latch memories also use, so the test bench itself raced | the co-simulation failing on the first run |
| Three mutations had silently stopped applying after an RTL change, so "43/43 caught" covered 43 of 46 | the mutation runner's "SKIP" lines, now part of the review |
| The latch memory added a half-clock path that failed timing at the slow corner | the layout's timing report |
| A script that watched for CI results queried the wrong workflow name, so a failed layout went unreported for two days | noticing that the script never printed a run ID |
| v4's first `skip` jumped over the next instruction in zero ticks, so a branch's timing depended on its condition, breaking the "every path takes the same time" style the firmware relies on | writing the CAN receiver, whose sample point and ACK edge must stay on the bit grid on every path; `skip` was changed to cancel the instruction instead, before any layout run |
| v4's tests did not exercise `x<y` with X equal to Y, so a comparison off by one would have passed | mutation testing (the `x<=y` mutant survived); a directed boundary test was added |
| The assembler lower-cased condition names, so `skip !flag F` could not use a `.define`d flag number | assembling the CAN receiver |
| The reset-strap test released reset and the strap on the same clock edge, which only works with zero delays: in the real layout reset arrives later than the strap | the timed gate-level simulation, at all three corners; the test now holds the strap two clocks longer, and the docs state the rule |
| The first FPGA lockstep check assumed reset clears the program memories. It does on the chip, but not on the FPGA, so leftover instructions showed up as false differences | the check itself, on the board; it now writes every word before each program |

One limit of simulation came up and is stated rather than hidden: a latch
closing on the wrong clock edge passes every zero-delay test. The design
avoids it by construction, and the routed netlist was also simulated with its
real gate delays (section 6 of [verification.md](verification.md)).

## What this means for the claims

The architecture, the novelty claims and the documents are the AI's work,
reviewed by the owner in plain language. The correctness claims do not rest
on that review: they rest on the model, the proofs, the mutation score, the
gate-level and timed simulations, the CI sign-off and the FPGA board, all of
which anyone can re-run from this repository.
