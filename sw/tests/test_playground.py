"""The browser playground's JavaScript model and assembler
(docs/playground/pemu.js) against the Python originals: random programs
with random host traffic and inputs, compared on every clock, plus the
assembler on every firmware file and on fuzzed source. Needs node."""

import glob
import json
import os
import random
import shutil
import subprocess

import pytest

from pemu import hwcheck
from pemu.asm import AsmError, assemble, disassemble
from pemu.host import ctrl
from pemu.model import A_CTRL, A_DMA0, A_RAM_ADDR, A_RAM_DATA, Core, engine_base

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
CHECK = os.path.join(ROOT, "docs", "playground", "check.js")
NODE = shutil.which("node")


def digest(c, rdata):
    e = [[g.pc, g.delay, g.x, g.y, g.isr, g.isr_cnt, g.osr, g.osr_cnt, g.crc, g.time, g.divcnt,
          g.stalled, g.timed_out, g.rx_ovf, g.tx_ovf, g.armed, g.edge_lvl, g.to_cnt, g.wake,
          g.annul, g.pop_armed, ",".join(map(str, g.txf)), ",".join(map(str, g.rxf))]
         for g in c.engines]
    ram = 0
    for i, w in enumerate(c.ram):
        ram = (ram * 31 + w + i) % 1000000007
    return json.dumps([rdata, c.pin_out, c.pin_oe, c.pin_late, c.flags, c.ctrl, c.dma[0],
                       c.dma[1], c.ram_addr, c.sync1, c.sync2, c.pulses, ram, e],
                      separators=(",", ":"))


def trace(seed, clocks):
    """Random programs (the hardware lockstep's generator), then the engines
    run freely while the host pokes registers, FIFOs, the RAM and DMA."""
    rng = random.Random(seed)
    if seed % 3 == 0:
        pair = [hwcheck.directed_program(rng, rng.choice(hwcheck.V4_PROGRAMS)) for _ in range(2)]
    else:
        pair = [hwcheck.random_program(rng) for _ in range(2)]
    ops = [(0, 0, 1, 0, 0, a, v) for a, v in hwcheck.setup_writes(rng, pair)[:-1]]
    for e in range(2):
        ops.append((0, 0, 1, 0, 0, engine_base(e), rng.choice([0, 0, 1, 2, 5])))
    ops.append((0, 0, 1, 0, 0, A_CTRL, ctrl(enable=3, loopback=rng.random() < 0.5, restart=3)))
    ui, uio = rng.randrange(256), rng.randrange(256)
    while len(ops) < clocks:
        r = rng.random()
        if r < 0.1:
            ui, uio = rng.randrange(256), rng.randrange(256)
        r = rng.random()
        if r < 0.03:
            addr = rng.choice([rng.randrange(0x40, 0x71), engine_base(rng.randrange(2)) + 15,
                               A_RAM_DATA, A_RAM_ADDR, A_DMA0, A_DMA0 + 1, A_CTRL])
            value = rng.randrange(1 << 16)
            if addr == A_CTRL:
                value = ctrl(enable=3, loopback=value & 4, restart=value >> 4 & 3,
                             step=value >> 6 & 3, flush=value >> 8 & 3)
            ops.append((ui, uio, 1, 0, 0, addr, value))
        elif r < 0.06:
            addr = rng.choice([rng.randrange(0x40, 0x71), engine_base(rng.randrange(2)) + 15,
                               A_RAM_DATA])
            ops.append((ui, uio, 0, 1, 0, addr, 0))
            ops.append((ui, uio, 0, 0, rng.random() < 0.9, addr, 0))
        else:
            ops.append((ui, uio, 0, 0, 0, 0, 0))
    c = Core()
    digests = []
    for ui_, uio_, we, re, rc, addr, wdata in ops:
        r = c.clock(ui_, uio_, we=bool(we), re=bool(re), rc=bool(rc), addr=addr, wdata=wdata)
        digests.append(digest(c, r))
    return {"ops": [list(map(int, o)) for o in ops], "digests": digests}


def asm_result(source):
    try:
        p = assemble(source)
    except AsmError:
        return {"error": True}
    return {"code": p.code, "labels": p.labels, "wrap": [p.wrap_bottom, p.wrap_top],
            "side": [p.side_count, p.side_pindir], "trap": p.trap, "name": p.name}


TOKENS = ["x", "y", "pins", "null", "isr", "osr", "crc", "time", "pc", "pindirs", "flag", "1",
          "0", "16", "17", "31", "32", "0x10", "0b11", "~x", "::y", "x + y", "y + time",
          "pins late", "!x", "x--", "y--", "!osre", "loop", "side 1", "[3]", "[40]", "[20]", "[7]",
          "[12] side 1", "timeout",
          "any", "edge", "gpio", "pin", "block", "noblock", "iffull", "ifempty", "x<y", "!flag 2",
          "x>=y", "x!=y", "rxfull", "txdata", "xneg", "BITS", ","]
