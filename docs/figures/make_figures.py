"""Draw the waveform figures in docs/writeup.md from simulation.

Each figure runs the real firmware (sw/programs/) on the Python model, the
reference the RTL is compared with on every clock, and records the pins.
Nothing is drawn by hand. Run from the repository root:

    python3 docs/figures/make_figures.py

Needs matplotlib.
"""

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "sw"))

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from pemu import eth  # noqa: E402
from pemu.bench import ModelBench  # noqa: E402
from pemu.periph_ext import CanNode  # noqa: E402
from pemu.scenarios import ETH_TD, eth_hello, eth_send, eth_tx_cfg, program  # noqa: E402
from pemu.scenarios_ext import CAN_RX_BIT, CAN_TXD, can_rx_run  # noqa: E402

INK, MUTED, ACCENT, SHADE = "#1f2328", "#59636e", "#0969da", "#ddf4ff"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "svg.fonttype": "none",
                     "axes.edgecolor": MUTED, "text.color": INK})


class HalfClockRecorder:
    """Records two pins in both halves of every clock (10 ns steps at 50 MHz),
    the way a 10BASE-T receiver sees late pin writes."""
    half_clock = True

    def __init__(self, a, b):
        self.a, self.b, self.samples = a, b, []

    def step2(self, early, late):
        for w in (early, late):
            self.samples.append(((w >> self.a) & 1, (w >> self.b) & 1))


def ethernet_figure(path):
    b = ModelBench()
    rec = b.monitor(HalfClockRecorder(ETH_TD, ETH_TD + 1))

    async def go():
        await b.setup(0, program("eth_tx"), 0, eth_tx_cfg(timeout=400, nlp_div=2))
        await b.start(1)
        await b.run(3000)
        await eth_send(b, eth_hello(1))
        await b.run(20_000)
    asyncio.run(go())

    line = [p - n for p, n in rec.samples]          # +1, -1, or 0 (idle)
    # the frame starts at the first transition followed by steady activity
    start = next(i for i in range(len(line) - 200)
                 if line[i] and all(line[j] for j in range(i, i + 200)))
    first, last = 52, 72                             # bits shown
    lo, hi = start + 10 * first, start + 10 * last
    t = [(i - start) / 10 for i in range(lo, hi + 1)]   # in bit times (10 samples)
    fig, ax = plt.subplots(figsize=(8, 2.3))
    for k in range(first, last):
        if 56 <= k < 64:
            ax.axvspan(k, k + 1, color=SHADE, lw=0)
    ax.step(t, [rec.samples[i][0] + 1.4 for i in range(lo, hi + 1)], where="post", color=ACCENT, lw=1.4)
    ax.step(t, [rec.samples[i][1] for i in range(lo, hi + 1)], where="post", color=INK, lw=1.4)
    for k in range(first, last):
        mid = start + 10 * k + 7                     # second half of the bit
        ax.text(k + 0.5, 2.6, str(1 if line[mid] > 0 else 0), ha="center", color=MUTED)
        ax.axvline(k, color="#d0d7de", lw=0.5, zorder=0)
    ax.text(56.05, -0.55, "start-of-frame delimiter", color=MUTED)
    ax.text(first + 0.05, -0.55, "preamble", color=MUTED)
    ax.text(64.05, -0.55, "destination address (broadcast)", color=MUTED)
    ax.set_yticks([0.5, 1.9], ["TD-", "TD+"])
    ax.set_xticks(range(first, last + 1, 2), [f"{k / 10:.1f}" for k in range(first, last + 1, 2)])
    ax.set_xlabel("time from start of frame (µs); each bit is 100 ns, value shown above")
    ax.set_xlim(first, last)
    ax.set_ylim(-0.8, 2.9)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.tick_params(left=False)
    fig.tight_layout()
    fig.savefig(path)
    return line, start


def can_figure(path):
    b = ModelBench()
    trace = []
    cycle = b._cycle

    def recording_cycle(*a, **k):
        r = cycle(*a, **k)
        for dev, _ in b.ui_devs:
            if hasattr(dev, "node_tx"):
                trace.append((dev.node_tx[0], (b.core.pin_out >> CAN_TXD) & 1, dev.level))
        return r
    b._cycle = recording_cycle
    node = CanNode(CAN_RX_BIT, tx_queue=[(0x123, b"PEMU")], ack=False, name="tx")
    asyncio.run(can_rx_run(b, node))
    assert node.acked == [True]

    ack = next(i for i, (_, txd, _) in enumerate(trace) if txd == 0)   # PEMU drives the bus
    bit = CAN_RX_BIT
    slot = ack + 1                                   # the bus follows TXD one clock later
    lo, hi = slot - 6 * bit, slot + 6 * bit
    t = [(i - slot) / bit for i in range(lo, hi)]
    fig, ax = plt.subplots(figsize=(8, 2.4))
    ax.axvspan(0, 1, color=SHADE, lw=0)
    rows = [("sender TX", 0, 4.4, MUTED), ("PEMU TXD", 1, 2.2, ACCENT), ("bus", 2, 0, INK)]
    for name, k, y, colour in rows:
        ax.step(t, [trace[i][k] * 1.4 + y for i in range(lo, hi)], where="post", color=colour, lw=1.4)
    labels = [(-6, 6.75, "CRC (end)"), (-1, 6.15, "CRC delimiter"), (0, 6.75, "ACK slot"),
              (1, 6.15, "ACK delimiter"), (2, 6.75, "end of frame (7 recessive bits)")]
    for x, y, s in labels:
        ax.text(x + 0.05, y, s, color=MUTED)
    for k in range(-6, 7):
        ax.axvline(k, color="#d0d7de", lw=0.5, zorder=0)
    ax.set_yticks([0.7, 2.9, 5.1], ["bus", "PEMU TXD", "sender TX"])
    ax.set_xticks(range(-6, 7), [str(k) for k in range(-6, 7)])
    ax.set_xlabel("bit times (1 µs each) from the ACK slot; low = dominant")
    ax.set_xlim(-6, 6)
    ax.set_ylim(-0.3, 7.2)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.tick_params(left=False)
    fig.tight_layout()
    fig.savefig(path)


if __name__ == "__main__":
    ethernet_figure(os.path.join(HERE, "ethernet.svg"))
    can_figure(os.path.join(HERE, "can-ack.svg"))
    print("wrote", os.path.join(HERE, "ethernet.svg"), "and can-ack.svg")
