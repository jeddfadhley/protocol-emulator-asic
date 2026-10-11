"""Assembler and disassembler tests."""

import random

import pytest

from pemu.asm import AsmError, assemble, disassemble


def one(src, side_set=""):
    return assemble((side_set + "\n" if side_set else "") + src).code[0]


@pytest.mark.parametrize("src, word", [
    ("jmp 5", 0x0005),
    ("jmp x--, 3", 0x0043),
    ("jmp !osre, 31", 0x00FF),
    ("wait 1 pin 2", 0x20A2),
    ("wait 0 gpio 15 timeout", 0x201F),
    ("wait 1 flag 7", 0x20C7),
    ("wait 1 edge 0 [3]", 0x23E0),
    ("in pins, 1", 0x4001),
    ("in null, 16", 0x4060),
    ("out pindirs, 8", 0x6088),
    ("out pc, 5", 0x60A5),
    ("push", 0x8020),
    ("push iffull noblock", 0x8040),
    ("pull ifempty", 0x80E0),
    ("pull noblock", 0x8080),
    ("mov x, ~y", 0xA02A),
    ("mov osr, ::crc", 0xA0F4),
    ("mov pins, time", 0xA005),
    ("nop", 0xA042),
    ("set pins, 31 [31]", 0xFF1F),
    ("set flag, 3", 0xE063),
    ("clr flag, 3", 0xE073),
    (".word 0xC123", 0xC123),
])
def test_encodings(src, word):
    assert one(src) == word


def test_side_set_and_delay_share_bits():
    assert one("nop side 1 [15]", ".side_set 1") == 0xA042 | (0x1F << 8)
    assert one("nop side 3 [7]", ".side_set 2") == 0xA042 | (0x1F << 8)
    with pytest.raises(AsmError):
        one("nop side 1 [16]", ".side_set 1")
    with pytest.raises(AsmError):
        one("nop", ".side_set 1")  # side value is mandatory


def test_labels_wrap_trap_defines():
    p = assemble("""
        .program t
        .define N 7
        .trap oops
        start:  set x, N
        .wrap_target
        loop:   jmp x--, loop
        .wrap
        oops:   jmp start
    """)
    assert p.name == "t"
    assert p.code == [0xE027, 0x0041, 0x0000]
    assert (p.wrap_bottom, p.wrap_top, p.trap) == (1, 1, 2)
    assert p.prog_reg(10) == 10 | 1 << 6 | 1 << 11


@pytest.mark.parametrize("bad", [
    "jmp 32", "in pins, 17", "in pins, 0", "set x, 32", "wait 2 pin 0",
    "wait 1 flag 8", "frob x", "jmp maybe, 1", "set flag, 9",
])
def test_errors(bad):
    with pytest.raises(AsmError):
        assemble(bad)


def test_too_long():
    with pytest.raises(AsmError):
        assemble("nop\n" * 33)


@pytest.mark.parametrize("side_count", [0, 1, 2, 3])
def test_disassemble_round_trip(side_count):
    rng = random.Random(side_count)
    hdr = f".side_set {side_count}\n" if side_count else ""
    for _ in range(3000):
        w = rng.randrange(1 << 16)
        text = disassemble(w, side_count)
        assert assemble(hdr + text).code == [w], text
