"""Routing engine: path computation, route selection and route updates."""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

from .monitor import LinkMonitor
from .policies import INF, FlowContext, RoutingPolicy
from .topology import Network, link_id

Path = list[str]


@dataclass
class Flow:
    id: str
    src: str
    dst: str
    demand_mbps: float


@dataclass
class RouteChange:
    time: float
    flow: str
    old_path: Path | None
    new_path: Path | None
    old_cost: float
    new_cost: float
    reason: str

    def describe(self) -> str:
        fmt = lambda p: "→".join(p) if p else "(none)"
        return f"t={self.time:6.1f}s  {self.flow}: {fmt(self.old_path)}  ⇒  {fmt(self.new_path)}   [{self.reason}]"


def path_links(path: Path | None) -> list[str]:
    if not path:
        return []
    return [link_id(a, b) for a, b in zip(path, path[1:])]


class RoutingEngine:
    def __init__(self, network: Network, policy: RoutingPolicy, monitor: LinkMonitor):
        self.net = network
        self.policy = policy
        self.monitor = monitor
        self.routes: dict[str, Path | None] = {}
        self.costs: dict[str, float] = {}
        self.last_change: dict[str, float] = {}
        # Per-flow, per-link best (lowest) cost breakdown seen since the route
        # was installed -- used to explain *why* a later route change happened.
        self._baseline_parts: dict[str, dict[str, dict]] = {}
        # This flow's own contribution to each link's measured load, smoothed
        # with the monitor's EWMA so it cancels exactly when subtracted.
        self._own_load: dict[str, dict[str, float]] = {}
        self.history: list[RouteChange] = []

    # ---- cost evaluation ------------------------------------------------
    def link_cost(self, lid: str, flow: Flow) -> tuple[float, dict]:
        est = self.monitor.est[lid]
        if not est.up:
            return INF, {}
        own = self._own_load.get(flow.id, {}).get(lid, 0.0)
        return self.policy.link_cost(self.net.links[lid], est, FlowContext(flow.demand_mbps, own))

    def path_cost(self, path: Path | None, flow: Flow) -> float:
        if not path:
            return INF
        return sum(self.link_cost(lid, flow)[0] for lid in path_links(path))

    def shortest_path(self, src: str, dst: str, flow: Flow) -> tuple[Path | None, float]:
        """Dijkstra over the policy's link costs (all non-negative)."""
        dist = {src: 0.0}
        prev: dict[str, str] = {}
        done: set[str] = set()
        heap = [(0.0, src)]
        while heap:
            d, u = heapq.heappop(heap)
            if u in done:
                continue
            done.add(u)
            if u == dst:
                break
            for v, link in self.net.neighbors(u):
                if v in done:
                    continue
                c, _ = self.link_cost(link.id, flow)
                if math.isinf(c):
                    continue
                nd = d + c
                if nd < dist.get(v, INF) - 1e-12:
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(heap, (nd, v))
        if dst not in done:
            return None, INF
        path = [dst]
        while path[-1] != src:
            path.append(prev[path[-1]])
        return path[::-1], dist[dst]

    # ---- route updates --------------------------------------------------
    def update(self, t: float, flows: list[Flow]) -> list[RouteChange]:
        self._track_own_load(flows)
        changes = []
        for flow in flows:
            change = self._update_flow(t, flow)
            if change:
                changes.append(change)
        self.history.extend(changes)
        return changes

    def _update_flow(self, t: float, flow: Flow) -> RouteChange | None:
        cur = self.routes.get(flow.id)
        best, best_cost = self.shortest_path(flow.src, flow.dst, flow)
        if flow.id not in self.routes:
            return self._install(t, flow, None, best, INF, best_cost, "initial route")

        cur_cost = self.path_cost(cur, flow)
        self.costs[flow.id] = cur_cost
        if best == cur:
            self._refresh_baseline(flow, cur)
            return None

        if math.isinf(cur_cost):
            reason = self._failure_reason(cur) if cur else "destination reachable again"
            return self._install(t, flow, cur, best, cur_cost, best_cost, reason)

        if best is None or best_cost >= cur_cost - 1e-9:
            return None
        improvement = (cur_cost - best_cost) / cur_cost
        if improvement <= self.policy.hysteresis:
            return None
        if t - self.last_change.get(flow.id, -INF) < self.policy.hold_down_s:
            return None
        reason = self._degradation_reason(flow, cur, improvement, cur_cost - best_cost)
        return self._install(t, flow, cur, best, cur_cost, best_cost, reason)

    def _install(self, t, flow, old, new, old_cost, new_cost, reason) -> RouteChange:
        self.routes[flow.id] = new
        self.costs[flow.id] = new_cost
        self.last_change[flow.id] = t
        self._baseline_parts[flow.id] = {}
        self._refresh_baseline(flow, new)
        return RouteChange(t, flow.id, old, new, old_cost, new_cost, reason)

    def _track_own_load(self, flows: list[Flow]) -> None:
        a = self.monitor.alpha
        for flow in flows:
            own = self._own_load.setdefault(flow.id, {})
            on_path = set(path_links(self.routes.get(flow.id)))
            for lid in self.net.links:
                sample = flow.demand_mbps if lid in on_path else 0.0
                own[lid] = (1 - a) * own.get(lid, 0.0) + a * sample

    def _refresh_baseline(self, flow: Flow, path: Path | None) -> None:
        base = self._baseline_parts.setdefault(flow.id, {})
        for lid in path_links(path):
            parts = self.link_cost(lid, flow)[1]
            old = base.get(lid)
            base[lid] = parts if old is None else {k: min(v, old.get(k, v)) for k, v in parts.items()}

    def _failure_reason(self, path: Path) -> str:
        down = [lid for lid in path_links(path) if not self.monitor.est[lid].up]
        return f"link {', '.join(down)} declared down" if down else "path unusable"

    def _degradation_reason(self, flow: Flow, cur: Path, improvement: float, gap: float) -> str:
        """Find the (link, metric) on the current path whose cost grew most."""
        before = self._baseline_parts.get(flow.id, {})
        # Only blame a metric if its growth explains a good part of the gap;
        # otherwise the alternative simply got better (e.g. a link was repaired).
        worst, worst_delta = None, max(1.0, 0.5 * gap)
        for lid in path_links(cur):
            now = self.link_cost(lid, flow)[1]
            for metric, value in now.items():
                delta = value - before.get(lid, {}).get(metric, value)
                if delta > worst_delta:
                    worst, worst_delta = (lid, metric), delta
        gain = f"{improvement:.0%} cheaper"
        if worst is None:
            return f"better path available ({gain})"
        lid, metric = worst
        e = self.monitor.est[lid]
        detail = {
            "delay": f"delay ≈{e.latency_ms:.0f}ms, util {e.load_mbps / max(e.capacity_mbps, 1e-9):.0%}",
            "loss": f"loss ≈{e.loss:.1%}, util {e.load_mbps / max(e.capacity_mbps, 1e-9):.0%}",
            "bandwidth": f"capacity {e.capacity_mbps:.0f}Mb/s, load {e.load_mbps:.0f}Mb/s",
            "stability": f"{e.flaps} recent flaps",
        }.get(metric, metric)
        return f"{metric} on {lid} degraded: {detail} ({gain})"
