"""10BASE-T Ethernet helpers for the eth_tx / eth_rx firmware.

The engines only move bits: frame building, checksums and the FCS are done
here, on the host. Everything is plain Python so it also runs on the
MicroPython board driver (no zlib or struct needed).

Packet RAM layouts:

* transmit image: word 0 = number of line bits - 1, then the bits of
  preamble + SFD + frame + FCS, LSB first, 16 per word. One extra bit is read
  after the last one (the firmware fetches ahead), so the image always has
  room for it.
* receive buffer: the bits after the SFD, LSB first, 16 per word, then the
  partial last word, then the number of unused bits (see eth_rx.pasm).
"""

PREAMBLE = bytes([0x55] * 7 + [0xD5])
BROADCAST = b"\xff" * 6
ETH_IPV4 = 0x0800
MIN_FRAME = 60          # without FCS
BIT_CLOCKS = 5          # clocks per bit at 50 MHz

_CRC_TABLE = []
for _n in range(256):
    _c = _n
    for _ in range(8):
        _c = (_c >> 1) ^ 0xEDB88320 if _c & 1 else _c >> 1
    _CRC_TABLE.append(_c)


def crc32(data):
    """IEEE 802.3 CRC-32 (same as zlib.crc32)."""
    c = 0xFFFFFFFF
    for b in data:
        c = _CRC_TABLE[(c ^ b) & 0xFF] ^ (c >> 8)
    return c ^ 0xFFFFFFFF


def with_fcs(frame):
    """Pad to the 60-byte minimum and append the FCS (CRC-32, LSB first)."""
    frame = bytes(frame)
    if len(frame) < MIN_FRAME:
        frame += bytes(MIN_FRAME - len(frame))
    c = crc32(frame)
    return frame + bytes((c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF, c >> 24))


def fcs_ok(frame_with_fcs):
    f = bytes(frame_with_fcs)
    if len(f) <= 4:
        return False
    c = crc32(f[:-4])
    return f[-4:] == bytes((c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF, c >> 24))


def _csum(data):
    if len(data) & 1:
        data += b"\x00"
    s = 0
    for i in range(0, len(data), 2):
        s += data[i] << 8 | data[i + 1]
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def ipv4(src, dst, proto, payload, ident=0, ttl=64):
    """An IPv4 header (no options) followed by the payload."""
    total = 20 + len(payload)
    hdr = bytearray([0x45, 0, total >> 8, total & 0xFF, ident >> 8, ident & 0xFF,
                     0x40, 0, ttl, proto, 0, 0]) + bytes(src) + bytes(dst)
    c = _csum(bytes(hdr))
    hdr[10], hdr[11] = c >> 8, c & 0xFF
    return bytes(hdr) + bytes(payload)


def udp(src_ip, dst_ip, sport, dport, payload):
    """A UDP datagram with its checksum (over the IPv4 pseudo-header)."""
    length = 8 + len(payload)
    hdr = bytes([sport >> 8, sport & 0xFF, dport >> 8, dport & 0xFF,
                 length >> 8, length & 0xFF, 0, 0])
    pseudo = bytes(src_ip) + bytes(dst_ip) + bytes([0, 17, length >> 8, length & 0xFF])
    c = _csum(pseudo + hdr + bytes(payload)) or 0xFFFF
    return hdr[:6] + bytes([c >> 8, c & 0xFF]) + bytes(payload)


def ethernet(dst, src, ethertype, payload):
    return bytes(dst) + bytes(src) + bytes([ethertype >> 8, ethertype & 0xFF]) + bytes(payload)


def udp_frame(payload, src_mac=b"\x02PEMU\x01", dst_mac=BROADCAST,
              src_ip=(192, 168, 7, 2), dst_ip=(255, 255, 255, 255),
              sport=7777, dport=7777):
    """A complete Ethernet + IPv4 + UDP frame (without FCS)."""
    seg = udp(bytes(src_ip), bytes(dst_ip), sport, dport, payload)
    return ethernet(dst_mac, src_mac, ETH_IPV4, ipv4(bytes(src_ip), bytes(dst_ip), 17, seg))


def parse_udp(frame):
    """(src_mac, dst_mac, src_ip, dst_ip, sport, dport, payload) or None."""
    f = bytes(frame)
    if len(f) < 42 or f[12:14] != b"\x08\x00" or f[14] >> 4 != 4 or f[23] != 17:
        return None
    ihl = (f[14] & 15) * 4
    total = f[16] << 8 | f[17]
    u = 14 + ihl
    length = f[u + 4] << 8 | f[u + 5]
    if total < ihl + length:
        return None
    return (f[6:12], f[0:6], tuple(f[26:30]), tuple(f[30:34]),
            f[u] << 8 | f[u + 1], f[u + 2] << 8 | f[u + 3], f[u + 8:u + length])


# ----- bits and packet RAM images -----

def bits_lsb_first(data):
    return [(byte >> i) & 1 for byte in data for i in range(8)]


def pack_bits(bits):
    """Bits into 16-bit words, first bit in bit 0."""
    words = []
    for i in range(0, len(bits), 16):
        w = 0
        for k, b in enumerate(bits[i:i + 16]):
            w |= b << k
        words.append(w)
    return words


