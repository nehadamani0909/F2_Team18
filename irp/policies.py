"""Routing policies = link cost functions.

Every policy sees the same LinkMonitor view (same probe rate, same dead timer),
so comparisons isolate the effect of the cost function itself.

* ``hopcount``  -- RIP-style: every live link costs 1.
* ``ospf``      -- static link-state: configured latency as the metric; only
                   reacts when a link is declared down / comes back.
* ``latency``   -- dynamic, latency-only (measured delay + queueing).
* ``adaptive``  -- the intelligent protocol: multi-metric, load-aware,
                   loss-aware and flap-damped, with hysteresis.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .monitor import LinkEstimate
from .topology import Link, overflow_loss, queue_delay_ms

INF = math.inf


@dataclass
class FlowContext:
    demand_mbps: float
    own_load_mbps: float  # this flow's share of the link's measured (smoothed) load


class RoutingPolicy:
    name = "base"
    label = "Base"
    dynamic = False        # reacts to measured conditions (not only up/down)?
    hysteresis = 0.0       # min relative improvement needed to switch path
    hold_down_s = 0.0      # min time between non-failure route changes

    def link_cost(self, link: Link, est: LinkEstimate, ctx: FlowContext) -> tuple[float, dict]:
        raise NotImplementedError


class HopCountPolicy(RoutingPolicy):
    name, label = "hopcount", "Hop count (RIP-like)"

    def link_cost(self, link, est, ctx):
        return 1.0, {"hops": 1.0}


class StaticOSPFPolicy(RoutingPolicy):
    name, label = "ospf", "Static shortest path (OSPF-like)"

    def link_cost(self, link, est, ctx):
        c = link.nominal.latency_ms
        return c, {"delay": c}


def _predicted_utilisation(est: LinkEstimate, ctx: FlowContext) -> float:
    """Utilisation this link would have if the flow were routed over it.
    The flow's own traffic is subtracted first so a flow is not scared off its
    current path by its own load (a classic cause of route oscillation)."""
    if est.capacity_mbps <= 0:
        return INF
    other = max(0.0, est.load_mbps - ctx.own_load_mbps)
    return (other + ctx.demand_mbps) / est.capacity_mbps


class LatencyPolicy(RoutingPolicy):
    name, label = "latency", "Dynamic latency-only"
    dynamic = True
    hysteresis = 0.10
    hold_down_s = 2.0

    def link_cost(self, link, est, ctx):
        delay = est.latency_ms + queue_delay_ms(_predicted_utilisation(est, ctx))
        return delay, {"delay": delay}


@dataclass
class AdaptiveWeights:
    latency: float = 1.0       # cost per ms of expected delay
    loss: float = 300.0        # cost per unit of -ln(1 - p)
    bandwidth: float = 1.0     # cost per (ref_bw / available bw)
    stability: float = 25.0    # cost per recent flap
    ref_bw_mbps: float = 100.0


@dataclass
class AdaptivePolicy(RoutingPolicy):
    """Composite cost, in 'effective milliseconds'::

        cost = w_lat  * (latency + queue_delay(rho'))
             + w_loss * -ln(1 - p')                    # additive along a path
             + w_bw   * ref_bw / available_bw
             + w_stab * flap_score

    rho' is the *predicted* utilisation with this flow added and p' combines
    measured random loss with predicted overflow loss. Using -ln(1-p) makes loss
    additive, so Dijkstra minimises end-to-end loss exactly.
    """
    weights: AdaptiveWeights = field(default_factory=AdaptiveWeights)
    hysteresis: float = 0.15
    hold_down_s: float = 3.0

    name = "adaptive"
    label = "Adaptive multi-metric (ours)"
    dynamic = True

    def link_cost(self, link, est, ctx):
        w = self.weights
        rho = _predicted_utilisation(est, ctx)
        delay = est.latency_ms + queue_delay_ms(rho)
        p = 1.0 - (1.0 - min(est.loss, 0.999)) * (1.0 - overflow_loss(rho))
        p = min(p, 0.999)
        available = max(est.capacity_mbps - max(0.0, est.load_mbps - ctx.own_load_mbps), 1e-3)
        parts = {
            "delay": w.latency * delay,
            "loss": w.loss * -math.log(1.0 - p),
            "bandwidth": w.bandwidth * w.ref_bw_mbps / available,
            "stability": w.stability * est.flap_score,
        }
        return sum(parts.values()), parts


POLICIES = {
    "adaptive": AdaptivePolicy,
    "ospf": StaticOSPFPolicy,
    "latency": LatencyPolicy,
    "hopcount": HopCountPolicy,
}


def make_policy(name: str, **kwargs) -> RoutingPolicy:
    return POLICIES[name](**kwargs)
