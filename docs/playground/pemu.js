// PEMU in JavaScript: the assembler (sw/pemu/asm.py) and the cycle-accurate
// model (sw/pemu/model.py), translated line for line for the browser
// playground. sw/tests/test_playground.py runs both against the Python
// originals (random programs, random host traffic, every clock compared), so
// a change to either Python file must be made here too.
(function (root) {
  "use strict";

  const M16 = 0xffff;
  const FIFO_DEPTH = 4;
  const RAM_WORDS = 128;
  const IMEM_WORDS = 64;
  const [A_CTRL, A_FLAGS, A_PIN_IN, A_PIN_OUT, A_PIN_OE, A_ID, A_PIN_SET, A_PIN_CLR,
    A_RAM_ADDR, A_RAM_DATA, A_DMA0, A_DMA1, A_FLAG_SET, A_FLAG_CLR, A_VERSION,
    A_PIN_LATE, A_IMEM_SEL] = Array.from({ length: 17 }, (_, i) => 0x60 + i);
  const CHIP_ID = 0x5045;
  const VERSION = 0x0004;
  const [R_CLKDIV, R_PROG, R_PINMAP, R_PINCFG, R_SHIFT, R_TIMEOUT, R_TRAP, R_CRCPOLY,
    R_CRC, R_X, R_Y, R_ISR, R_OSR, R_STATUS, R_TIME, R_FIFO] = Array.from({ length: 16 }, (_, i) => i);
  const PINCFG_MASK = 0xf7ff;
  const SHIFT_MASK = 0x3fff;

  const engineBase = (e) => 0x40 + 16 * e;
  const isPort = (addr) => {
    addr &= 0x7f;
    return ((addr & 0x60) === 0x40 && (addr & 15) === R_FIFO) || addr === A_RAM_DATA;
  };
  const dmaWord = (addr, count, rx) => (addr & 127) | ((count & 255) << 7) | ((rx ? 1 : 0) << 15);
  const rotl = (v, s) => { s &= 15; return ((v << s) | (v >>> (16 - s))) & M16; };
  const rotr = (v, s) => { s &= 15; return ((v >>> s) | (v << (16 - s))) & M16; };
  const bitrev16 = (v) => { let r = 0; for (let i = 0; i < 16; i++) r |= ((v >> i) & 1) << (15 - i); return r; };
  const maskN = (n) => (n >= 32 ? -1 : (1 << n) - 1);
  const nfield = (v) => { v &= 15; return v === 0 ? 16 : v; };
  const crcStep = (crc, poly, bit) => {
    const fb = ((crc >> 15) & 1) ^ (bit & 1);
    crc = (crc << 1) & M16;
    return fb ? crc ^ poly : crc;
  };

  class Effects {
    constructor() {
      this.out_mask = 0; this.out_val = 0; this.oe_mask = 0; this.oe_val = 0;
      this.late_val = 0; this.flag_set = 0; this.flag_clr = 0;
      this.tx_pop = false; this.rx_push = null;
    }
    mirror(base) {
      const b = 1 << (base & 15), m = 1 << ((base + 1) & 15);
      if (this.out_mask & b) {
        this.out_mask |= m;
        this.out_val = (this.out_val & ~m) | (this.out_val & b ? 0 : m);
        this.late_val = (this.late_val & ~m) | (this.late_val & b ? m : 0);
      }
      if (this.oe_mask & b) {
        this.oe_mask |= m;
        this.oe_val = (this.oe_val & ~m) | (this.oe_val & b ? m : 0);
      }
    }
    writePins(dirs, base, n, value, late = false) {
      const m = rotl(maskN(n) & M16, base);
      const v = rotl(value & maskN(n) & M16, base);
      if (dirs) {
        this.oe_mask |= m;
        this.oe_val = (this.oe_val & ~m) | v;
      } else {
        this.out_mask |= m;
        this.out_val = (this.out_val & ~m) | v;
        this.late_val = (this.late_val & ~m) | (late ? m : 0);
      }
    }
  }

  class Engine {
    constructor() {
      this.clkdiv = 0; this.prog = 0xfc00; this.pinmap = 0; this.pincfg = 0; this.shift = 0;
      this.timeout = 0xffff; this.trap = 0; this.crcpoly = 0;
      this.crc = 0; this.x = 0; this.y = 0; this.pc = 0; this.delay = 0;
      this.isr = 0; this.isr_cnt = 0; this.osr = 0; this.osr_cnt = 16;
      this.armed = 0; this.edge_lvl = 0; this.to_cnt = 0; this.time = 0; this.divcnt = 0;
      this.stalled = 0; this.timed_out = 0; this.rx_ovf = 0; this.tx_ovf = 0; this.pop_armed = 0;
      this.wake = 0; this.annul = 0; this.now = 0;
      this.txf = []; this.rxf = [];
    }
    get origin() { return (this.prog & 15) << 2; }
    get wrap_bottom() { return (this.prog >> 4) & 63; }
    get wrap_top() { return (this.prog >> 10) & 63; }
    get out_base() { return this.pinmap & 15; }
    get set_base() { return (this.pinmap >> 4) & 15; }
    get side_base() { return (this.pinmap >> 8) & 15; }
    get in_base() { return (this.pinmap >> 12) & 15; }
    get out_count() { return nfield(this.pincfg); }
    get set_count() { return Math.min((this.pincfg >> 4) & 7, 5); }
    get side_count() { return (this.pincfg >> 7) & 3; }
    get side_pindir() { return (this.pincfg >> 9) & 1; }
    get diff() { return (this.pincfg >> 10) & 1; }
    get jmp_pin() { return (this.pincfg >> 12) & 15; }
    get in_right() { return this.shift & 1; }
    get out_right() { return (this.shift >> 1) & 1; }
    get autopush() { return (this.shift >> 2) & 1; }
    get autopull() { return (this.shift >> 3) & 1; }
    get push_thresh() { return nfield(this.shift >> 4); }
    get pull_thresh() { return nfield(this.shift >> 8); }
    get crc_in() { return (this.shift >> 12) & 1; }
    get crc_out() { return (this.shift >> 13) & 1; }

    status() {
      return ((this.pc & 31) | (this.txf.length << 5) | (this.rxf.length << 8) | (this.stalled << 11)
        | ((this.pc >> 5) << 15) | (this.timed_out << 12) | (this.rx_ovf << 13) | (this.tx_ovf << 14));
    }
    restart() {
      this.pc = this.delay = this.isr = this.isr_cnt = this.osr = 0;
      this.osr_cnt = 16;
      this.armed = this.edge_lvl = this.to_cnt = this.time = this.divcnt = this.stalled = 0;
      this.wake = this.annul = 0;
    }
    source(src, pinIn) {
      switch (src) {
        case 0: return rotr(pinIn, this.in_base);
        case 1: return this.x;
        case 2: return this.y;
        case 3: return 0;
        case 4: return this.crc;
        case 5: return this.now;
        case 6: return this.isr;
        default: return this.osr;
      }
    }
    cond(code, pinIn, flags) {
      const sel = code & 15;
      let r;
      if (sel === 0) r = this.x < this.y;
      else if (sel === 1) r = this.x === this.y;
      else if (sel === 2) r = (this.x >> 15) & 1;
      else if (sel === 3) r = this.x & 1;
      else if (sel === 4) r = this.txf.length > 0;
      else if (sel === 5) r = this.rxf.length >= FIFO_DEPTH;
      else if (sel === 6) r = (pinIn >> this.jmp_pin) & 1;
      else if (sel === 7) r = this.osr_cnt >= this.pull_thresh;
      else r = (flags >> (sel - 8)) & 1;
      return Boolean(r) !== Boolean(code & 16);
    }
    nextPc(pc) { return pc === this.wrap_top ? this.wrap_bottom : (pc + 1) & 63; }

    execute(pinIn, flags, imem, fx) {
      this.now = this.time;
      this.time = (this.time + 1) & M16;
      if (this.delay) {
        this.delay -= 1;
        this.stalled = 0;
        return;
      }
      const ins = imem[(this.origin + this.pc) & 63];
      const op = ins >> 13;
      const sc = this.side_count;
      const sd = (ins >> 8) & 31;
      const dly = sd & maskN(5 - sc);
      const pin = (i) => (pinIn >> (i & 15)) & 1;
      const rel = (i) => pin(this.in_base + i);

      let done = true, trapped = false, jump = null, skip = false, wake = 0;
      const pins = new Effects();
      const cancelled = this.annul;
      this.annul = 0;

      if (cancelled) {
        // skipped: no operation, but side-set and delay as usual
      } else if (op === 0 || op === 6) { // JMP
        const cond = (ins >> 5) & 7, addr = (ins & 31) | (op === 6 ? 32 : 0);
        const take = [true, this.x === 0, this.x !== 0, this.y === 0, this.y !== 0,
          this.x !== this.y, pin(this.jmp_pin) === 1, this.osr_cnt < this.pull_thresh][cond];
        if (cond === 2) this.x = (this.x - 1) & M16;
        if (cond === 4) this.y = (this.y - 1) & M16;
        if (take) jump = addr;
      } else if (op === 1) { // WAIT
        const pol = (ins >> 7) & 1, src = (ins >> 5) & 3, tmo = (ins >> 4) & 1, idx = ins & 15;
        if (src === 0) done = pin(idx) === pol;
        else if (src === 1) done = rel(idx) === pol;
        else if (src === 2) {
          done = ((flags >> (idx & 7)) & 1) === pol;
          if (done && pol) fx.flag_clr |= 1 << (idx & 7);
        } else if (idx & 8) {
          const p = rel(idx & 7);
          done = Boolean(this.armed) && p !== this.edge_lvl;
          if (!this.armed) { this.armed = 1; this.edge_lvl = p; }
        } else {
          const p = rel(idx & 7);
          done = Boolean(this.armed) && p === pol;
          if (!done && p !== pol) this.armed = 1;
        }
        if (!done && this.wake && this.cond(this.wake, pinIn, flags)) done = true;
        trapped = !done && tmo && this.to_cnt === this.timeout;
      } else if (op === 2) { // IN
        const src = (ins >> 5) & 7, n = nfield(ins);
        const data = this.source(src, pinIn) & maskN(n) & M16;
        const nisr = this.in_right ? ((this.isr >>> n) | (data << (16 - n))) & M16
          : ((this.isr << n) | data) & M16;
        const ncnt = Math.min(this.isr_cnt + n, 16);
        if (this.autopush && ncnt >= this.push_thresh) {
          if (this.rxf.length >= FIFO_DEPTH) done = false;
          else { fx.rx_push = nisr; this.isr = 0; this.isr_cnt = 0; }
        } else {
          this.isr = nisr; this.isr_cnt = ncnt;
        }
        if (done && this.crc_in && n === 1) this.crc = crcStep(this.crc, this.crcpoly, data);
      } else if (op === 3) { // OUT
        const dst = (ins >> 5) & 7, n = nfield(ins);
        let src, base;
        if (this.autopull && this.osr_cnt >= this.pull_thresh) {
          if (!this.txf.length) done = false;
          else { src = this.txf[0]; base = 0; fx.tx_pop = true; }
        } else {
          src = this.osr; base = this.osr_cnt;
        }
        if (done) {
          let data;
          if (this.out_right) {
            data = src & maskN(n) & M16;
            this.osr = src >>> n;
          } else {
            data = src >>> (16 - n);
            this.osr = (src << n) & M16;
          }
          this.osr_cnt = Math.min(base + n, 16);
          if (dst === 0) pins.writePins(0, this.out_base, n, data);
          else if (dst === 1) this.x = data;
          else if (dst === 2) this.y = data;
          else if (dst === 4) pins.writePins(1, this.out_base, n, data);
          else if (dst === 5) jump = data & 63;
          else if (dst === 6) { this.isr = data; this.isr_cnt = n; }
          if (this.crc_out && n === 1) this.crc = crcStep(this.crc, this.crcpoly, data);
        }
      } else if (op === 4) { // PUSH / PULL
        const isPull = (ins >> 7) & 1, cond = (ins >> 6) & 1, block = (ins >> 5) & 1;
        if (!isPull) {
          if (cond && this.isr_cnt < this.push_thresh) { /* nothing */ }
          else if (this.rxf.length >= FIFO_DEPTH) {
            if (block) done = false;
            else { this.isr = 0; this.isr_cnt = 0; this.rx_ovf = 1; }
          } else {
            fx.rx_push = this.isr; this.isr = 0; this.isr_cnt = 0;
          }
        } else {
          if (cond && this.osr_cnt < this.pull_thresh) { /* nothing */ }
          else if (!this.txf.length) {
            if (block) done = false;
            else { this.osr = this.x; this.osr_cnt = 0; }
          } else {
            this.osr = this.txf[0]; this.osr_cnt = 0; fx.tx_pop = true;
          }
        }
      } else if (op === 5) { // MOV
        const dst = (ins >> 5) & 7, mop = (ins >> 3) & 3, src = ins & 7;
        let v = this.source(src, pinIn);
        if (mop === 1) v = ~v & M16;
        else if (mop === 2) v = bitrev16(v);
        if (dst === 0) pins.writePins(0, this.out_base, this.out_count, v, mop === 3);
        else if (dst === 1) this.x = mop === 3 ? (this.x + v) & M16 : v;
        else if (dst === 2) this.y = mop === 3 ? (this.y + v) & M16 : v;
        else if (dst === 3) this.crc = v;
        else if (dst === 4) pins.writePins(1, this.out_base, this.out_count, v);
        else if (dst === 5) jump = v & 63;
        else if (dst === 6) { this.isr = v; this.isr_cnt = 0; }
        else if (dst === 7) { this.osr = v; this.osr_cnt = 0; }
      } else if (op === 7) { // SET
        const dst = (ins >> 5) & 7, imm = ins & 31;
        if (dst === 0) pins.writePins(0, this.set_base, this.set_count, imm);
        else if (dst === 1) this.x = imm;
        else if (dst === 2) this.y = imm;
        else if (dst === 3) {
          if (imm & 16) fx.flag_clr |= 1 << (imm & 7);
          else fx.flag_set |= 1 << (imm & 7);
        } else if (dst === 4) pins.writePins(1, this.set_base, this.set_count, imm);
        else if (dst === 5) wake = 32 | imm;
        else if (dst === 6) skip = this.cond(imm, pinIn, flags);
      }

      fx.out_mask = pins.out_mask; fx.out_val = pins.out_val; fx.late_val = pins.late_val;
      fx.oe_mask = pins.oe_mask; fx.oe_val = pins.oe_val;
      if (sc) fx.writePins(this.side_pindir, this.side_base, sc, sd >> (5 - sc));
      if (this.diff) fx.mirror(this.out_base);

      if (done) {
        this.pc = jump !== null ? jump : this.nextPc(this.pc);
        this.delay = dly;
        this.to_cnt = 0; this.armed = 0; this.stalled = 0;
        this.wake = wake;
        this.annul = skip ? 1 : 0;
      } else if (trapped) {
        this.pc = this.trap; this.delay = 0; this.to_cnt = 0; this.armed = 0;
        this.timed_out = 1; this.stalled = 1; this.wake = 0;
      } else {
        this.to_cnt = (this.to_cnt + 1) & M16;
        this.stalled = 1;
      }
    }
  }

  class Core {
    constructor(strap = 0) { this.reset(strap); }
    reset(strap = 0) {
      this.imem = [new Array(IMEM_WORDS).fill(0), new Array(IMEM_WORDS).fill(0)];
      this.imem_sel = 3;
      this.ram = new Array(RAM_WORDS).fill(0);
      this.ram_addr = 0;
      this.dma = [0, 0];
      this.engines = [new Engine(), new Engine()];
      this.ctrl = 0; this.flags = 0;
      this.pin_out = strap ? 0x7f00 : 0;
      this.pin_oe = 0; this.pin_late = 0;
      this.pin_prev = this.pin_out;
      this.sync1 = 0; this.sync2 = 0; this.pulses = 0;
    }
    get loopback() { return (this.ctrl >> 2) & 1; }
    pinIn() {
      if (this.loopback) {
        const low = ((this.pin_out & this.pin_oe) | ~this.pin_oe) & 0xff;
        return (this.pin_out & 0xff00) | low;
      }
      return this.sync2;
    }
    padEarly() { return (this.pin_late & this.pin_prev) | (~this.pin_late & this.pin_out) & M16; }

    read(addr) {
      addr &= 0x7f;
      if (addr < 0x40) return 0;
      if (addr < 0x60) {
        const e = this.engines[(addr >> 4) & 1];
        switch (addr & 15) {
          case R_CLKDIV: return e.clkdiv;
          case R_PROG: return e.prog;
          case R_PINMAP: return e.pinmap;
          case R_PINCFG: return e.pincfg;
          case R_SHIFT: return e.shift;
          case R_TIMEOUT: return e.timeout;
          case R_TRAP: return e.trap;
          case R_CRCPOLY: return e.crcpoly;
          case R_CRC: return e.crc;
          case R_X: return e.x;
          case R_Y: return e.y;
          case R_ISR: return e.isr;
          case R_OSR: return e.osr;
          case R_STATUS: return e.status();
          case R_TIME: return e.time;
          default: return e.rxf.length ? e.rxf[0] : 0;
        }
      }
      switch (addr) {
        case A_CTRL: return this.ctrl & 7;
        case A_FLAGS: return this.flags;
        case A_PIN_IN: return this.pinIn();
        case A_PIN_OUT: return this.pin_out;
        case A_PIN_OE: return this.pin_oe;
        case A_ID: return CHIP_ID;
        case A_RAM_ADDR: return this.ram_addr;
        case A_RAM_DATA: return this.ram[this.ram_addr];
        case A_DMA0: return this.dma[0];
        case A_DMA1: return this.dma[1];
        case A_VERSION: return VERSION;
        case A_PIN_LATE: return this.pin_late;
        case A_IMEM_SEL: return this.imem_sel;
        default: return 0;
      }
    }

    // One clock. bus: {we, re, rc, addr, wdata}. Returns this cycle's read data.
    clock(ui_in = 0, uio_in = 0, bus = null) {
      const we = !!(bus && bus.we), re = !!(bus && bus.re), rc = !!(bus && bus.rc);
      const addr = bus ? (bus.addr || 0) & 0x7f : 0;
      const wdata = bus ? (bus.wdata || 0) & M16 : 0;
      const rdata = this.read(addr);
      const pinIn = this.pinIn();
      const p = this.pulses;
      this.pulses = we && addr === A_CTRL ? wdata & 0x3f0 : 0;

      const fxs = [];
      for (let i = 0; i < 2; i++) {
        const e = this.engines[i];
        const fx = new Effects();
        fxs.push(fx);
        const en = (this.ctrl >> i) & 1;
        if ((p >> (4 + i)) & 1) { e.restart(); continue; }
        let tick;
        if (en) {
          tick = e.divcnt >= e.clkdiv;
          e.divcnt = tick ? 0 : (e.divcnt + 1) & M16;
        } else {
          e.divcnt = 0;
          tick = (p >> (6 + i)) & 1;
        }
        if (tick) e.execute(pinIn, this.flags, this.imem[i], fx);
      }

      let pinOut = this.pin_out, pinOe = this.pin_oe, flags = this.flags, pinLate = this.pin_late;
      for (const fx of fxs) {
        pinLate = (pinLate & ~fx.out_mask) | (fx.late_val & fx.out_mask);
        pinOut = (pinOut & ~fx.out_mask) | (fx.out_val & fx.out_mask);
        pinOe = (pinOe & ~fx.oe_mask) | (fx.oe_val & fx.oe_mask);
        flags = (flags & ~fx.flag_clr) | fx.flag_set;
      }

      const ramWe = we && addr === A_RAM_DATA;
      const ramRe = re && addr === A_RAM_DATA;
      const dmaPush = [null, null], dmaPop = [false, false];
      const dmaNext = this.dma.slice();
      let ram = this.ram;
      for (let c = 0; c < 2; c++) {
        const d = this.dma[c];
        const daddr = d & 127, dcount = (d >> 7) & 255, drx = d >> 15;
        const fifo = engineBase(c) + R_FIFO;
        const e = this.engines[c];
        if (dcount === 0) continue;
        const ok = drx ? (e.rxf.length > 0 && !(rc && addr === fifo) && !ramWe)
          : (e.txf.length < FIFO_DEPTH && !(we && addr === fifo) && !ramRe);
        if (!ok) continue;
        if (drx) {
          ram = ram.slice();
          ram[daddr] = e.rxf[0];
          dmaPop[c] = true;
        } else {
          dmaPush[c] = this.ram[daddr];
        }
        dmaNext[c] = dmaWord(daddr + 1, dcount === 255 ? dcount : dcount - 1, drx);
        break;
      }

      for (let i = 0; i < 2; i++) {
        const e = this.engines[i], fx = fxs[i], base = engineBase(i);
        const txPush = we && addr === base + R_FIFO;
        const rxLoad = re && addr === base + R_FIFO;
        const rxCommit = rc && addr === base + R_FIFO;
        const txf = e.txf, rxf = e.rxf;
        const rxPop = rxCommit && e.pop_armed && rxf.length > 0;
        if (rxLoad) e.pop_armed = rxf.length > 0 ? 1 : 0;
        else if (rxCommit) e.pop_armed = 0;
        let ntx = fx.tx_pop ? txf.slice(1) : txf.slice();
        if (txPush) {
          if (txf.length < FIFO_DEPTH) ntx.push(wdata);
          else e.tx_ovf = 1;
        }
        if (dmaPush[i] !== null) ntx.push(dmaPush[i]);
        let nrx = (rxPop || dmaPop[i]) && rxf.length ? rxf.slice(1) : rxf.slice();
        if (fx.rx_push !== null) nrx.push(fx.rx_push);
        if ((p >> (8 + i)) & 1) { ntx = []; nrx = []; }
        e.txf = ntx; e.rxf = nrx;
      }

      this.ram = ram; this.dma = dmaNext;
      if (rc && addr === A_RAM_DATA) this.ram_addr = (this.ram_addr + 1) % RAM_WORDS;

      if (we) {
        if (addr < 0x40) {
          for (let i = 0; i < 2; i++) if ((this.imem_sel >> i) & 1) this.imem[i][addr] = wdata;
        } else if (addr < 0x60) {
          const e = this.engines[(addr >> 4) & 1];
          switch (addr & 15) {
            case R_CLKDIV: e.clkdiv = wdata; break;
            case R_PROG: e.prog = wdata; break;
            case R_PINMAP: e.pinmap = wdata; break;
            case R_PINCFG: e.pincfg = wdata & PINCFG_MASK; break;
            case R_SHIFT: e.shift = wdata & SHIFT_MASK; break;
            case R_TIMEOUT: e.timeout = wdata; break;
            case R_TRAP: e.trap = wdata & 63; break;
            case R_CRCPOLY: e.crcpoly = wdata; break;
            case R_CRC: e.crc = wdata; break;
            case R_X: e.x = wdata; break;
            case R_Y: e.y = wdata; break;
            case R_STATUS:
              if ((wdata >> 12) & 1) e.timed_out = 0;
              if ((wdata >> 13) & 1) e.rx_ovf = 0;
              if ((wdata >> 14) & 1) e.tx_ovf = 0;
              break;
            default: break;
          }
        } else if (addr === A_CTRL) this.ctrl = wdata & 7;
        else if (addr === A_IMEM_SEL) this.imem_sel = wdata & 3;
        else if (addr === A_FLAGS) flags = wdata & 0xff;
        else if (addr === A_PIN_OUT) { pinOut = wdata; pinLate = 0; }
        else if (addr === A_PIN_OE) pinOe = wdata;
        else if (addr === A_PIN_SET) { pinOut |= wdata; pinLate &= ~wdata; }
        else if (addr === A_PIN_CLR) { pinOut &= ~wdata & M16; pinLate &= ~wdata; }
        else if (addr === A_RAM_ADDR) this.ram_addr = wdata % RAM_WORDS;
        else if (addr === A_RAM_DATA) {
          this.ram = this.ram.slice();
          this.ram[this.ram_addr] = wdata;
          this.ram_addr = (this.ram_addr + 1) % RAM_WORDS;
        } else if (addr === A_DMA0 || addr === A_DMA1) this.dma[addr - A_DMA0] = wdata;
        else if (addr === A_FLAG_SET) flags |= wdata & 0xff;
        else if (addr === A_FLAG_CLR) flags &= ~wdata & 0xff;
      }

      this.pin_prev = this.pin_out;
      this.pin_out = pinOut & M16; this.pin_oe = pinOe & M16; this.flags = flags & 0xff;
      this.pin_late = pinLate & M16;
      this.sync2 = this.sync1;
      this.sync1 = ((ui_in & 0xff) << 8) | (uio_in & 0xff);
      return rdata;
    }
  }

  // ------------------------------------------------------------ assembler

  const JMP_CONDS = { "": 0, "!x": 1, "x--": 2, "!y": 3, "y--": 4, "x!=y": 5, "pin": 6, "!osre": 7 };
  const WAIT_SRCS = { gpio: 0, pin: 1, flag: 2, edge: 3 };
  const SOURCES = { pins: 0, x: 1, y: 2, null: 3, crc: 4, time: 5, isr: 6, osr: 7 };
  const OUT_DSTS = { pins: 0, x: 1, y: 2, null: 3, pindirs: 4, pc: 5, isr: 6 };
  const MOV_DSTS = { pins: 0, x: 1, y: 2, crc: 3, pindirs: 4, pc: 5, isr: 6, osr: 7 };
  const SET_DSTS = { pins: 0, x: 1, y: 2, pindirs: 4 };
  const SET_WAKE = 5, SET_SKIP = 6;
  const CONDS = { "x<y": 0, "x==y": 1, "xneg": 2, "xodd": 3, "txdata": 4, "rxfull": 5, "pin": 6, "osre": 7 };
  const COND_ALIASES = { "x>=y": "!x<y", "x!=y": "!x==y" };
  const MAX_WORDS = 64;

  class AsmError extends Error {}
  const has = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
  const lookup = (o, k) => {
    if (!has(o, k)) throw new AsmError(`'${k}'`);
    return o[k];
  };
  const pyStrip = (s) => s.replace(/^\s+|\s+$/g, "");
  const pySplit = (s) => pyStrip(s).split(/\s+/).filter((t) => t.length);

  function parseInt0(tok) {
    // Python int(tok, 0)
    const m = /^([+-]?)(0[xX](?:_?[0-9a-fA-F])+|0[bB](?:_?[01])+|0[oO](?:_?[0-7])+|0(?:_?0)*|[1-9](?:_?[0-9])*)$/.exec(tok);
    if (!m) return null;
    let body = m[2].replace(/_/g, "");
    let v;
    if (/^0[xX]/.test(body)) v = parseInt(body.slice(2), 16);
    else if (/^0[bB]/.test(body)) v = parseInt(body.slice(2), 2);
    else if (/^0[oO]/.test(body)) v = parseInt(body.slice(2), 8);
    else v = parseInt(body, 10);
    return m[1] === "-" ? -v : v;
  }

  function num(tok, defines, labels = null) {
    tok = pyStrip(tok);
    if (labels !== null && has(labels, tok)) return labels[tok];
    if (has(defines, tok)) return defines[tok];
    const v = parseInt0(tok);
    if (v === null) throw new AsmError(`bad number or unknown symbol '${tok}'`);
    return v;
  }

  function count(tok, defines, what) {
    const n = num(tok, defines);
    if (!(n >= 1 && n <= 16)) throw new AsmError(`${what} bit count must be 1-16, got ${n}`);
    return n & 15;
  }

  function cond(text, defines) {
    let t = pySplit(text).join(" ");
    const key = t.replace(/ /g, "").toLowerCase();
    if (has(COND_ALIASES, key)) t = COND_ALIASES[key];
    const neg = t.startsWith("!");
    if (neg) t = pyStrip(t.slice(1));
    let code;
    if (t.toLowerCase().startsWith("flag")) {
      const idx = num(t.slice(4), defines);
      if (!(idx >= 0 && idx < 8)) throw new AsmError("flag index must be 0-7");
      code = 8 + idx;
    } else if (has(CONDS, t.replace(/ /g, "").toLowerCase())) {
      code = CONDS[t.replace(/ /g, "").toLowerCase()];
    } else {
      throw new AsmError(`unknown condition '${text}'`);
    }
    return code | (neg ? 16 : 0);
  }

  function condName(code) {
    const sel = code & 15;
    let name = sel >= 8 ? `flag ${sel - 8}` : Object.keys(CONDS).find((k) => CONDS[k] === sel);
    if (code & 16) return { "x<y": "x>=y", "x==y": "x!=y" }[name] || "!" + name;
    return name;
  }

  function assemble(source, name = null) {
    const defines = {}, labels = {};
    let sideCount = 0, sidePindir = false, wrapBottom = null, wrapTop = null, trapLabel = null;
    let progName = name || "program";
    const lines = [];
    const rawLines = source.split(/\r\n|\r|\n/);
    if (rawLines.length && rawLines[rawLines.length - 1] === "") rawLines.pop();
    rawLines.forEach((raw, i) => {
      const lineno = i + 1;
      let text = pyStrip(raw.split(/;|\/\//)[0]);
      if (!text) return;
      try {
        for (;;) {
          const m = /^([A-Za-z_]\w*):\s*(.*)$/.exec(text);
          if (!m) break;
          if (has(labels, m[1])) throw new AsmError(`duplicate label '${m[1]}'`);
          labels[m[1]] = lines.length;
          text = m[2];
        }
        if (!text) return;
        if (text.startsWith(".") && !text.toLowerCase().startsWith(".word")) {
          const parts = pySplit(text);
          const need = (k) => { if (parts.length <= k) throw new AsmError("list index out of range"); return parts[k]; };
          const d = parts[0].toLowerCase();
          if (d === ".program") progName = need(1);
          else if (d === ".side_set") {
            sideCount = num(need(1), defines);
            if (!(sideCount >= 0 && sideCount <= 3)) throw new AsmError("side_set count must be 0-3");
            sidePindir = parts.length > 2 && parts[2].toLowerCase() === "pindirs";
          } else if (d === ".define") {
            const k = need(1);
            defines[k] = num(need(2), defines);
          } else if (d === ".wrap_target") wrapBottom = lines.length;
          else if (d === ".wrap") {
            if (!lines.length) throw new AsmError(".wrap before any instruction");
            wrapTop = lines.length - 1;
          } else if (d === ".trap") trapLabel = need(1);
          else throw new AsmError(`unknown directive ${d}`);
          return;
        }
        lines.push([lineno, text]);
      } catch (exc) {
        if (exc instanceof AsmError) { const e = new AsmError(`line ${lineno}: ${exc.message}`); e.line = lineno; throw e; }
        throw exc;
      }
    });
    if (lines.length > MAX_WORDS) {
      throw new AsmError(`program is ${lines.length} instructions; the limit is ${MAX_WORDS}`);
    }
    const code = [];
    for (const [lineno, text] of lines) {
      try {
        code.push(encode(text, defines, labels, sideCount));
      } catch (exc) {
        if (exc instanceof AsmError) { const e = new AsmError(`line ${lineno}: ${exc.message}: '${text}'`); e.line = lineno; throw e; }
        throw exc;
      }
    }
    const trap = trapLabel === null ? 0 : num(trapLabel, defines, labels);
    return {
      name: progName, code, labels,
      wrap_bottom: wrapBottom === null ? 0 : wrapBottom,
      wrap_top: wrapTop === null ? code.length - 1 : wrapTop,
      side_count: sideCount, side_pindir: sidePindir, trap, defines,
      lines: lines.map((l) => l[0]),
    };
  }

  function progReg(program, origin) {
    return ((origin >> 2) & 15) | (program.wrap_bottom << 4) | (program.wrap_top << 10);
  }

  function encode(text, defines, labels, sideCount) {
    if (text.toLowerCase().startsWith(".word")) {
      const w = num(text.slice(5), defines);
      if (!(w >= 0 && w < 65536)) throw new AsmError(".word value out of range");
      return w;
    }
    let delay = 0;
    let m = /\[([^\]]+)\]\s*$/.exec(text);
    if (m) { delay = num(m[1], defines); text = pyStrip(text.slice(0, m.index)); }
    let side = null;
    m = /\bside\s+(\S+)\s*$/.exec(text);
    if (m) { side = num(m[1], defines); text = pyStrip(text.slice(0, m.index)); }
    m = /\[([^\]]+)\]\s*$/.exec(text);
    if (m) { delay = num(m[1], defines); text = pyStrip(text.slice(0, m.index)); }
    const maxDelay = (1 << (5 - sideCount)) - 1;
    if (!(delay >= 0 && delay <= maxDelay)) throw new AsmError(`delay ${delay} out of range 0-${maxDelay}`);
    let sd;
    if (sideCount) {
      if (side === null) throw new AsmError("side-set value required (.side_set is in use)");
      if (!(side >= 0 && side < (1 << sideCount))) throw new AsmError(`side value ${side} does not fit in ${sideCount} bits`);
      sd = (side << (5 - sideCount)) | delay;
    } else {
      if (side !== null) throw new AsmError("'side' used without .side_set");
      sd = delay;
    }
    const sp = text.indexOf(" ");
    const mnem = (sp < 0 ? text : text.slice(0, sp)).toLowerCase();
    const rest = sp < 0 ? "" : text.slice(sp + 1);
    const args = pyStrip(rest) ? rest.split(",").map(pyStrip) : [];
    const [op, operand] = operands(mnem, args, pyStrip(rest), defines, labels);
    return (op << 13) | (sd << 8) | operand;
  }

  function arg(args, i) {
    if (i >= args.length) throw new AsmError("list index out of range");
    return args[i];
  }

  function operands(mnem, args, rest, defines, labels) {
    if (mnem === "nop") return [5, (MOV_DSTS.y << 5) | SOURCES.y];
    if (mnem === "jmp") {
      let c, target;
      if (args.length === 1) { c = ""; target = args[0]; }
      else { c = arg(args, 0).toLowerCase().replace(/ /g, ""); target = arg(args, 1); }
      if (!has(JMP_CONDS, c)) throw new AsmError(`unknown jmp condition '${c}'`);
      const addr = num(target, defines, labels);
      if (!(addr >= 0 && addr < MAX_WORDS)) throw new AsmError("jump target out of range");
      return [addr & 32 ? 6 : 0, (JMP_CONDS[c] << 5) | (addr & 31)];
    }
    if (mnem === "wait") {
      let toks = pySplit(rest.replace(/,/g, " "));
      if (!toks.length) throw new AsmError("list index out of range");
      const timeout = toks[toks.length - 1].toLowerCase() === "timeout";
      if (timeout) toks = toks.slice(0, -1);
      if (toks.length !== 3) throw new AsmError("expected 'wait <0|1|any> <gpio|pin|flag|edge> <index> [timeout]'");
      const src = lookup(WAIT_SRCS, toks[1].toLowerCase());
      let idx = num(toks[2], defines);
      if (!(idx >= 0 && idx < (src === 2 || src === 3 ? 8 : 16))) throw new AsmError("wait index out of range");
      let pol;
      if (toks[0].toLowerCase() === "any") {
        if (src !== 3) throw new AsmError("'any' only applies to edge waits");
        pol = 0; idx |= 8;
      } else {
        pol = num(toks[0], defines);
        if (pol !== 0 && pol !== 1) throw new AsmError("wait polarity must be 0 or 1");
      }
      return [1, (pol << 7) | (src << 5) | ((timeout ? 1 : 0) << 4) | idx];
    }
    if (mnem === "in") return [2, (lookup(SOURCES, arg(args, 0).toLowerCase()) << 5) | count(arg(args, 1), defines, "in")];
    if (mnem === "out") return [3, (lookup(OUT_DSTS, arg(args, 0).toLowerCase()) << 5) | count(arg(args, 1), defines, "out")];
    if (mnem === "push" || mnem === "pull") {
      const toks = pySplit(rest).map((t) => t.toLowerCase());
      const condWord = mnem === "push" ? "iffull" : "ifempty";
      let block = 1, c = 0;
      for (const t of toks) {
        if (t === condWord) c = 1;
        else if (t === "block") block = 1;
        else if (t === "noblock") block = 0;
        else throw new AsmError(`unexpected '${t}'`);
      }
      return [4, ((mnem === "pull" ? 1 : 0) << 7) | (c << 6) | (block << 5)];
    }
    if (mnem === "skip" || mnem === "wake") {
      if (!rest) throw new AsmError(`${mnem} needs a condition`);
      return [7, ((mnem === "skip" ? SET_SKIP : SET_WAKE) << 5) | cond(rest, defines)];
    }
    if (mnem === "mov") {
      const dst = lookup(MOV_DSTS, arg(args, 0).toLowerCase());
      let src = arg(args, 1).replace(/ /g, "").toLowerCase();
      let mop = 0;
      const m = /^([xy])\+(\w+)$/.exec(src);
      if (m) {
        if (m[1] !== args[0].toLowerCase()) throw new AsmError("add form is 'mov x, x + src' or 'mov y, y + src'");
        return [5, (dst << 5) | (3 << 3) | lookup(SOURCES, m[2])];
      }
      if (src.endsWith("late")) {
        if (args[0].toLowerCase() !== "pins") throw new AsmError("'late' only applies to mov pins");
        return [5, (dst << 5) | (3 << 3) | lookup(SOURCES, src.slice(0, -4))];
      }
      if (src.startsWith("::")) { mop = 2; src = src.slice(2); }
      else if (src.slice(0, 1) === "~" || src.slice(0, 1) === "!") { mop = 1; src = src.slice(1); }
      return [5, (dst << 5) | (mop << 3) | lookup(SOURCES, src)];
    }
    if (mnem === "set" || mnem === "clr") {
      if (arg(args, 0).toLowerCase() === "flag") {
        const idx = num(arg(args, 1), defines);
        if (!(idx >= 0 && idx < 8)) throw new AsmError("flag index must be 0-7");
        return [7, (3 << 5) | ((mnem === "clr" ? 1 : 0) << 4) | idx];
      }
      if (mnem === "clr") throw new AsmError("clr only applies to flags");
      const imm = num(arg(args, 1), defines);
      if (!(imm >= 0 && imm < 32)) throw new AsmError("set value must be 0-31");
      return [7, (lookup(SET_DSTS, args[0].toLowerCase()) << 5) | imm];
    }
    throw new AsmError(`unknown instruction '${mnem}'`);
  }

  const inv = (o) => Object.fromEntries(Object.entries(o).map(([k, v]) => [v, k]));
  const JMP_NAMES = inv(JMP_CONDS), WAIT_NAMES = inv(WAIT_SRCS), SRC_NAMES = inv(SOURCES);
  const OUT_NAMES = inv(OUT_DSTS), MOV_NAMES = inv(MOV_DSTS), SET_NAMES = inv(SET_DSTS);
  const hex4 = (w) => "0x" + w.toString(16).padStart(4, "0");

  function disassemble(word, sideCount = 0) {
    word &= 0xffff;
    const op = word >> 13, sd = (word >> 8) & 31;
    const side = sideCount ? sd >> (5 - sideCount) : null;
    const delay = sd & ((1 << (5 - sideCount)) - 1);
    const raw = `.word ${hex4(word)}`;
    const n = (word & 15) || 16;
    let text;
    if (op === 0 || op === 6) {
      const c = JMP_NAMES[(word >> 5) & 7];
      text = `jmp ${c ? c + ", " : ""}${(word & 31) | (op === 6 ? 32 : 0)}`;
    } else if (op === 1) {
      const src = (word >> 5) & 3;
      text = `wait ${(word >> 7) & 1} ${WAIT_NAMES[src]} ${word & 15}`;
      if (src === 2 && word & 8) return raw;
      if (src === 3 && word & 8) {
        if (word & 128) return raw;
        text = `wait any edge ${word & 7}`;
      } else if (src === 3) text = `wait ${(word >> 7) & 1} edge ${word & 7}`;
      if (word & 16) text += " timeout";
    } else if (op === 2) {
      if (word & 16) return raw;
      text = `in ${SRC_NAMES[(word >> 5) & 7]}, ${n}`;
    } else if (op === 3) {
      if (word & 16 || ((word >> 5) & 7) === 7) return raw;
      text = `out ${OUT_NAMES[(word >> 5) & 7]}, ${n}`;
    } else if (op === 4) {
      if (word & 31) return raw;
      const isPull = (word >> 7) & 1, c = (word >> 6) & 1, block = (word >> 5) & 1;
      text = isPull ? "pull" : "push";
      if (c) text += isPull ? " ifempty" : " iffull";
      text += block ? " block" : " noblock";
    } else if (op === 5) {
      const mop = (word >> 3) & 3;
      if (mop === 3) {
        const dst = (word >> 5) & 7;
        if (dst === 0) text = `mov pins, ${SRC_NAMES[word & 7]} late`;
        else if (dst === 1 || dst === 2) {
          const r = MOV_NAMES[dst];
          text = `mov ${r}, ${r} + ${SRC_NAMES[word & 7]}`;
        } else return raw;
      } else {
        text = `mov ${MOV_NAMES[(word >> 5) & 7]}, ${["", "~", "::"][mop]}${SRC_NAMES[word & 7]}`;
      }
    } else if (op === 7) {
      const dst = (word >> 5) & 7, imm = word & 31;
      if (dst === 3) {
        if (imm & 8) return raw;
        text = `${imm & 16 ? "clr" : "set"} flag, ${imm & 7}`;
      } else if (has(SET_NAMES, dst)) text = `set ${SET_NAMES[dst]}, ${imm}`;
      else if (dst === SET_SKIP || dst === SET_WAKE) text = `${dst === SET_SKIP ? "skip" : "wake"} ${condName(imm)}`;
      else return raw;
    } else return raw;
    if (side !== null) text += ` side ${side}`;
    if (delay) text += ` [${delay}]`;
    return text;
  }

  const api = {
    M16, FIFO_DEPTH, RAM_WORDS, IMEM_WORDS, CHIP_ID, VERSION,
    A_CTRL, A_FLAGS, A_PIN_IN, A_PIN_OUT, A_PIN_OE, A_ID, A_PIN_SET, A_PIN_CLR, A_RAM_ADDR,
    A_RAM_DATA, A_DMA0, A_DMA1, A_FLAG_SET, A_FLAG_CLR, A_VERSION, A_PIN_LATE, A_IMEM_SEL,
    R_CLKDIV, R_PROG, R_PINMAP, R_PINCFG, R_SHIFT, R_TIMEOUT, R_TRAP, R_CRCPOLY, R_CRC, R_X,
    R_Y, R_ISR, R_OSR, R_STATUS, R_TIME, R_FIFO,
    engineBase, isPort, dmaWord, Core, Engine, AsmError, assemble, disassemble, progReg, condName,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PEMU = api;
})(typeof window !== "undefined" ? window : globalThis);
