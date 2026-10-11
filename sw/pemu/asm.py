"""PEMU assembler.

Syntax (see docs/programming.md):

    .program uart_tx
    .side_set 1                 ; or ".side_set 1 pindirs"
    .define BITS 7
    .wrap_target
        pull            side 1 [7]
        set x, BITS     side 0 [7]
    bitloop:
        out pins, 1
        jmp x--, bitloop       [6]
    .wrap

Comments start with ';' or '//'. Numbers may be decimal, 0x hex or 0b binary.
"""

import re
import sys
from dataclasses import dataclass, field

JMP_CONDS = {"": 0, "!x": 1, "x--": 2, "!y": 3, "y--": 4, "x!=y": 5, "pin": 6, "!osre": 7}
WAIT_SRCS = {"gpio": 0, "pin": 1, "flag": 2, "edge": 3}
SOURCES = {"pins": 0, "x": 1, "y": 2, "null": 3, "crc": 4, "time": 5, "isr": 6, "osr": 7}
OUT_DSTS = {"pins": 0, "x": 1, "y": 2, "null": 3, "pindirs": 4, "pc": 5, "isr": 6}
MOV_DSTS = {"pins": 0, "x": 1, "y": 2, "crc": 3, "pindirs": 4, "pc": 5, "isr": 6, "osr": 7}
SET_DSTS = {"pins": 0, "x": 1, "y": 2, "pindirs": 4}

OP_JMP, OP_WAIT, OP_IN, OP_OUT, OP_PUSHPULL, OP_MOV, OP_SET = 0, 1, 2, 3, 4, 5, 7


class AsmError(Exception):
    pass


@dataclass
class Program:
    name: str
    code: list
    labels: dict
    wrap_bottom: int
    wrap_top: int
    side_count: int = 0
    side_pindir: bool = False
    trap: int = 0
    defines: dict = field(default_factory=dict)

    def __len__(self):
        return len(self.code)

    def prog_reg(self, origin):
        """Value for the engine PROG register when loaded at `origin`."""
        return (origin & 63) | (self.wrap_bottom << 6) | (self.wrap_top << 11)


def _num(tok, defines, labels=None):
    tok = tok.strip()
    if labels is not None and tok in labels:
        return labels[tok]
    if tok in defines:
        return defines[tok]
    try:
        return int(tok, 0)
    except ValueError:
        raise AsmError(f"bad number or unknown symbol '{tok}'") from None


def _count(tok, defines, what):
    n = _num(tok, defines)
    if not 1 <= n <= 16:
        raise AsmError(f"{what} bit count must be 1-16, got {n}")
    return n & 15


def assemble(source, name=None):
    """Assemble one program from source text. Returns a Program."""
    defines = {}
    labels = {}
    side_count = 0
    side_pindir = False
    wrap_bottom = None
    wrap_top = None
    trap_label = None
    prog_name = name or "program"
    lines = []  # (lineno, text)

    # Pass 1: directives, labels, instruction lines.
    for lineno, raw in enumerate(source.splitlines(), 1):
        text = re.split(r";|//", raw, maxsplit=1)[0].strip()
        if not text:
            continue
        try:
            while True:
                m = re.match(r"^([A-Za-z_]\w*):\s*(.*)$", text)
                if not m:
                    break
                if m.group(1) in labels:
                    raise AsmError(f"duplicate label '{m.group(1)}'")
                labels[m.group(1)] = len(lines)
                text = m.group(2)
            if not text:
                continue
            if text.startswith(".") and not text.lower().startswith(".word"):
                parts = text.split()
                d = parts[0].lower()
                if d == ".program":
                    prog_name = parts[1]
                elif d == ".side_set":
                    side_count = _num(parts[1], defines)
                    if not 0 <= side_count <= 3:
                        raise AsmError("side_set count must be 0-3")
                    side_pindir = len(parts) > 2 and parts[2].lower() == "pindirs"
                elif d == ".define":
                    defines[parts[1]] = _num(parts[2], defines)
                elif d == ".wrap_target":
                    wrap_bottom = len(lines)
                elif d == ".wrap":
                    if not lines:
                        raise AsmError(".wrap before any instruction")
                    wrap_top = len(lines) - 1
                elif d == ".trap":
                    trap_label = parts[1]
                else:
                    raise AsmError(f"unknown directive {d}")
                continue
            lines.append((lineno, text))
        except (AsmError, IndexError) as exc:
            raise AsmError(f"line {lineno}: {exc}") from None

    if len(lines) > 32:
        raise AsmError(f"program is {len(lines)} instructions; the limit is 32")

    # Pass 2: encode.
    code = []
    for lineno, text in lines:
        try:
            code.append(_encode(text, defines, labels, side_count))
        except (AsmError, IndexError, KeyError) as exc:
            raise AsmError(f"line {lineno}: {exc}: '{text}'") from None

    trap = 0
    if trap_label is not None:
        trap = _num(trap_label, defines, labels)

    return Program(
        name=prog_name,
        code=code,
        labels=labels,
        wrap_bottom=0 if wrap_bottom is None else wrap_bottom,
        wrap_top=len(code) - 1 if wrap_top is None else wrap_top,
        side_count=side_count,
        side_pindir=side_pindir,
        trap=trap,
        defines=defines,
    )


