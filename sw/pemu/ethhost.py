"""An Ethernet station on PEMU: both engines, the packet RAM and DMA.

Engine 0 runs eth_tx on the TD+/TD- pair, engine 1 runs eth_rx on the
receive comparator input. The packet RAM is split: words 0-63 receive
(frames up to 124 bytes are captured whole), words 64-127 transmit (frames
up to 117 bytes including the FCS). Works on any bench (model, RTL).
"""

from . import eth
from .scenarios import ETH_RD, ETH_RX_ORIGIN, ETH_TX_ORIGIN, eth_rx_cfg, eth_tx_cfg, program

RX_BASE, RX_WORDS = 0, 64
TX_BASE, TX_WORDS = 64, 64
MAX_RX_BYTES = (RX_WORDS - 2) * 2       # 124
MAX_TX_BYTES = ((TX_WORDS - 1) * 16 - 1) // 8 - 8   # frame incl. FCS: 117


class Station:
    def __init__(self, bench, rd_pin=ETH_RD, loopback=False, tx_timeout=0xFFFF, nlp_div=11):
        self.b = bench
        self.rd_pin = rd_pin
        self.loopback = loopback
        self.tx_cfg = eth_tx_cfg(timeout=tx_timeout, nlp_div=nlp_div)
        self.rx_cfg = eth_rx_cfg(rd_pin, MAX_RX_BYTES)
        self.x = self.rx_cfg.x

    async def start(self):
        b = self.b
        await b.write(0x60, 0)
        await b.setup(0, program("eth_tx"), ETH_TX_ORIGIN, self.tx_cfg)
        await b.setup(1, program("eth_rx"), ETH_RX_ORIGIN, self.rx_cfg)
        await b.flag_clr(3)
        await b.dma(1, RX_BASE, RX_WORDS, rx=True)
        await b.start(3, loopback=self.loopback)

    async def send(self, frame):
        """Queue a frame (without FCS). Returns at once; frames queue up."""
        f = eth.with_fcs(frame)
        if len(f) > MAX_TX_BYTES:
            raise ValueError("frame too long for the packet RAM (%d > %d bytes)" % (len(f), MAX_TX_BYTES))
        img = eth.tx_image(f)
        while (await self.b.dma_state(0))[1]:
            pass  # the previous frame is still being fetched
        await self.b.ram_write(TX_BASE, img)
        await self.b.dma(0, TX_BASE, len(img))
        await self.b.flag_set(1)

    async def poll(self):
        """A received frame (with FCS), or None if none is waiting. Frames
        with a bad FCS (noise, truncation) are returned too: check fcs_ok."""
        if not (await self.b.flags()) & 2:
            return None
        end, _ = await self.b.dma_state(1)
        words = await self.b.ram_read(RX_BASE, (end - RX_BASE) % 128)
        await self.b.flag_clr(2)
        await self.b.dma(1, RX_BASE, RX_WORDS, rx=True)
        data, _ = eth.rx_decode(words, self.x)
        return data

    async def serve(self, mac=eth.DEFAULT_MAC, ip=eth.DEFAULT_IP, frames=None, polls=1_000_000):
        """Answer ARP, ping and UDP echo. Stops after `frames` good frames."""
        seen = 0
        for _ in range(polls):
            f = await self.poll()
            if f is None:
                continue
            if not eth.fcs_ok(f):
                continue
            seen += 1
            reply = eth.respond(f[:-4], mac, ip)
            if reply:
                await self.send(reply)
            if frames is not None and seen >= frames:
                return seen
        return seen
