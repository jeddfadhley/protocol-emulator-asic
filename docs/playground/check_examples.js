// Run every playground example in node and print what the decoders read,
// as JSON, for sw/tests/test_playground.py to check against the Python model.
"use strict";
const P = require("./pemu.js");
const DATA = require("./data.js");
const PL = require("./play.js");

const out = {};
for (const ex of PL.EXAMPLES) {
  const inputs = Object.fromEntries((ex.inputs || []).map((i) => [i.id, i.value]));
  const sources = ex.engines.map((e) => (e ? (e.source || DATA.programs[e.prog]) : ""));
  const cfgs = ex.engines.map((e) => (e ? e.cfg : {}));
  const t0 = Date.now();
  const s = new PL.Session(ex, sources, cfgs, inputs);
  s.run(ex.clocks(inputs));
  const d = ex.decode(s);
  const r = { clocks: s.length, ms: Date.now() - t0, changes: s.trace.n,
    pcs: [0, 1].map((e) => s.core.engines[e].pc) };
  if (d.text) r.text = d.text.value;
  if (d.marks) r.marks = d.marks.map((m) => m.label);
  if (d.leds) r.leds = d.leds.map((l) => [l.r, l.g, l.b]);
  if (d.notes) r.notes = d.notes.map((n) => [n.name, Math.round(n.hz * 10) / 10, n.end - n.start]);
  if (d.vga) {
    let bits = "";
    for (let y = 7; y < 480; y += 15) for (let x = 5; x < 640; x += 10) {
      bits += d.vga.pixels[4 * (y * 640 + x) + 1] > 128 ? "1" : "0";
    }
    r.vga = { rows: bits, linesOk: d.vga.linesOk, bg: Array.from(d.vga.pixels.slice(0, 3)) };
  }
  if (d.audio) {
    const a = PL.audioSamples(s.trace, d.audio.pin, d.audio.clockS, 8000);
    let rms = 0;
    for (const v of a) rms += v * v;
    r.audio = { samples: a.length, rms: Math.sqrt(rms / a.length) };
  }
  out[ex.id] = r;
}
console.log(JSON.stringify(out));
