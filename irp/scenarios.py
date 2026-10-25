"""Topologies and network experiments.

Main topology (``mesh8``)::

            B ------ E
          / |        | \\
        A - C ------ F - H
          \\ |        | /
            D ------ G

Flow f1 (A→H, 40 Mb/s) is the flow under test; its shortest path is A→B→E→H
(20 ms). Alternatives: A→C→F→H (22 ms) and the long, fat A→D→G→H (45 ms,
1 Gb/s). Flow f2 (C→E, 30 Mb/s) is competing traffic.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable

from .engine import Flow
from .simulator import Event, degrade, fail, restore
from .topology import Network

FAULT_T, FAULT_END = 15.0, 40.0


def mesh8() -> Network:
    net = Network("mesh8")
    for name, pos in {"A": (0, 1), "B": (1, 2), "C": (1, 1), "D": (1, 0),
                      "E": (2.4, 2), "F": (2.4, 1), "G": (2.4, 0), "H": (3.4, 1)}.items():
        net.add_router(name, pos)
    for a, b, lat, bw in [
        ("A", "B", 5, 100), ("A", "C", 7, 100), ("A", "D", 15, 1000),
        ("B", "E", 10, 100), ("C", "F", 9, 100), ("D", "G", 15, 1000),
        ("E", "H", 5, 100), ("F", "H", 6, 100), ("G", "H", 15, 1000),
        ("B", "C", 3, 100), ("C", "D", 4, 100), ("E", "F", 3, 100), ("F", "G", 4, 100),
    ]:
        net.add_link(a, b, lat, bw, loss=0.001)
    return net


def diamond() -> Network:
    """The motivating example: A→B→D is fast but lossy, A→C→D slow but clean."""
    net = Network("diamond")
    for name, pos in {"A": (0, 1), "B": (1, 2), "C": (1, 0), "D": (2, 1)}.items():
        net.add_router(name, pos)
    net.add_link("A", "B", 10, 100, loss=0.001)
    net.add_link("B", "D", 10, 100, loss=0.15)
    net.add_link("A", "C", 17.5, 100, loss=0.005)
    net.add_link("C", "D", 17.5, 100, loss=0.005)
    return net


MESH_FLOWS = [Flow("f1", "A", "H", 40.0), Flow("f2", "C", "E", 30.0)]


@dataclass
class Scenario:
    name: str
    title: str
    description: str
    topology: Callable[[], Network]
    flows: list[Flow]
    events: Callable[[int], list[Event]] = lambda seed: []
    duration_s: float = 60.0
    fault_time: float | None = FAULT_T
    fault_end: float | None = FAULT_END
    focus_flow: str = "f1"
    tags: list[str] = field(default_factory=list)

    def build(self, seed: int = 0):
        return self.topology(), [Flow(**vars(f)) for f in self.flows], self.events(seed)


def _flapping(seed: int) -> list[Event]:
    evs, t = [], FAULT_T
    while t < FAULT_END - 1:
        evs += [fail(t, "E-H"), restore(t + 2.0, "E-H")]
        t += 4.0
    return evs


def _congestion(seed: int) -> list[Event]:
    steps = [(15, 50), (18, 70), (21, 85), (24, 95), (FAULT_END, 0)]
    return [degrade(t, "B-E", f"B-E background {bg} Mb/s", background_mbps=bg) for t, bg in steps]


def _chaos(seed: int) -> list[Event]:
    """Random independent faults; each lasts 5–15 s then reverts."""
    rng = random.Random(1000 + seed)
    net = mesh8()
    lids = sorted(net.links)
    evs = []
    for _ in range(7):
        lid = rng.choice(lids)
        n = net.links[lid].nominal
        t0 = round(rng.uniform(5, 45), 1)
        t1 = round(t0 + rng.uniform(5, 15), 1)
        kind = rng.choice(["latency", "loss", "bandwidth", "fail"])
        if kind == "fail":
            evs += [fail(t0, lid), restore(t1, lid)]
        elif kind == "latency":
            evs += [degrade(t0, lid, latency_ms=round(n.latency_ms * rng.uniform(4, 12), 1)),
                    degrade(t1, lid, latency_ms=n.latency_ms)]
        elif kind == "loss":
            evs += [degrade(t0, lid, loss=round(rng.uniform(0.05, 0.25), 3)),
                    degrade(t1, lid, loss=n.loss)]
        else:
            evs += [degrade(t0, lid, bandwidth_mbps=round(n.bandwidth_mbps * rng.uniform(0.05, 0.3), 1)),
                    degrade(t1, lid, bandwidth_mbps=n.bandwidth_mbps)]
    return evs


SCENARIOS: dict[str, Scenario] = {s.name: s for s in [
    Scenario("steady", "Steady state",
             "No faults. Sanity check: the adaptive protocol should not cost anything when the network is healthy.",
             mesh8, MESH_FLOWS, fault_time=None, fault_end=None),
    Scenario("latency_spike", "Latency spike",
             "B-E latency jumps 10 → 150 ms at 15 s (e.g. traffic rerouted onto a satellite backup) and recovers at 40 s.",
             mesh8, MESH_FLOWS,
             lambda s: [degrade(FAULT_T, "B-E", "B-E latency 150 ms", latency_ms=150),
                        degrade(FAULT_END, "B-E", "B-E latency back to 10 ms", latency_ms=10)]),
    Scenario("bandwidth_drop", "Bandwidth drop",
             "E-H capacity falls 100 → 15 Mb/s at 15 s (link renegotiates to a lower rate); f1 needs 40 Mb/s.",
             mesh8, MESH_FLOWS,
             lambda s: [degrade(FAULT_T, "E-H", "E-H capacity 15 Mb/s", bandwidth_mbps=15),
                        degrade(FAULT_END, "E-H", "E-H capacity back to 100 Mb/s", bandwidth_mbps=100)]),
    Scenario("loss_increase", "Packet loss increase",
             "B-E random loss rises 0.1% → 12% at 15 s (failing optic / interference) and recovers at 40 s.",
             mesh8, MESH_FLOWS,
             lambda s: [degrade(FAULT_T, "B-E", "B-E loss 12%", loss=0.12),
                        degrade(FAULT_END, "B-E", "B-E loss back to 0.1%", loss=0.001)]),
    Scenario("link_failure", "Link failure",
             "B-E goes hard down at 15 s and comes back at 40 s. Tests failure detection and recovery time.",
             mesh8, MESH_FLOWS, lambda s: [fail(FAULT_T, "B-E"), restore(FAULT_END, "B-E")]),
    Scenario("congestion", "Congestion build-up",
             "Cross traffic on B-E ramps 50 → 95 Mb/s between 15 s and 24 s, clears at 40 s.",
             mesh8, MESH_FLOWS, _congestion),
    Scenario("flapping", "Flapping link",
             "E-H fails for 2 s every 4 s between 15 s and 40 s. Tests route stability / flap damping.",
             mesh8, MESH_FLOWS, _flapping),
    Scenario("chaos", "Random faults",
             "Seven random faults (failures, latency, loss, bandwidth) on random links; different per seed.",
             mesh8, MESH_FLOWS, _chaos, fault_time=None, fault_end=None),
    Scenario("lossy_shortcut", "Lossy shortcut (A→B→D vs A→C→D)",
             "The motivating example: A→B→D is 20 ms with 15% loss, A→C→D is 35 ms with ~1% loss. "
             "At 30 s B-D is repaired, so the fast path becomes the right choice again.",
             diamond, [Flow("f1", "A", "D", 20.0)],
             lambda s: [degrade(30.0, "B-D", "B-D repaired (loss 0.1%)", loss=0.001)],
             fault_time=None, fault_end=None),
]}
