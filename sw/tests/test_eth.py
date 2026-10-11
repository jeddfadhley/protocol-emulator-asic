"""Ethernet helpers and the receiver's tolerance to clock error and jitter."""

import asyncio
import zlib

import pytest

from pemu import eth
from pemu import scenarios as S
from pemu.bench import ModelBench
from pemu.periph import EthSource


def test_crc32_matches_zlib():
    for data in (b"", b"a", b"123456789", bytes(range(256)) * 3):
        assert eth.crc32(data) == zlib.crc32(data)


def test_fcs_and_padding():
    f = eth.with_fcs(b"\x01" * 20)
    assert len(f) == 64 and f[20:60] == bytes(40)
    assert eth.fcs_ok(f)
    assert not eth.fcs_ok(f[:-1] + bytes([f[-1] ^ 1]))


def test_udp_frame_round_trip_and_checksums():
    payload = b"Hello from PEMU"
    f = eth.udp_frame(payload, src_ip=(10, 0, 0, 2), dst_ip=(10, 0, 0, 1), sport=1234, dport=7)
    src_mac, dst_mac, sip, dip, sport, dport, data = eth.parse_udp(f)
    assert (sip, dip, sport, dport, data) == ((10, 0, 0, 2), (10, 0, 0, 1), 1234, 7, payload)
    assert eth._csum(f[14:34]) == 0  # IPv4 header checksum verifies
    seg = f[34:]
    pseudo = bytes(sip) + bytes(dip) + bytes([0, 17, 0, len(seg)])
    assert eth._csum(pseudo + seg) == 0  # UDP checksum verifies


def test_tx_image_layout():
    f = eth.with_fcs(eth.udp_frame(b"x"))
    img = eth.tx_image(f)
    nbits = img[0] + 1
    assert nbits == 8 * (8 + len(f))
    assert img[1] == 0x5555 and len(img) == 1 + (nbits + 1 + 15) // 16


def test_rx_decode_inverts_the_receiver_layout():
    data = bytes(range(37))
    bits = eth.bits_lsb_first(data)
    x = 8 * 50 - 1
    words = eth.pack_bits(bits[:len(bits) // 16 * 16])
    k = len(bits) % 16
    words.append(eth.pack_bits(bits[len(bits) - k:])[0] << (16 - k))  # partial, left-aligned
    words.append((x - len(bits)) & 0xFFFF)
    assert eth.rx_decode(words, x) == (data, len(bits))


async def _receive(phase, ppm, jitter, seed):
    b = ModelBench()
    frame = eth.with_fcs(eth.udp_frame(bytes((seed * 7 + i) & 255 for i in range(40))))
    _, x = eth.rx_words(100)
    src = EthSource([frame], gap_ns=5000, phase_ns=phase, ppm=ppm, jitter_ns=jitter, seed=seed)
    await b.setup(1, S.program("eth_rx"), 16, S.eth_rx_cfg(S.ETH_RD, 100))
    await b.dma(1, 0, 128, rx=True)
    await b.start(2)
    b.ui(src, 0)
    end = await S.eth_wait_frame(b, limit=4000)
    data, _ = eth.rx_decode(await b.ram_read(0, end), x)
    return data == frame


@pytest.mark.parametrize("ppm", [0, 100, -100, 2000, -2000])
def test_rx_tolerance(ppm):
    """Every phase of the sender's clock, its frequency off by up to 2000 ppm
    (the standard allows 100), and every edge moved by up to +-4 ns."""
    results = [asyncio.run(_receive(phase, ppm, 4.0, seed))
               for phase in range(0, 20, 2) for seed in range(3)]
    assert all(results), results.count(False)