def _encode(text, defines, labels, side_count):
    if text.lower().startswith(".word"):  # raw instruction word
        w = _num(text[5:], defines)
        if not 0 <= w < 1 << 16:
            raise AsmError(".word value out of range")
        return w
    # Strip the optional "[delay]" and "side v" suffixes.
    delay = 0
    m = re.search(r"\[([^\]]+)\]\s*$", text)
    if m:
        delay = _num(m.group(1), defines)
        text = text[: m.start()].strip()
    side = None
    m = re.search(r"\bside\s+(\S+)\s*$", text)
    if m:
        side = _num(m.group(1), defines)
        text = text[: m.start()].strip()
    m = re.search(r"\[([^\]]+)\]\s*$", text)  # also allow "[d] side v"
    if m:
        delay = _num(m.group(1), defines)
        text = text[: m.start()].strip()

    max_delay = (1 << (5 - side_count)) - 1
    if not 0 <= delay <= max_delay:
        raise AsmError(f"delay {delay} out of range 0-{max_delay}")
    if side_count:
        if side is None:
            raise AsmError("side-set value required (.side_set is in use)")
        if not 0 <= side < (1 << side_count):
            raise AsmError(f"side value {side} does not fit in {side_count} bits")
        sd = (side << (5 - side_count)) | delay
    else:
        if side is not None:
            raise AsmError("'side' used without .side_set")
        sd = delay

    mnem, _, rest = text.partition(" ")
    mnem = mnem.lower()
    args = [a.strip() for a in rest.split(",")] if rest.strip() else []
    op, operand = _operands(mnem, args, rest.strip(), defines, labels)
    return (op << 13) | (sd << 8) | operand


def _operands(mnem, args, rest, defines, labels):
    if mnem == "nop":
        return OP_MOV, (MOV_DSTS["y"] << 5) | SOURCES["y"]

    if mnem == "jmp":
        if len(args) == 1:
            cond, target = "", args[0]
        else:
            cond, target = args[0].lower().replace(" ", ""), args[1]
        if cond not in JMP_CONDS:
            raise AsmError(f"unknown jmp condition '{cond}'")
        addr = _num(target, defines, labels)
        if not 0 <= addr < 32:
            raise AsmError("jump target out of range")
        return OP_JMP, (JMP_CONDS[cond] << 5) | addr

    if mnem == "wait":
        toks = rest.replace(",", " ").split()
        timeout = toks[-1].lower() == "timeout"
        if timeout:
            toks = toks[:-1]
        if len(toks) != 3:
            raise AsmError("expected 'wait <pol> <gpio|pin|flag|edge> <index> [timeout]'")
        pol = _num(toks[0], defines)
        if pol not in (0, 1):
            raise AsmError("wait polarity must be 0 or 1")
        src = WAIT_SRCS[toks[1].lower()]
        idx = _num(toks[2], defines)
        if not 0 <= idx < (8 if src == 2 else 16):
            raise AsmError("wait index out of range")
        return OP_WAIT, (pol << 7) | (src << 5) | (int(timeout) << 4) | idx

    if mnem == "in":
        src = SOURCES[args[0].lower()]
        return OP_IN, (src << 5) | _count(args[1], defines, "in")

    if mnem == "out":
        dst = OUT_DSTS[args[0].lower()]
        return OP_OUT, (dst << 5) | _count(args[1], defines, "out")

    if mnem in ("push", "pull"):
        toks = [t.lower() for t in rest.split()]
        cond_word = "iffull" if mnem == "push" else "ifempty"
        block = 1
        cond = 0
        for t in toks:
            if t == cond_word:
                cond = 1
            elif t == "block":
                block = 1
            elif t == "noblock":
                block = 0
            else:
                raise AsmError(f"unexpected '{t}'")
        return OP_PUSHPULL, ((mnem == "pull") << 7) | (cond << 6) | (block << 5)

    if mnem == "mov":
        dst = MOV_DSTS[args[0].lower()]
        src = args[1].replace(" ", "").lower()
        mop = 0
        if src.startswith("::"):
            mop, src = 2, src[2:]
        elif src[:1] in ("~", "!"):
            mop, src = 1, src[1:]
        return OP_MOV, (dst << 5) | (mop << 3) | SOURCES[src]

    if mnem in ("set", "clr"):
        if args[0].lower() == "flag":
            idx = _num(args[1], defines)
            if not 0 <= idx < 8:
                raise AsmError("flag index must be 0-7")
            return OP_SET, (3 << 5) | ((mnem == "clr") << 4) | idx
        if mnem == "clr":
            raise AsmError("clr only applies to flags")
        imm = _num(args[1], defines)
        if not 0 <= imm < 32:
            raise AsmError("set value must be 0-31")
        return OP_SET, (SET_DSTS[args[0].lower()] << 5) | imm

    raise AsmError(f"unknown instruction '{mnem}'")