def tx_image(frame_with_fcs):
    """Packet RAM words for eth_tx: [bits - 1, line bits..., spare bit]."""
    bits = bits_lsb_first(PREAMBLE + bytes(frame_with_fcs))
    return [len(bits) - 1] + pack_bits(bits + [0])


def rx_words(max_bytes):
    """(buffer words, X value) for eth_rx capturing up to max_bytes."""
    words = (max_bytes * 8 + 15) // 16 + 2
    return words, max_bytes * 8 - 1


def rx_decode(words, x_init):
    """Turn an eth_rx buffer back into bytes. `words` is everything the RX
    DMA wrote for one frame (data words, partial word, unused-bit count).
    Returns (frame bytes, bit count)."""
    if len(words) < 2:
        return b"", 0
    nbits = (x_init - words[-1]) & 0xFFFF
    full, part = divmod(nbits, 16)
    data = list(words[:full])
    if part:
        data.append(words[full] >> (16 - part))
    bits = []
    for i, w in enumerate(data):
        n = 16 if i < full else part
        bits += [(w >> k) & 1 for k in range(n)]
    out = bytearray()
    for i in range(0, len(bits) - 7, 8):
        out.append(sum(b << k for k, b in enumerate(bits[i:i + 8])))
    return bytes(out), nbits


def manchester(bits):
    """Line levels per half bit (1 = TD+ positive): the inverse, then the bit."""
    out = []
    for b in bits:
        out += [1 - b, b]
    return out


# ----- a tiny network stack: ARP, ping and UDP echo -----
# The chip moves the bits; the host answers. With these, a laptop with a
# static IP can `ping` the chip and get UDP datagrams echoed back.

ETH_ARP = 0x0806
DEFAULT_MAC = b"\x02PEMU\x01"       # locally administered
DEFAULT_IP = (192, 168, 7, 2)


def arp_request(src_mac, src_ip, target_ip):
    body = (b"\x00\x01\x08\x00\x06\x04\x00\x01" + bytes(src_mac) + bytes(src_ip)
            + bytes(6) + bytes(target_ip))
    return ethernet(BROADCAST, src_mac, ETH_ARP, body)


def icmp_echo_request(src_mac, dst_mac, src_ip, dst_ip, ident=1, seq=1, data=b"pemu-ping"):
    msg = bytearray([8, 0, 0, 0, ident >> 8, ident & 0xFF, seq >> 8, seq & 0xFF]) + bytes(data)
    c = _csum(bytes(msg))
    msg[2], msg[3] = c >> 8, c & 0xFF
    return ethernet(dst_mac, src_mac, ETH_IPV4, ipv4(src_ip, dst_ip, 1, bytes(msg)))


def respond(frame, mac=DEFAULT_MAC, ip=DEFAULT_IP):
    """The reply to a received frame (without FCS), or None.

    Answers ARP requests for `ip`, ICMP echo requests (ping) to `ip`, and
    UDP datagrams to `ip` port 7 (echo)."""
    f = bytes(frame)
    ip = bytes(ip)
    if len(f) < 42 or (f[0:6] != bytes(mac) and f[0:6] != BROADCAST):
        return None
    etype = f[12] << 8 | f[13]
    if etype == ETH_ARP:
        if f[14:22] != b"\x00\x01\x08\x00\x06\x04\x00\x01" or f[38:42] != ip:
            return None
        sender_mac, sender_ip = f[22:28], f[28:32]
        body = (b"\x00\x01\x08\x00\x06\x04\x00\x02" + bytes(mac) + ip + sender_mac + sender_ip)
        return ethernet(sender_mac, mac, ETH_ARP, body)
    if etype != ETH_IPV4 or f[14] >> 4 != 4 or f[30:34] != ip:
        return None
    ihl = (f[14] & 15) * 4
    total = f[16] << 8 | f[17]
    if _csum(f[14:14 + ihl]) != 0 or total > len(f) - 14:
        return None
    src_ip = f[26:30]
    payload = f[14 + ihl:14 + total]
    proto = f[23]
    if proto == 1 and len(payload) >= 8 and payload[0] == 8:  # ICMP echo request
        msg = bytearray(payload)
        msg[0] = 0  # echo reply
        msg[2] = msg[3] = 0
        c = _csum(bytes(msg))
        msg[2], msg[3] = c >> 8, c & 0xFF
        return ethernet(f[6:12], mac, ETH_IPV4, ipv4(ip, src_ip, 1, bytes(msg)))
    if proto == 17 and len(payload) >= 8:
        sport, dport = payload[0] << 8 | payload[1], payload[2] << 8 | payload[3]
        length = payload[4] << 8 | payload[5]
        if dport == 7:
            return ethernet(f[6:12], mac, ETH_IPV4,
                            ipv4(ip, src_ip, 17, udp(ip, src_ip, 7, sport, payload[8:length])))
    return None


def parse_icmp_echo(frame):
    """(type, ident, seq, data) of an ICMP echo frame, checking checksums."""
    f = bytes(frame)
    if len(f) < 42 or f[12:14] != b"\x08\x00" or f[23] != 1:
        return None
    ihl = (f[14] & 15) * 4
    total = f[16] << 8 | f[17]
    msg = f[14 + ihl:14 + total]
    if _csum(f[14:14 + ihl]) != 0 or _csum(msg) != 0:
        return None
    return msg[0], msg[4] << 8 | msg[5], msg[6] << 8 | msg[7], msg[8:]
