"""Discrete-time network simulator.

Each tick (default 100 ms):
  1. scheduled events change the true link state (failures, latency spikes...)
  2. every probe interval the monitor measures links and the engine re-routes
  3. every flow sends a batch of packets along its current route; each packet
     survives a link with probability (1 - random loss) * (1 - overflow loss)
     and picks up propagation + queueing delay.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from .engine import Flow, RouteChange, RoutingEngine, path_links
from .monitor import LinkMonitor, binomial
from .policies import RoutingPolicy
from .topology import Network, overflow_loss, queue_delay_ms


@dataclass
class Event:
    time: float
    link: str
    changes: dict
    label: str = ""

    def apply(self, net: Network) -> None:
        state = net.link(self.link).state
        for key, value in self.changes.items():
            setattr(state, key, value)


def fail(t: float, link: str) -> Event:
    return Event(t, link, {"up": False}, f"{link} fails")


def restore(t: float, link: str) -> Event:
    return Event(t, link, {"up": True}, f"{link} restored")


def degrade(t: float, link: str, label: str = "", **changes) -> Event:
    text = ", ".join(f"{k}={v}" for k, v in changes.items())
    return Event(t, link, changes, label or f"{link}: {text}")


@dataclass
class SimConfig:
    duration_s: float = 60.0
    dt: float = 0.1
    probe_interval_s: float = 0.5
    packets_per_tick: int = 100
    seed: int = 0
    record_links: bool = True
    monitor: dict = field(default_factory=dict)  # extra LinkMonitor kwargs


@dataclass
class SimResult:
    policy: str
    network: Network                 # initial topology (for drawing)
    flows: list[Flow]
    events: list[Event]
    config: SimConfig
    samples: list[dict]              # one row per (tick, flow)
    link_log: list[dict]             # one row per (tick, link), if recorded
    route_changes: list[RouteChange]


class Simulation:
    def __init__(self, network: Network, flows: list[Flow], policy: RoutingPolicy,
                 events: list[Event] | None = None, config: SimConfig | None = None):
        self.config = config or SimConfig()
        self.initial = network.copy()
        self.net = network.copy()
        self.flows = flows
        self.policy = policy
        self.events = sorted(events or [], key=lambda e: e.time)
        self.rng = random.Random(self.config.seed)
        self.monitor = LinkMonitor(self.net, self.rng, **self.config.monitor)
        self.engine = RoutingEngine(self.net, policy, self.monitor)

    def link_loads(self) -> dict[str, float]:
        loads = {lid: l.state.background_mbps for lid, l in self.net.links.items()}
        for f in self.flows:
            for lid in path_links(self.engine.routes.get(f.id)):
                loads[lid] += f.demand_mbps
        return loads

    def run(self) -> SimResult:
        cfg = self.config
        steps = int(round(cfg.duration_s / cfg.dt))
        probe_every = max(1, int(round(cfg.probe_interval_s / cfg.dt)))
        # Random probe phase per seed, so faults don't always line up with probes.
        phase = self.rng.randrange(probe_every)
        samples, link_log = [], []
        ev = 0

        for i in range(steps):
            t = round(i * cfg.dt, 6)
            while ev < len(self.events) and self.events[ev].time <= t + 1e-9:
                self.events[ev].apply(self.net)
                ev += 1

            if i == 0 or (i - phase) % probe_every == 0:
                self.monitor.probe(t, self.link_loads())
                self.engine.update(t, self.flows)

            loads = self.link_loads()
            for f in self.flows:
                samples.append(self._transmit(t, f, loads))
            if cfg.record_links:
                for lid, link in self.net.links.items():
                    s = link.state
                    rho = loads[lid] / s.bandwidth_mbps if s.bandwidth_mbps > 0 else 0.0
                    link_log.append({
                        "t": t, "link": lid, "up": s.up,
                        "latency_ms": s.latency_ms + queue_delay_ms(rho),
                        "loss": 1 - (1 - s.loss) * (1 - overflow_loss(rho)),
                        "capacity_mbps": s.bandwidth_mbps, "load_mbps": loads[lid],
                        "util": rho,
                        "est_up": self.monitor.est[lid].up,
                        "est_loss": self.monitor.est[lid].loss,
                    })

        return SimResult(self.policy.name, self.initial, self.flows, self.events,
                         cfg, samples, link_log, list(self.engine.history))

    def _transmit(self, t: float, f: Flow, loads: dict[str, float]) -> dict:
        n = self.config.packets_per_tick
        path = self.engine.routes.get(f.id)
        p_ok, latency = (1.0, 0.0) if path else (0.0, 0.0)
        for lid in path_links(path):
            s = self.net.links[lid].state
            if not s.up or s.bandwidth_mbps <= 0:
                p_ok = 0.0
                break
            rho = loads[lid] / s.bandwidth_mbps
            p_ok *= (1 - s.loss) * (1 - overflow_loss(rho))
            latency += s.latency_ms + queue_delay_ms(rho)
        delivered = n - binomial(self.rng, n, 1 - p_ok) if p_ok > 0 else 0
        if delivered:
            latency *= max(0.0, 1 + self.rng.gauss(0, 0.02))
        return {
            "t": t, "flow": f.id, "path": "→".join(path) if path else "",
            "sent": n, "delivered": delivered,
            "latency_ms": latency if delivered else None,
            "goodput_mbps": f.demand_mbps * delivered / n,
            "demand_mbps": f.demand_mbps,
            "cost": self.engine.costs.get(f.id),
        }


def simulate(network, flows, policy, events=None, **config) -> SimResult:
    return Simulation(network, flows, policy, events, SimConfig(**config)).run()