MNEMS = ["jmp", "wait", "in", "out", "push", "pull", "mov", "set", "clr", "skip", "wake", "nop",
         ".word", "bogus"]


def fuzz_source(rng):
    sc = rng.choice([0, 0, 1, 2, 3])
    lines = [".program fuzz", ".define BITS 7"]
    if sc:
        lines.append(f".side_set {sc}" + (" pindirs" if rng.random() < 0.3 else ""))
    for i in range(rng.randrange(1, 6)):
        r = rng.random()
        if r < 0.35:
            w = rng.randrange(1 << 16)
            text = disassemble(w, sc)
        elif r < 0.6:
            # a valid instruction with a random side-set value and delay
            # (some out of range, in both orders)
            text = disassemble(rng.randrange(1 << 16), 0).split(" [")[0]
            side = f" side {rng.randrange(8)}" if rng.random() < 0.8 else ""
            delay = f" [{rng.randrange(34)}]" if rng.random() < 0.8 else ""
            text += side + delay if rng.random() < 0.7 else delay + side
        else:
            text = rng.choice(MNEMS) + " " + " ".join(rng.choice(TOKENS)
                                                     for _ in range(rng.randrange(4)))
            if sc and rng.random() < 0.7:
                text += f" side {rng.randrange(1 << sc)}"
        if rng.random() < 0.15:
            text = "loop: " + text if i == 0 else f"l{i}: " + text
        if rng.random() < 0.1:
            text += " ; comment"
        lines.append(text)
        if rng.random() < 0.05:
            lines.append(rng.choice([".wrap_target", ".wrap", ".trap loop", ".bogus"]))
    return "\n".join(lines) + "\n"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_javascript_matches_python(tmp_path):
    traces = [trace(seed, 3000) for seed in range(60)]
    asm = []
    for path in sorted(glob.glob(os.path.join(ROOT, "sw", "programs", "*.pasm"))):
        src = open(path).read()
        asm.append({"source": src, "result": asm_result(src)})
    rng = random.Random(5)
    for _ in range(3000):
        src = fuzz_source(rng)
        asm.append({"source": src, "result": asm_result(src)})
    disasm = []
    for _ in range(20000):
        w, sc = rng.randrange(1 << 16), rng.randrange(4)
        disasm.append([w, sc, disassemble(w, sc)])
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps({"traces": traces, "asm": asm, "disasm": disasm}))
    r = subprocess.run([NODE, CHECK, str(cases)], capture_output=True, text=True, timeout=600)
    assert r.returncode == 0 and r.stdout.startswith("OK"), r.stdout + r.stderr
    errors = sum(1 for a in asm if "error" in a["result"])
    assert 300 < errors < len(asm) - 300, "the fuzzer should give both good and bad programs"


def test_generated_files_are_up_to_date():
    """docs/playground/data.js and index.html match build.py's output (rerun
    python3 docs/playground/build.py after changing firmware or the page)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "build", os.path.join(ROOT, "docs", "playground", "build.py"))
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    data, _, html = build.build()
    pg = os.path.join(ROOT, "docs", "playground")
    assert open(os.path.join(pg, "data.js")).read() == data
    assert open(os.path.join(pg, "index.html")).read() == html


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_examples_decode_what_they_send():
    """Every playground example, run in node exactly as the page runs it."""
    from pemu import music, vga
    r = subprocess.run([NODE, os.path.join(ROOT, "docs", "playground", "check_examples.js")],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["uart"]["text"] == "Hi from PEMU!"
    assert "".join(out["uart"]["marks"]) == "Hi from PEMU!"
    assert out["leds"]["leds"] == [[0xFF, 0, 0], [0xFF, 0xA0, 0], [0x30, 0xD0, 0x30],
                                   [0, 0x60, 0xFF], [0xB0, 0x40, 0xFF]]
    img = vga.text_picture(["PEMU", "HELLO VGA"])
    assert out["vga"]["vga"]["rows"] == "".join(str(b) for row in img for b in row)
    assert out["vga"]["vga"]["linesOk"]
    names = [n for n, _ in music.ODE_TO_JOY]
    assert [n[0] for n in out["music"]["notes"]] == names
    for (name, hz, length), (_, steps) in zip(out["music"]["notes"], music.ODE_TO_JOY):
        assert abs(hz - music.played_frequency(music.half_period_x(music.note_number(name)))) < 0.2
        # each step is 0.25 s at 120 bpm; the simulation clock stands for 1 us
        assert abs(length / 1e6 - 0.25 * steps) < 0.03, (name, length, steps)
    assert out["music"]["audio"]["rms"] > 0.1
