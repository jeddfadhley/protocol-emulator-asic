"""Host operations shared by the model and RTL benches.

A bench provides: async write(addr, value), async read(addr, pop=True),
async run(n), async run_until(cond, limit), plus gpio()/ui()/monitor() to
attach device models. This mixin adds the higher-level host operations.
"""

from .host import config_writes, ctrl, load_writes
from .model import (A_CTRL, A_DMA0, A_FLAG_CLR, A_FLAG_SET, A_FLAGS, A_RAM_ADDR,
                    A_RAM_DATA, R_FIFO, R_STATUS, dma_word, engine_base)


class HostApi:
    async def apply(self, writes):
        for a, v in writes:
            await self.write(a, v)

    async def setup(self, engine, program, origin, cfg):
        await self.apply(load_writes(program, origin))
        await self.apply(config_writes(engine, program, origin, cfg))

    async def start(self, mask, loopback=False):
        await self.write(A_CTRL, ctrl(enable=mask, loopback=loopback, restart=mask))

    async def push(self, engine, value):
        await self.write(engine_base(engine) + R_FIFO, value)

    async def pop(self, engine):
        return await self.read(engine_base(engine) + R_FIFO)

    async def status(self, engine):
        return await self.read(engine_base(engine) + R_STATUS, pop=False)

    async def send(self, engine, words, polls=100_000):
        """Push words, polling STATUS for TX FIFO space when needed."""
        level = 4
        for w in words:
            for _ in range(polls):
                if level < 4:
                    break
                level = ((await self.status(engine)) >> 5) & 7
            else:
                raise TimeoutError("TX FIFO never drained")
            await self.push(engine, w)
            level += 1

    async def stream(self, engine, words, interval):
        """Push one word every `interval` clocks without polling: for real-time
        streams where the host knows the engine's consumption rate."""
        for w in words:
            t0 = self.cycles
            await self.push(engine, w)
            left = interval - (self.cycles - t0)
            if left < 0:
                raise RuntimeError("host too slow for this stream interval")
            await self.run(left)

    async def drain(self, engine, n, polls=100_000):
        """Pop n words from the RX FIFO, polling STATUS until each arrives."""
        out = []
        while len(out) < n:
            for _ in range(polls):
                level = ((await self.status(engine)) >> 8) & 7
                if level:
                    break
            else:
                raise TimeoutError(f"RX FIFO: got {len(out)} of {n} words")
            for _ in range(min(level, n - len(out))):
                out.append(await self.pop(engine))
        return out

    # ----- packet RAM, DMA and flags -----

    async def ram_write(self, addr, words):
        await self.write(A_RAM_ADDR, addr)
        burst = getattr(self, "write_burst", None)
        if burst:
            await burst(A_RAM_DATA, list(words))
        else:
            for w in words:
                await self.write(A_RAM_DATA, w)

    async def ram_read(self, addr, n):
        await self.write(A_RAM_ADDR, addr)
        burst = getattr(self, "read_burst", None)
        if burst and n:
            return await burst(A_RAM_DATA, n)
        return [await self.read(A_RAM_DATA) for _ in range(n)]

    async def dma(self, channel, addr, count, rx=False):
        await self.write(A_DMA0 + channel, dma_word(addr, count, rx))

    async def dma_state(self, channel):
        """(ADDR, COUNT) of a DMA channel."""
        v = await self.read(A_DMA0 + channel, pop=False)
        return v & 127, (v >> 7) & 255

    async def flags(self):
        return await self.read(A_FLAGS, pop=False)

    async def flag_set(self, mask):
        await self.write(A_FLAG_SET, mask)

    async def flag_clr(self, mask):
        await self.write(A_FLAG_CLR, mask)
