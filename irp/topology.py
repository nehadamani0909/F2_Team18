"""Network model: routers, links and the *true* physical state of each link.

The routing engine never reads this state directly -- it only sees what the
LinkMonitor measures (see monitor.py). The simulator uses the true state to
decide what actually happens to packets.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace

# Queueing model shared by the simulator (ground truth) and the adaptive cost
# function (prediction). M/M/1-style: delay grows as rho / (1 - rho).
QUEUE_SCALE_MS = 3.0
QUEUE_MAX_MS = 250.0


def queue_delay_ms(rho: float) -> float:
    """Queueing delay for a link at utilisation ``rho`` (load / capacity)."""
    if rho <= 0:
        return 0.0
    if rho >= 1:
        return QUEUE_MAX_MS
    return min(QUEUE_MAX_MS, QUEUE_SCALE_MS * rho / (1.0 - rho))


def overflow_loss(rho: float) -> float:
    """Fraction of offered traffic dropped because the link is over capacity."""
    return 0.0 if rho <= 1.0 else 1.0 - 1.0 / rho


def link_id(a: str, b: str) -> str:
    return "-".join(sorted((a, b)))


@dataclass
class LinkState:
    latency_ms: float          # propagation delay
    bandwidth_mbps: float      # capacity
    loss: float = 0.0          # random (non-congestion) loss probability
    background_mbps: float = 0.0  # cross traffic not controlled by us
    up: bool = True


@dataclass
class Link:
    a: str
    b: str
    state: LinkState
    nominal: LinkState = field(default=None)  # configured values at build time

    def __post_init__(self):
        if self.nominal is None:
            self.nominal = replace(self.state)

    @property
    def id(self) -> str:
        return link_id(self.a, self.b)

    def other(self, node: str) -> str:
        return self.b if node == self.a else self.a


class Network:
    """Undirected graph of routers connected by full-duplex links."""

    def __init__(self, name: str = "network"):
        self.name = name
        self.routers: list[str] = []
        self.links: dict[str, Link] = {}
        self.adj: dict[str, list[str]] = {}
        self.pos: dict[str, tuple[float, float]] = {}

    def add_router(self, name: str, pos: tuple[float, float] | None = None) -> None:
        if name in self.adj:
            return
        self.routers.append(name)
        self.adj[name] = []
        if pos is not None:
            self.pos[name] = pos

    def add_link(self, a: str, b: str, latency_ms: float, bandwidth_mbps: float,
                 loss: float = 0.0, background_mbps: float = 0.0) -> Link:
        self.add_router(a)
        self.add_router(b)
        link = Link(a, b, LinkState(latency_ms, bandwidth_mbps, loss, background_mbps))
        if link.id in self.links:
            raise ValueError(f"duplicate link {link.id}")
        self.links[link.id] = link
        self.adj[a].append(link.id)
        self.adj[b].append(link.id)
        return link

    def link(self, a: str, b: str | None = None) -> Link:
        lid = a if b is None else link_id(a, b)
        if "-" in lid and b is None:
            lid = link_id(*lid.split("-"))
        return self.links[lid]

    def neighbors(self, node: str):
        for lid in self.adj[node]:
            link = self.links[lid]
            yield link.other(node), link

    def copy(self) -> "Network":
        return copy.deepcopy(self)
