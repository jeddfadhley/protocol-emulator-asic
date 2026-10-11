"""USB low-speed line coding (host side) and decoding (test side).

PEMU transmits USB packets as a precisely timed stream of line states; the
host prepares that stream: SYNC, PID, payload, CRC, bit stuffing, NRZI and
EOP. `decode_states` is an independent decoder used by the tests.
"""

J, K, SE0, END = 2, 1, 0, 3  # line state = (D- << 1) | D+ for low speed

PID_OUT, PID_IN, PID_SETUP = 0x1, 0x9, 0xD
PID_DATA0, PID_DATA1 = 0x3, 0xB
PID_ACK, PID_NAK, PID_STALL = 0x2, 0xA, 0xE


def crc5(value, nbits=11):
    crc = 0x1F
    for i in range(nbits):
        if (crc ^ (value >> i)) & 1:
            crc = (crc >> 1) ^ 0x14
        else:
            crc >>= 1
    return crc ^ 0x1F


def crc16(data):
    crc = 0xFFFF
    for byte in data:
        for i in range(8):
            if (crc ^ (byte >> i)) & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc ^ 0xFFFF


def pid_byte(pid):
    return (pid & 15) | ((~pid & 15) << 4)


def token(pid, addr, endp):
    v = (addr & 0x7F) | (endp & 15) << 7
    v |= crc5(v) << 11
    return bytes([pid_byte(pid), v & 0xFF, v >> 8])


def data_packet(pid, payload):
    c = crc16(payload)
    return bytes([pid_byte(pid)]) + bytes(payload) + bytes([c & 0xFF, c >> 8])


def handshake(pid):
    return bytes([pid_byte(pid)])


def encode(packet):
    """Packet bytes (after SYNC) -> line states, ending with EOP and END."""
    bits = []
    for byte in b"\x80" + packet:  # SYNC is 0x80 sent LSB first: 0000 0001
        bits += [(byte >> i) & 1 for i in range(8)]
    stuffed, ones = [], 0
    for b in bits:
        stuffed.append(b)
        ones = ones + 1 if b else 0
        if ones == 6:
            stuffed.append(0)
            ones = 0
    states, level = [], J
    for b in stuffed:
        if b == 0:
            level = K if level == J else J
        states.append(level)
    return states + [SE0, SE0, J]


def to_words(states):
    """Pack line states into 16-bit TX words, 8 per word, first in bits [15:14].

    A leading J (idle) is added, and END goes in the very last slot so the
    engine's OSR is empty when it releases the bus; the gap is padded with J,
    which is the idle state anyway.
    """
    states = [J] + list(states)
    states += [J] * (-(len(states) + 1) % 8) + [END]
    words = []
    for i in range(0, len(states), 8):
        w = 0
        for s in states[i:i + 8]:
            w = (w << 2) | s
        words.append(w)
    return words


class UsbError(Exception):
    pass


def decode_states(states):
    """Line states of one packet (SYNC .. EOP) -> packet bytes after SYNC."""
    try:
        eop = states.index(SE0)
    except ValueError:
        raise UsbError("no EOP") from None
    if states[eop:eop + 3] != [SE0, SE0, J]:
        raise UsbError(f"bad EOP {states[eop:eop + 3]}")
    level, bits = J, []
    for s in states[:eop]:
        if s not in (J, K):
            raise UsbError(f"illegal state {s} in packet")
        bits.append(1 if s == level else 0)
        level = s
    out, ones, i = [], 0, 0
    while i < len(bits):
        b = bits[i]
        out.append(b)
        ones = ones + 1 if b else 0
        if ones == 6:
            i += 1
            if i < len(bits) and bits[i] != 0:
                raise UsbError("bit stuffing violation")
            ones = 0
        i += 1
    if out[:8] != [0, 0, 0, 0, 0, 0, 0, 1]:
        raise UsbError(f"bad SYNC {out[:8]}")
    body = out[8:]
    if len(body) % 8:
        raise UsbError(f"{len(body)} bits is not whole bytes")
    data = bytes(sum(body[i + k] << k for k in range(8)) for i in range(0, len(body), 8))
    pid = data[0]
    if (pid & 15) != (~pid >> 4) & 15:
        raise UsbError(f"bad PID check {pid:#04x}")
    return data