_JMP_NAMES = {v: k for k, v in JMP_CONDS.items()}
_WAIT_NAMES = {v: k for k, v in WAIT_SRCS.items()}
_SRC_NAMES = {v: k for k, v in SOURCES.items()}
_OUT_NAMES = {v: k for k, v in OUT_DSTS.items()}
_MOV_NAMES = {v: k for k, v in MOV_DSTS.items()}
_SET_NAMES = {v: k for k, v in SET_DSTS.items()}


def disassemble(word, side_count=0):
    """One instruction word back to source text (canonical form).

    Encodings with no canonical spelling (reserved opcode or fields) come
    back as ".word 0x....", so disassemble -> assemble always round-trips.
    """
    word &= 0xFFFF
    op, sd = word >> 13, (word >> 8) & 31
    side = sd >> (5 - side_count) if side_count else None
    delay = sd & ((1 << (5 - side_count)) - 1)
    raw = f".word {word:#06x}"
    n = (word & 15) or 16
    if op == OP_JMP:
        cond = _JMP_NAMES[(word >> 5) & 7]
        text = f"jmp {cond + ', ' if cond else ''}{word & 31}"
    elif op == OP_WAIT:
        text = f"wait {(word >> 7) & 1} {_WAIT_NAMES[(word >> 5) & 3]} {word & 15}"
        if (word >> 5) & 3 == 2 and word & 8:
            return raw  # flag index uses 3 bits
        if word & 16:
            text += " timeout"
    elif op == OP_IN:
        if word & 16:
            return raw
        text = f"in {_SRC_NAMES[(word >> 5) & 7]}, {n}"
    elif op == OP_OUT:
        if word & 16 or (word >> 5) & 7 == 7:
            return raw
        text = f"out {_OUT_NAMES[(word >> 5) & 7]}, {n}"
    elif op == OP_PUSHPULL:
        if word & 31:
            return raw
        is_pull, cond, block = (word >> 7) & 1, (word >> 6) & 1, (word >> 5) & 1
        text = "pull" if is_pull else "push"
        if cond:
            text += " ifempty" if is_pull else " iffull"
        text += " block" if block else " noblock"
    elif op == OP_MOV:
        mop = (word >> 3) & 3
        if mop == 3:
            return raw
        prefix = ["", "~", "::"][mop]
        text = f"mov {_MOV_NAMES[(word >> 5) & 7]}, {prefix}{_SRC_NAMES[word & 7]}"
    elif op == OP_SET:
        dst, imm = (word >> 5) & 7, word & 31
        if dst == 3:
            if imm & 8:
                return raw
            text = f"{'clr' if imm & 16 else 'set'} flag, {imm & 7}"
        elif dst in _SET_NAMES:
            text = f"set {_SET_NAMES[dst]}, {imm}"
        else:
            return raw
    else:
        return raw
    if side is not None:
        text += f" side {side}"
    if delay:
        text += f" [{delay}]"
    return text


def assemble_file(path):
    with open(path) as f:
        return assemble(f.read())


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(description="PEMU assembler")
    ap.add_argument("source")
    ap.add_argument("-o", "--output", help="write hex words, one per line")
    args = ap.parse_args(argv)
    try:
        prog = assemble_file(args.source)
    except AsmError as exc:
        print(f"{args.source}: {exc}", file=sys.stderr)
        return 1
    text = "".join(f"{w:04x}\n" for w in prog.code)
    if args.output:
        with open(args.output, "w") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    print(
        f"; {prog.name}: {len(prog)} words, wrap {prog.wrap_bottom}..{prog.wrap_top}, "
        f"side_set {prog.side_count}{' pindirs' if prog.side_pindir else ''}, trap {prog.trap}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
