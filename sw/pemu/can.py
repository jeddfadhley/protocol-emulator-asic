"""CAN 2.0A frames for the `can_tx` firmware: build the stuffed bit stream
the host hands to the chip, and decode bus bits back into frames.

Bits are levels: 0 = dominant, 1 = recessive.
"""

CRC15_POLY = 0x4599


class CanError(Exception):
    pass


def crc15(bits):
    crc = 0
    for b in bits:
        fb = b ^ ((crc >> 14) & 1)
        crc = (crc << 1) & 0x7FFF
        if fb:
            crc ^= CRC15_POLY
    return crc


def _bits(value, n):
    return [(value >> (n - 1 - i)) & 1 for i in range(n)]


def frame_fields(ident, data=b"", rtr=False):
    """Unstuffed bits from SOF to the end of the CRC (standard 11-bit ID)."""
    if not 0 <= ident < 2048 or len(data) > 8:
        raise ValueError("bad frame")
    bits = [0] + _bits(ident, 11) + [int(rtr), 0, 0] + _bits(len(data), 4)
    for byte in ([] if rtr else data):
        bits += _bits(byte, 8)
    return bits + _bits(crc15(bits), 15)


def stuff(bits):
    out, run, last = [], 0, None
    for b in bits:
        out.append(b)
        run = run + 1 if b == last else 1
        last = b
        if run == 5:
            out.append(1 - b)
            last, run = 1 - b, 1
    return out


def frame_bits(ident, data=b"", rtr=False):
    """The whole frame as sent: stuffed SOF..CRC, CRC delimiter, ACK slot
    (sent recessive), ACK delimiter and 7 EOF bits. Returns (levels,
    monitor flags): every recessive bit is monitored except the ACK slot."""
    body = stuff(frame_fields(ident, data, rtr))
    levels = body + [1, 1, 1] + [1] * 7
    ack_slot = len(body) + 1
    monitor = [int(lv == 1 and i != ack_slot) for i, lv in enumerate(levels)]
    return levels, monitor


def tx_words(ident, data=b"", rtr=False):
    """can_tx words: the bit count - 1, then [level, monitor] pairs LSB first."""
    levels, monitor = frame_bits(ident, data, rtr)
    words = [len(levels) - 1]
    for i in range(0, len(levels), 8):
        w = 0
        for k, (lv, mon) in enumerate(zip(levels[i:i + 8], monitor[i:i + 8])):
            w |= (lv | mon << 1) << (2 * k)
        words.append(w)
    return words


def decode(bits):
    """Decode bus levels starting at SOF. Returns (ident, data, rtr, ack,
    stuffed_length) or raises CanError. ack is True if the ACK slot was
    dominant."""
    if not bits or bits[0] != 0:
        raise CanError("no SOF")
    raw, run, last, i = [], 0, None, 0
    need = None
    while need is None or len(raw) < need:
        if i >= len(bits):
            raise CanError("frame cut short")
        b = bits[i]
        i += 1
        if run == 5:
            if b == last:
                raise CanError(f"stuff error at bit {i - 1}")
            run, last = 1, b
            continue
        run = run + 1 if b == last else 1
        last = b
        raw.append(b)
        if len(raw) == 19:
            dlc = min(int("".join(map(str, raw[15:19])), 2), 8)
            rtr = raw[12]
            need = 19 + (0 if rtr else 8 * dlc) + 15
    if run == 5 and i < len(bits):          # a stuff bit can follow the CRC
        if bits[i] == last:
            raise CanError("stuff error after the CRC")
        i += 1
    if crc15(raw[:-15]) != int("".join(map(str, raw[-15:])), 2):
        raise CanError("CRC error")
    ident = int("".join(map(str, raw[1:12])), 2)
    data = bytes(int("".join(map(str, raw[19 + 8 * k:27 + 8 * k])), 2)
                 for k in range(0 if raw[12] else dlc))
    tail = bits[i:i + 10]
    if len(tail) < 10:
        raise CanError("no ACK field / EOF")
    if tail[0] != 1 or tail[2] != 1 or any(b != 1 for b in tail[3:10]):
        raise CanError(f"form error in {tail}")
    return ident, data, bool(raw[12]), tail[1] == 0, i
