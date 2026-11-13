"""Host side of ARM Serial Wire Debug on the `swd` firmware.

The firmware only shifts bits (see sw/programs/swd.pasm); this module builds
SWD packets from those shifts: request, turnaround, ACK, data and parity.
It works on any bench (model, RTL) or on the board through the same host API.
"""

from .periph_ext import SWD_FAULT, SWD_OK, SWD_WAIT, parity

ACK_NAMES = {SWD_OK: "OK", SWD_WAIT: "WAIT", SWD_FAULT: "FAULT"}


class SwdError(Exception):
    pass


class SwdHost:
    def __init__(self, bench, engine=0):
        self.b, self.e = bench, engine

    async def write_bits(self, value, n):
        words = [(value >> (16 * i)) & 0xFFFF for i in range((n + 15) // 16)]
        await self.b.send(self.e, [(n - 1)] + words)

    async def read_bits(self, n):
        await self.b.send(self.e, [(n - 1) | 1 << 6])
        words = await self.b.drain(self.e, n // 16 + 1)
        value = 0
        for i, w in enumerate(words[:-1]):
            value |= w << (16 * i)
        rest = n % 16
        if rest:
            value |= (words[-1] >> (16 - rest)) << (16 * (n // 16))
        return value

    async def line_reset(self):
        """56 ones, then 8 idle cycles."""
        await self.write_bits((1 << 56) - 1, 56)
        await self.write_bits(0, 8)

    async def _request(self, apndp, rnw, addr):
        a2, a3 = (addr >> 2) & 1, (addr >> 3) & 1
        req = 1 | apndp << 1 | rnw << 2 | a2 << 3 | a3 << 4
        req |= parity(apndp | rnw << 1 | a2 << 2 | a3 << 3) << 5 | 1 << 7   # stop 0, park 1
        await self.write_bits(req, 8)
        ack = (await self.read_bits(4)) >> 1          # turnaround, then 3 ACK bits
        return ack

    async def read(self, apndp, addr):
        ack = await self._request(apndp, 1, addr)
        if ack != SWD_OK:
            await self.read_bits(1)                   # turnaround
            raise SwdError(ACK_NAMES.get(ack, f"no response ({ack:#05b})"))
        v = await self.read_bits(33)
        await self.read_bits(1)                       # turnaround
        await self.write_bits(0, 8)                   # idle cycles
        data, par = v & 0xFFFFFFFF, v >> 32
        if par != parity(data):
            raise SwdError(f"read parity error ({data:#010x})")
        return data

    async def write(self, apndp, addr, value, bad_parity=False):
        ack = await self._request(apndp, 0, addr)
        await self.read_bits(1)                       # turnaround
        if ack != SWD_OK:
            raise SwdError(ACK_NAMES.get(ack, f"no response ({ack:#05b})"))
        await self.write_bits(value | (parity(value) ^ bad_parity) << 32, 33)
        await self.write_bits(0, 8)

    async def dp_read(self, addr):
        return await self.read(0, addr)

    async def dp_write(self, addr, value):
        await self.write(0, addr, value)

    async def mem_write(self, address, value):
        await self.write(1, 0x04, address)            # TAR
        await self.write(1, 0x0C, value)              # DRW

    async def mem_read(self, address):
        await self.write(1, 0x04, address)
        await self.read(1, 0x0C)                      # posted: returns the previous result
        return await self.dp_read(0x0C)               # RDBUFF
