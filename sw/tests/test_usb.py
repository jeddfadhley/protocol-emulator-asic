"""USB low-speed line coding: known packets, and the decoder rejects bad ones."""

import pytest

from pemu import usb
from pemu.periph import UsbLsMonitor


def test_known_packets():
    # Byte sequences as captured on real buses
    assert usb.token(usb.PID_SETUP, 0, 0).hex() == "2d0010"
    req = bytes([0x80, 0x06, 0x00, 0x01, 0x00, 0x00, 0x40, 0x00])
    assert usb.data_packet(usb.PID_DATA0, req).hex() == "c38006000100004000dd94"
    assert usb.handshake(usb.PID_ACK).hex() == "d2"


@pytest.mark.parametrize("payload", [b"", b"\x00", b"\xff" * 8, bytes(range(8))])
def test_round_trip(payload):
    p = usb.data_packet(usb.PID_DATA1, payload)
    assert usb.decode_states(usb.encode(p)) == p
    assert usb.check_crc(p)


def test_stuffing_is_inserted():
    states = usb.encode(usb.data_packet(usb.PID_DATA0, b"\xff\xff"))
    # Six 1s (no transitions) may never be followed by a seventh
    run = 1
    for a, b in zip(states, states[1:]):
        run = run + 1 if a == b else 1
        assert run <= 7


def test_decoder_rejects_errors():
    good = usb.encode(usb.handshake(usb.PID_ACK))
    bad_sync = list(good)
    bad_sync[2] = usb.J if bad_sync[2] == usb.K else usb.K
    with pytest.raises(usb.UsbError):
        usb.decode_states(bad_sync)
    with pytest.raises(usb.UsbError):
        usb.decode_states(good[:-3])  # no EOP
    p = bytearray(usb.data_packet(usb.PID_DATA0, b"abc"))
    p[2] ^= 1
    assert not usb.check_crc(bytes(p))


def test_monitor_flags_timing_errors():
    mon = UsbLsMonitor(0, 1, 4)
    states = usb.encode(usb.handshake(usb.PID_ACK))
    wave = []
    for i, s in enumerate(states):
        wave += [s] * (5 if i == 3 else 4)  # one bit 25% too long
    for s in wave + [3] * 8:
        mon.step(s)
    assert mon.errors and "lasted 5 clocks" in mon.errors[0]


# The receiver (usb_ls_rx firmware on the model, which the co-simulation ties
# to the RTL clock for clock) against a device whose clock is off by up to
# +-4%, at 6 phases across one bit. USB low speed only requires +-1.5%.
USB_BIT_NS = 1e9 / 1.5e6
PHASES = [i * USB_BIT_NS / 6 for i in range(6)]


def _usb_rx_ok(ppm, phase_ns):
    import asyncio

    from pemu import scenarios
    from pemu.bench import ModelBench

    try:
        asyncio.run(scenarios._usb_rx(ModelBench(), ppm=ppm, phase_ns=phase_ns))
        return True
    except AssertionError:
        return False


@pytest.mark.parametrize("ppm", [-40000, -20000, -15000, 0, 15000, 20000, 40000])
def test_usb_rx_tolerance(ppm):
    assert all(_usb_rx_ok(ppm, p) for p in PHASES)


def test_usb_rx_tolerance_can_fail():
    """The sweep above means something: at 8% the receiver loses packets."""
    assert not _usb_rx_ok(80000, PHASES[0])
    assert not _usb_rx_ok(-80000, PHASES[0])
