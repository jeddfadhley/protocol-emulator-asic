"""Host operations shared by the model and RTL benches.

A bench provides: async write(addr, value), async read(addr, pop=True),
async run(n), async run_until(cond, limit), plus gpio()/ui()/monitor() to
attach device models. This mixin adds the higher-level host operations.
"""

from .host import config_writes, ctrl, load_writes
from .model import A_CTRL, R_FIFO, R_STATUS, engine_base


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
