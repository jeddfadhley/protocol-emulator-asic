// Compare pemu.js with the Python model and assembler: reads the cases
// written by sw/tests/test_playground.py (a JSON file) and prints "OK" or
// the first difference. Run: node check.js cases.json
"use strict";
const fs = require("fs");
const P = require("./pemu.js");

const cases = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));

function digest(c, rdata) {
  const e = c.engines.map((g) => [g.pc, g.delay, g.x, g.y, g.isr, g.isr_cnt, g.osr, g.osr_cnt,
    g.crc, g.time, g.divcnt, g.stalled, g.timed_out, g.rx_ovf, g.tx_ovf, g.armed, g.edge_lvl,
    g.to_cnt, g.wake, g.annul, g.pop_armed, g.txf.join(","), g.rxf.join(",")]);
  let ram = 0;
  c.ram.forEach((w, i) => { ram = (ram * 31 + w + i) % 1000000007; });
  return JSON.stringify([rdata, c.pin_out, c.pin_oe, c.pin_late, c.flags, c.ctrl, c.dma[0], c.dma[1],
    c.ram_addr, c.sync1, c.sync2, c.pulses, ram, e]);
}

let clocks = 0;
for (const [n, t] of cases.traces.entries()) {
  const c = new P.Core();
  for (const [i, op] of t.ops.entries()) {
    const [ui, uio, we, re, rc, addr, wdata] = op;
    const r = c.clock(ui, uio, { we: !!we, re: !!re, rc: !!rc, addr, wdata });
    const d = digest(c, r);
    if (d !== t.digests[i]) {
      console.log(`trace ${n} clock ${i}: js ${d}\n                 py ${t.digests[i]}`);
      process.exit(1);
    }
    clocks++;
  }
}

let programs = 0;
for (const [n, a] of cases.asm.entries()) {
  let got;
  try {
    const p = P.assemble(a.source);
    got = { code: p.code, labels: p.labels, wrap: [p.wrap_bottom, p.wrap_top],
      side: [p.side_count, p.side_pindir], trap: p.trap, name: p.name };
  } catch (e) {
    if (!(e instanceof P.AsmError)) throw e;
    got = { error: true };
  }
  const want = a.result;
  if (JSON.stringify(got) !== JSON.stringify(want)) {
    console.log(`assembly ${n}: js ${JSON.stringify(got)}\n  py ${JSON.stringify(want)}\n${a.source}`);
    process.exit(1);
  }
  programs++;
}
for (const [w, sc, text] of cases.disasm) {
  if (P.disassemble(w, sc) !== text) {
    console.log(`disassemble ${w} side ${sc}: js '${P.disassemble(w, sc)}' py '${text}'`);
    process.exit(1);
  }
}
console.log(`OK ${clocks} clocks, ${programs} assemblies, ${cases.disasm.length} disassemblies`);