def check_crc(packet):
    pid = packet[0] & 15
    if pid in (PID_OUT, PID_IN, PID_SETUP):
        v = packet[1] | packet[2] << 8
        return crc5(v & 0x7FF) == v >> 11
    if pid in (PID_DATA0, PID_DATA1):
        return crc16(packet[1:-2]) == (packet[-2] | packet[-1] << 8)
    return len(packet) == 1


# ----- receive side: the usb_ls_rx capture -----

PID_NAMES = {0x1: "OUT", 0x9: "IN", 0x5: "SOF", 0xD: "SETUP", 0x3: "DATA0", 0xB: "DATA1",
             0x7: "DATA2", 0xF: "MDATA", 0x2: "ACK", 0xA: "NAK", 0xE: "STALL", 0x6: "NYET",
             0xC: "PRE"}


def capture_states(words):
    """usb_ls_rx RAM words -> line states, 8 per word, first in bits [1:0]."""
    return [(w >> (2 * k)) & 3 for w in words for k in range(8)]


def split_capture(states):
    """Line states sampled once per bit -> [(packet bytes or None, error)].

    A packet starts at the first K after idle (J) and ends with SE0 SE0 J."""
    return split_stream(states)[0]


def split_stream(states):
    """Like split_capture, for a growing capture: returns (packets, used),
    where states[used:] may still hold an unfinished packet."""
    out, i, n = [], 0, len(states)
    used = 0
    while i < n:
        while i < n and states[i] != K:
            i += 1
        if i >= n:
            break
        j = i
        while j + 1 < n and not (states[j] == SE0 and states[j + 1] == SE0):
            j += 1
        if j + 1 >= n:
            break  # incomplete packet at the end of the capture
        seg = states[i:j + 2] + [J]
        used = j + 2
        try:
            pkt = decode_states(seg)
            out.append((pkt, None if check_crc(pkt) else "CRC error"))
        except UsbError as e:
            out.append((None, str(e)))
        i = j + 2
    if i >= n:
        used = n
    return out, used


def describe(packet):
    """A short human-readable description of a packet."""
    pid = packet[0] & 15
    name = PID_NAMES.get(pid, f"PID{pid:x}")
    if pid in (0x1, 0x9, 0xD):
        v = packet[1] | packet[2] << 8
        return f"{name} addr {v & 0x7F} endp {(v >> 7) & 15}"
    if pid == 0x5:
        return f"SOF frame {(packet[1] | packet[2] << 8) & 0x7FF}"
    if pid in (0x3, 0xB, 0x7, 0xF):
        return f"{name} {packet[1:-2].hex(' ')}"
    return name


HID_KEYS = {**{4 + i: chr(ord("a") + i) for i in range(26)},
            **{30 + i: "1234567890"[i] for i in range(10)},
            40: "\n", 41: "<esc>", 42: "<bksp>", 43: "\t", 44: " ", 45: "-", 46: "=",
            47: "[", 48: "]", 49: "\\", 51: ";", 52: "'", 53: "`", 54: ",", 55: ".", 56: "/"}


def hid_keys(report, previous=b""):
    """Keys newly pressed in an 8-byte boot keyboard report (shift applied)."""
    if len(report) != 8:
        return ""
    shift = report[0] & 0x22
    out = ""
    for code in report[2:]:
        if code and code not in previous[2:]:
            ch = HID_KEYS.get(code, f"<{code:#x}>")
            out += ch.upper() if shift and len(ch) == 1 else ch
    return out
