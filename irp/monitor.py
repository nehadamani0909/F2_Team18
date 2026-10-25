"""Link-state measurement: what each router actually *knows* about its links.

Every probe interval, each link is probed (like BFD hellos plus interface
counters). Measurements are noisy and smoothed with an EWMA, so the routing
engine reacts to trends rather than to single samples.

Failure detection works like a dead timer: a link is declared down after
``dead_after`` consecutive missed probes. Missed probes are also fed into the
loss estimate, which is what lets loss-aware policies move traffic *before* the
dead timer fires.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from .topology import Network


def binomial(rng: random.Random, n: int, p: float) -> int:
    if p <= 0.0:
        return 0
    if p >= 1.0:
        return n
    if hasattr(rng, "binomialvariate"):
        return rng.binomialvariate(n, p)
    return sum(1 for _ in range(n) if rng.random() < p)


@dataclass
class LinkEstimate:
    latency_ms: float       # smoothed propagation latency
    loss: float             # smoothed random-loss rate (probe loss + error counters)
    capacity_mbps: float    # interface speed
    load_mbps: float        # smoothed offered load (all traffic)
    up: bool = True
    missed: int = 0
    flap_score: float = 0.0  # decaying count of recent down transitions
    flaps: int = 0
    last_update: float = 0.0


class LinkMonitor:
    def __init__(self, network: Network, rng: random.Random, alpha: float = 0.3,
                 dead_after: int = 3, probe_count: int = 50, jitter: float = 0.05,
                 flap_half_life_s: float = 20.0):
        self.net = network
        self.rng = rng
        self.alpha = alpha
        self.dead_after = dead_after
        self.probe_count = probe_count
        self.jitter = jitter
        self.flap_half_life_s = flap_half_life_s
        self.est: dict[str, LinkEstimate] = {}
        for lid, link in network.links.items():
            n = link.nominal
            self.est[lid] = LinkEstimate(n.latency_ms, n.loss, n.bandwidth_mbps,
                                         n.background_mbps)

    def _ewma(self, old: float, sample: float) -> float:
        return (1 - self.alpha) * old + self.alpha * sample

    def probe(self, t: float, loads: dict[str, float]) -> list[str]:
        """Take one round of measurements. Returns ids of links whose believed
        up/down status changed."""
        changed = []
        for lid, link in self.net.links.items():
            e, s = self.est[lid], link.state
            if e.flap_score:
                e.flap_score *= 0.5 ** ((t - e.last_update) / self.flap_half_life_s)
            e.last_update = t
            # Offered load comes from interface counters; tracked even while down
            # so the engine's own-load EWMA stays in lock-step with it.
            e.load_mbps = self._ewma(e.load_mbps, loads.get(lid, 0.0))

            if not s.up:
                e.missed += 1
                # Every probe was lost: that is evidence of loss too.
                e.loss = self._ewma(e.loss, 1.0)
                if e.up and e.missed >= self.dead_after:
                    e.up = False
                    e.flaps += 1
                    e.flap_score += 1.0
                    changed.append(lid)
                continue

            lat = s.latency_ms * max(0.0, 1.0 + self.rng.gauss(0.0, self.jitter))
            loss = binomial(self.rng, self.probe_count, s.loss) / self.probe_count
            if not e.up:
                # Link came back: start from fresh measurements, not stale ones.
                e.up = True
                e.latency_ms, e.loss = lat, loss
                e.capacity_mbps = s.bandwidth_mbps
                changed.append(lid)
            else:
                e.latency_ms = self._ewma(e.latency_ms, lat)
                e.loss = self._ewma(e.loss, loss)
                e.capacity_mbps = s.bandwidth_mbps
            e.missed = 0
        return changed
