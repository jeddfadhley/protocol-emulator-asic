// The playground's examples, simulation session and decoders. No DOM here:
// sw/tests/test_playground.py runs every example in node and checks what
// the decoders read back (check_examples.js).
(function (root) {
  "use strict";
  const P = root.PEMU || require("./pemu.js");
  const DATA = root.PEMU_DATA || require("./data.js");
  const CLK_HZ = 50e6;

  // ---------------------------------------------------------------- config

  const DEFAULT_CFG = {
    clkdiv: 0, out_base: 0, out_count: 1, set_base: 0, set_count: 0, side_base: 0,
    in_base: 0, jmp_pin: 0, diff: false, in_right: false, out_right: false,
    autopush: false, autopull: false, push_thresh: 16, pull_thresh: 16, crc_in: false,
    crc_out: false, crc_poly: 0, crc_init: 0, timeout: 0xffff, x: 0, y: 0,
  };
  const cfgOf = (c) => Object.assign({}, DEFAULT_CFG, c || {});

  function configWrites(e, prog, c) {
    const b = P.engineBase(e);
    const pinmap = (c.out_base & 15) | (c.set_base & 15) << 4 | (c.side_base & 15) << 8 | (c.in_base & 15) << 12;
    const pincfg = (c.out_count & 15) | (c.set_count & 7) << 4 | (prog.side_count & 3) << 7
      | (prog.side_pindir ? 1 : 0) << 9 | (c.diff ? 1 : 0) << 10 | (c.jmp_pin & 15) << 12;
    const shift = (c.in_right ? 1 : 0) | (c.out_right ? 2 : 0) | (c.autopush ? 4 : 0) | (c.autopull ? 8 : 0)
      | (c.push_thresh & 15) << 4 | (c.pull_thresh & 15) << 8 | (c.crc_in ? 1 : 0) << 12 | (c.crc_out ? 1 : 0) << 13;
    return [
      [b + P.R_CLKDIV, c.clkdiv], [b + P.R_PROG, P.progReg(prog, 0)], [b + P.R_PINMAP, pinmap],
      [b + P.R_PINCFG, pincfg], [b + P.R_SHIFT, shift], [b + P.R_TIMEOUT, c.timeout],
      [b + P.R_TRAP, prog.trap], [b + P.R_CRCPOLY, c.crc_poly], [b + P.R_CRC, c.crc_init],
      [b + P.R_X, c.x], [b + P.R_Y, c.y],
    ];
  }

  // ---------------------------------------------------------------- traces

  // The 16 pin levels over time, stored as changes: level vals[i] from clock
  // times[i] until the next change. `length` is the number of clocks.
  class Trace {
    constructor() {
      this.times = new Uint32Array(1024);
      this.vals = new Uint16Array(1024);
      this.n = 0;
      this.length = 0;
    }
    push(v) {
      if (this.n === 0 || this.vals[this.n - 1] !== v) {
        if (this.n === this.times.length) {
          const t = new Uint32Array(this.n * 2), w = new Uint16Array(this.n * 2);
          t.set(this.times); w.set(this.vals);
          this.times = t; this.vals = w;
        }
        this.times[this.n] = this.length;
        this.vals[this.n++] = v;
      }
      this.length++;
    }
    index(t) {           // the change in force at clock t
      let lo = 0, hi = this.n - 1;
      while (lo < hi) {
        const mid = (lo + hi + 1) >> 1;
        if (this.times[mid] <= t) lo = mid; else hi = mid - 1;
      }
      return lo;
    }
    at(t) { return this.n ? this.vals[this.index(t)] : 0; }
    bit(t, pin) { return (this.at(t) >> pin) & 1; }
    // [time, level] at every change of one pin
    edges(pin) {
      const out = [];
      let last = -1;
      for (let i = 0; i < this.n; i++) {
        const b = (this.vals[i] >> pin) & 1;
        if (b !== last) { out.push([this.times[i], b]); last = b; }
      }
      return out;
    }
  }

  // ---------------------------------------------------------------- session

  // What is on the 16 pins: uio pins float high (pull-ups) unless driven,
  // uo_out is always driven, pin 15 (ui_in[7]) idles high.
  const wires = (c) => (((c.pin_out & c.pin_oe) | ~c.pin_oe) & 0xff) | (c.pin_out & 0x7f00) | 0x8000;

  class Session {
    constructor(example, sources, cfgs, inputs) {
      this.example = example;
      this.inputs = inputs;
      this.core = new P.Core();
      this.progs = sources.map((s, e) => (s && s.trim() ? P.assemble(s, `engine ${e}`) : null));
      this.cfgs = cfgs.map(cfgOf);
      this.trace = new Trace();  // the pins, from the clock the engines start
      this.received = [[], []]; // words the host read from each RX FIFO
      this.queue = [[], []];    // words the host still has to push
      this.ui = 0xff;
      const host = this.host = {
        write: (a, v) => this.clock({ we: true, addr: a, wdata: v }),
        read: (a) => { const r = this.clock({ re: true, addr: a }); this.clock({ rc: true, addr: a }); return r; },
        push: (e, w) => host.write(P.engineBase(e) + P.R_FIFO, w),
        send: (e, words) => { this.queue[e].push(...words); },
        ramWrite: (addr, words) => { host.write(P.A_RAM_ADDR, addr); words.forEach((w) => host.write(P.A_RAM_DATA, w)); },
        dma: (ch, addr, count, rx = false) => host.write(P.A_DMA0 + ch, P.dmaWord(addr, count, rx)),
        flagSet: (m) => host.write(P.A_FLAG_SET, m),
      };
      this.recording = false;
      host.write(P.A_CTRL, 0);
      if (example.pinOE) host.write(P.A_PIN_OE, example.pinOE);
      if (example.pinSet) host.write(P.A_PIN_SET, example.pinSet);
      this.progs.forEach((p, e) => {
        if (!p) return;
        host.write(P.A_IMEM_SEL, 1 << e);
        p.code.forEach((w, i) => host.write(i, w));
        configWrites(e, p, this.cfgs[e]).forEach(([a, v]) => host.write(a, v));
      });
      if (example.setup) example.setup(host, inputs, this);
      const mask = this.progs.reduce((m, p, e) => m | (p ? 1 << e : 0), 0);
      this.recording = true;
      host.write(P.A_CTRL, mask | (example.loopback ? 4 : 0) | mask << 4);
    }

    clock(bus) {
      const c = this.core;
      const w = wires(c);
      const r = c.clock(this.ui, w & 0xff, bus);
      if (this.recording) this.trace.push(wires(c));
      return r;
    }

    // The host's background work every 32 clocks: keep the TX FIFOs fed from
    // the queues and empty the RX FIFOs, like the RP2040 on the demo board.
    service() {
      const c = this.core;
      for (let e = 0; e < 2; e++) {
        const eng = c.engines[e];
        while (this.queue[e].length && eng.txf.length < P.FIFO_DEPTH) this.host.push(e, this.queue[e].shift());
        if (this.example.drain && this.example.drain[e]) {
          let n = eng.rxf.length;
          while (n-- > 0) this.received[e].push(this.host.read(P.engineBase(e) + P.R_FIFO));
        }
      }
    }

    get length() { return this.trace.length; }

    run(n) {
      const end = this.length + n;
      while (this.length < end) {
        if ((this.length & 31) === 0) this.service();
        if (this.length < end) this.clock(null);
      }
    }

    // The source line of each engine's current instruction.
    pcLine(e) {
      const p = this.progs[e];
      if (!p) return null;
      return p.lines[this.core.engines[e].pc] || null;
    }
  }

  // ---------------------------------------------------------------- decoders

  // 8N1 UART frames on `pin`, `cpb` clocks per bit, LSB first.
  function decodeUart(tr, pin, cpb) {
    const out = [];
    let busyUntil = -1;
    for (const [t, level] of tr.edges(pin)) {
      if (level !== 0 || t < busyUntil || t === 0) continue;
      const mid = (k) => t + Math.floor(cpb * (k + 0.5));
      if (mid(9) >= tr.length) break;
      let byte = 0;
      for (let k = 0; k < 8; k++) byte |= tr.bit(mid(k + 1), pin) << k;
      const ok = tr.bit(mid(0), pin) === 0 && tr.bit(mid(9), pin) === 1;
      out.push({ start: t, end: t + 10 * cpb, value: byte, ok });
      busyUntil = t + Math.floor(9.5 * cpb);
    }
    return out;
  }

  // WS2812: 24 bits per LED, a 1 is a high pulse longer than `threshold` clocks.
  function decodeWs2812(tr, pin, threshold) {
    const e = tr.edges(pin);
    const bits = [];
    for (let i = 0; i + 1 < e.length; i++) {
      if (e[i][1] === 1 && e[i][0] > 0) bits.push({ start: e[i][0], len: e[i + 1][0] - e[i][0] });
    }
    const leds = [];
    for (let i = 0; i + 24 <= bits.length; i += 24) {
      let v = 0;
      for (let k = 0; k < 24; k++) v = (v << 1) | (bits[i + k].len > threshold ? 1 : 0);
      leds.push({ start: bits[i].start, end: bits[i + 23].start + bits[i + 23].len,
        g: (v >> 16) & 255, r: (v >> 8) & 255, b: v & 255 });
    }
    return leds;
  }

  // VGA (vga_sync + vga_pixels pins): the first complete frame as RGBA
  // pixels, sampled from the HSYNC edge the way a monitor does.
  const VGA = { HS: 7, VS: 3, LINE: 1600, H0: 288, CPP: 2, W: 640, H: 480, VBACK: 35 };
  function decodeVga(tr) {
    const falls = tr.edges(VGA.HS).filter(([t, l]) => l === 0 && t > 0).map(([t]) => t);
    const vs = falls.map((t) => tr.bit(t, VGA.VS));
    let start = -1;
    for (let i = 1; i < vs.length; i++) if (vs[i - 1] && !vs[i]) { start = i; break; }
    if (start < 0 || start + VGA.VBACK + VGA.H > falls.length) return null;
    const px = new Uint8ClampedArray(VGA.W * VGA.H * 4);
    const ch = (c, hi, lo) => 85 * (2 * ((c >> hi) & 1) + ((c >> lo) & 1));
    let k = 0;
    for (let y = 0; y < VGA.H; y++) {
      const line = falls[start + VGA.VBACK + y];
      for (let x = 0; x < VGA.W; x++) {
        const t = line + VGA.H0 + VGA.CPP * x + 1;
        while (k + 1 < tr.n && tr.times[k + 1] <= t) k++;
        const c = tr.vals[k];
        const o = 4 * (y * VGA.W + x);
        px[o] = ch(c, 0, 4); px[o + 1] = ch(c, 1, 5); px[o + 2] = ch(c, 2, 6); px[o + 3] = 255;
      }
    }
    const linesOk = falls.slice(2).every((t, i) => t - falls[i + 1] === VGA.LINE);
    return { pixels: px, width: VGA.W, height: VGA.H, frameStart: falls[start], linesOk };
  }

  // A square wave on `pin` as notes: [{start, end, hz}], `clockS` seconds per clock.
  function decodeNotes(tr, pin, clockS) {
    const rises = tr.edges(pin).filter(([t, l]) => l === 1 && t > 0).map(([t]) => t);
    const notes = [];
    let cur = null;
    for (let i = 1; i < rises.length; i++) {
      const period = rises[i] - rises[i - 1];
      if (cur && Math.abs(period - cur.period) <= 1) { cur.end = rises[i]; cur.n++; }
      else { cur = { start: rises[i - 1], end: rises[i], period, n: 1 }; notes.push(cur); }
    }
    return notes.filter((n) => n.n >= 3).map((n) => ({ start: n.start, end: n.end, hz: 1 / (n.period * clockS) }));
  }

  // The speaker pin as audio: `clockS` seconds per recorded clock. Each
  // sample is the share of its interval the pin spent high (a box filter).
  function audioSamples(tr, pin, clockS, rate) {
    const n = Math.floor(tr.length * clockS * rate);
    const out = new Float32Array(n);
    const per = 1 / (clockS * rate);
    const e = tr.edges(pin);
    if (!e.length) return out;
    const cum = new Float64Array(e.length);      // clocks spent high before e[i]
    for (let i = 1; i < e.length; i++) cum[i] = cum[i - 1] + (e[i - 1][1] ? e[i][0] - e[i - 1][0] : 0);
    let k = 0;
    const highBefore = (t) => {                  // t never decreases between calls
      while (k + 1 < e.length && e[k + 1][0] <= t) k++;
      return cum[k] + (e[k][1] ? t - e[k][0] : 0);
    };
    let prev = highBefore(0);
    for (let i = 0; i < n; i++) {
      const next = highBefore((i + 1) * per);
      out[i] = ((next - prev) / per - 0.5) * 0.25;
      prev = next;
    }
    const f = Math.min(n, Math.floor(0.005 * rate));
    for (let i = 0; i < f; i++) { out[i] *= i / f; out[n - 1 - i] *= i / f; }
    return out;
  }

  // ---------------------------------------------------------------- pictures

  function textPicture(lines) {
    const W = 64, H = 32;
    const img = Array.from({ length: H }, () => new Array(W).fill(0));
    for (let x = 0; x < W; x++) { img[0][x] = 1; img[H - 1][x] = 1; }
    for (let y = 0; y < H; y++) { img[y][0] = 1; img[y][W - 1] = 1; }
    const draw = (text, top, scale) => {
      const w = text.length * 6 * scale - scale;
      const left = Math.floor((W - w) / 2);
      [...text].forEach((chr, i) => {
        const rows = DATA.font[chr] || DATA.font[" "];
        rows.forEach((bits, r) => {
          for (let c = 0; c < 5; c++) {
            if (!((bits >> (4 - c)) & 1)) continue;
            for (let dy = 0; dy < scale; dy++) for (let dx = 0; dx < scale; dx++) {
              const y = top + r * scale + dy, x = left + (i * 6 + c) * scale + dx;
              if (y >= 0 && y < H && x >= 0 && x < W) img[y][x] = 1;
            }
          }
        });
      });
    };
    lines = lines.map((l) => l.toUpperCase().slice(0, 10)).filter((l) => l.length).slice(0, 3);
    if (lines.length === 1 && lines[0].length <= 5) draw(lines[0], 9, 2);
    else if (lines.length === 2 && lines[0].length <= 5) { draw(lines[0], 3, 2); draw(lines[1], 21, 1); }
    else {
      const top = Math.floor((H - 9 * lines.length + 2) / 2);
      lines.forEach((l, i) => draw(l, top + 9 * i, 1));
    }
    return img;
  }

  function pictureWords(img) {
    const words = [];
    for (const row of img) for (let w = 0; w < 4; w++) {
      let v = 0;
      for (let k = 0; k < 16; k++) v = (v << 1) | (row[16 * w + k] & 1);
      words.push(v);
    }
    return words;
  }

  // ---------------------------------------------------------------- examples

  const bytesOf = (s) => [...s].map((ch) => ch.charCodeAt(0) & 0xff);
  const UART_DIV = 3, UART_CPB = 8 * (UART_DIV + 1);

  const EXAMPLES = [
    {
      id: "uart",
      title: "Serial text",
      summary: "Engine 0 sends text as a UART, the format of every serial port. Each byte is a low start bit, 8 data bits (least significant first) and a high stop bit. Engine 1 reads it back through the on-chip loopback, so two programs talk with no wires.",
      engines: [
        { prog: "uart_tx", cfg: { clkdiv: UART_DIV, out_base: 12, set_base: 12, set_count: 1, out_right: true, x: 7 } },
        { prog: "uart_rx", cfg: { clkdiv: UART_DIV, in_base: 12, jmp_pin: 12, in_right: true } },
      ],
      loopback: true, pinSet: 1 << 12, drain: [false, true],
      inputs: [{ id: "text", label: "Text to send", value: "Hi from PEMU!" }],
      setup: (h, inp) => h.send(0, bytesOf(inp.text)),
      clocks: (inp) => (inp.text.length + 2) * 10 * UART_CPB,
      pins: [[12, "TX (pin 12)"]],
      note: "32 clocks per bit (1.56 Mbaud) so the whole message fits on screen. On the board, CLKDIV 53 gives 115,200 baud.",
      decode(s) {
        const frames = decodeUart(s.trace, 12, UART_CPB);
        return {
          marks: frames.map((f) => ({ start: f.start, end: f.end, pin: 12, label: f.value >= 32 && f.value < 127 ? String.fromCharCode(f.value) : "0x" + f.value.toString(16), bad: !f.ok })),
          text: { label: "Engine 1 received", value: s.received[1].map((w) => String.fromCharCode(w & 0xff)).join("") },
        };
      },
    },
    {
      id: "leds",
      title: "RGB LEDs",
      summary: "WS2812 LEDs (NeoPixels) read colours from a single wire: a long high pulse is a 1, a short one a 0, 24 bits per LED. Side-set drives the pin while the program decides each bit, so every pulse lands on the exact tick.",
      engines: [{ prog: "ws2812", cfg: { clkdiv: 5, side_base: 13, autopull: true, pull_thresh: 8 } }, null],
      inputs: [{ id: "colors", label: "Colours (hex RGB)", value: "ff0000 ffa000 30d030 0060ff b040ff" }],
      setup: (h, inp) => {
        const words = [];
        inp.colors.split(/[\s,]+/).filter(Boolean).slice(0, 8).forEach((hex) => {
          const v = parseInt(hex.replace(/^#/, ""), 16) || 0;
          words.push(((v >> 8) & 255) << 8, ((v >> 16) & 255) << 8, (v & 255) << 8);   // G, R, B
        });
        h.send(0, words);
      },
      clocks: (inp) => inp.colors.split(/[\s,]+/).filter(Boolean).slice(0, 8).length * 24 * 60 + 600,
      pins: [[13, "DIN (pin 13)"]],
      note: "CLKDIV 5: a 120 ns tick, 1.2 µs per bit, as on the real chip at 50 MHz.",
      decode(s) {
        const leds = decodeWs2812(s.trace, 13, 4.5 * 6);
        return {
          marks: leds.map((l, i) => ({ start: l.start, end: l.end, pin: 13, label: `LED ${i + 1}` })),
          leds,
        };
      },
    },
    {
      id: "vga",
      title: "VGA picture",
      summary: "A 640×480 picture for any monitor. Engine 0 makes the sync pulses, 1600 clocks per line and 525 lines per frame. Engine 1 draws a 64×32 picture that DMA streams from the packet RAM, one row of 4 words per 15 lines. Both are new programs: the chip has no video hardware.",
      engines: [
        { prog: "vga_sync", cfg: { clkdiv: 15, set_base: 3, set_count: 5, crc_init: 478, x: 14 } },
        { prog: "vga_pixels", cfg: { clkdiv: 1, out_base: 1, set_base: 1, set_count: 1, pull_thresh: 15 } },
      ],
      pinOE: 0xff,
      inputs: [{ id: "line1", label: "Big text", value: "PEMU" }, { id: "line2", label: "Small text", value: "HELLO VGA" }],
      setup: (h, inp) => {
        h.ramWrite(0, pictureWords(textPicture([inp.line1, inp.line2])));
        h.dma(1, 0, 255);
        h.flagSet(2);
      },
      clocks: () => 2 * 525 * 1600,
      view: [0, 3400],
      pins: [[7, "HSYNC"], [3, "VSYNC"], [1, "Green"], [6, "Blue (low)"]],
      note: "TinyVGA Pmod wiring on the uio pins. Running two frames takes the simulator about a tenth of a second.",
      decode(s) { return { vga: decodeVga(s.trace) }; },
    },
    {
      id: "music",
      title: "Music box",
      summary: "The packet RAM holds a tune, one word per note. Both engines' DMA channels stream it: engine 0 (the conductor) times each note and raises a flag, and engine 1 (the instrument) plays square waves to a speaker. Once started, the chip plays the whole song with no help from the host.",
      engines: [
        { prog: "conductor", cfg: { clkdiv: 9, crc_init: DATA.music.step } },
        { prog: "tone", cfg: { clkdiv: 0, side_base: 7, crc_init: DATA.music.gap } },
      ],
      pinOE: 1 << 7, timeScale: 50,
      inputs: [{ id: "bpm", label: "Tempo (bpm)", value: "120" }],
      setup: (h, inp, s) => {
        const bpm = Math.min(400, Math.max(30, parseInt(inp.bpm, 10) || 120));
        h.write(P.engineBase(0) + P.R_CRC, Math.round(30 / bpm / (500 / CLK_HZ)) - 3);
        const words = DATA.music.ode;
        h.ramWrite(0, words);
        h.dma(0, 0, words.length);
        h.dma(1, 0, words.length);
        h.flagSet(1);
      },
      clocks: (inp) => Math.round(DATA.music.steps * 30 / Math.min(400, Math.max(30, parseInt(inp.bpm, 10) || 120)) * 1e6) + 200000,
      view: [0, 1200000],
      pins: [[7, "Speaker (pin 7)"]],
      note: "The simulator runs both engines 50 times faster than the chip (CLKDIV 0 and 9 instead of 49 and 499) and plays the result back 50 times slower, which is exactly the chip's waveform.",
      decode(s) {
        const clockS = 50 / CLK_HZ;
        const notes = decodeNotes(s.trace, 7, clockS);
        const name = (hz) => {
          const n = Math.round(69 + 12 * Math.log2(hz / 440));
          return ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"][n % 12] + (Math.floor(n / 12) - 1);
        };
        return {
          marks: notes.map((n) => ({ start: n.start, end: n.end, pin: 7, label: name(n.hz) })),
          notes: notes.map((n) => ({ ...n, name: name(n.hz) })),
          audio: { pin: 7, clockS },
        };
      },
    },
    {
      id: "blink",
      title: "Your own program",
      summary: "A starting point: a square wave on pin 8 and a counter on pins 9 to 11. Change the program, press Run and watch the pins. Every instruction takes exactly 1 + its [delay] ticks.",
      engines: [
        { source: ".program square\n; A square wave on pin 8 (side-set) and a counter on pins 9-11.\n.side_set 1\n.wrap_target\ntop:\n    nop           side 1 [3]   ; high for 4 ticks\n    mov pins, x   side 0       ; X on pins 9-11\n    jmp x--, top  side 0 [2]   ; count down: low for 4 ticks in all\n.wrap\n",
          cfg: { side_base: 8, out_base: 9, out_count: 3, x: 7 } },
        null,
      ],
      clocks: () => 400,
      pins: [[8, "Pin 8"], [9, "Pin 9"], [10, "Pin 10"], [11, "Pin 11"]],
      note: "Engines start at their first instruction. Pins 8 to 14 are the chip's dedicated outputs.",
      decode() { return {}; },
    },
  ];

  const api = { CLK_HZ, Session, Trace, EXAMPLES, DEFAULT_CFG, cfgOf, configWrites, decodeUart, decodeWs2812,
    decodeVga, decodeNotes, audioSamples, textPicture, pictureWords, wires };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PEMU_PLAY = api;
})(typeof window !== "undefined" ? window : globalThis);
