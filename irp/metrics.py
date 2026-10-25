"""Performance metrics computed from a SimResult."""
from __future__ import annotations

import math
from collections import defaultdict

from .simulator import SimResult


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return math.nan
    values = sorted(values)
    k = (len(values) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def flow_series(result: SimResult, flow: str) -> list[dict]:
    return [s for s in result.samples if s["flow"] == flow]


def recovery_time(series: list[dict], fault_time: float, dt: float,
                  threshold: float = 0.9, window_s: float = 1.0, baseline_s: float = 5.0,
                  horizon: float | None = None) -> tuple[float, bool]:
    """Seconds from ``fault_time`` until goodput is back to ``threshold`` x its
    pre-fault level and stays there for ``window_s``. Returns (time, recovered).
    If service never dropped, recovery is 0. If it never recovers before
    ``horizon``, the time to the horizon is returned with recovered=False."""
    pre = [s["goodput_mbps"] for s in series if fault_time - baseline_s <= s["t"] < fault_time]
    if not pre:
        return math.nan, False
    target = threshold * sum(pre) / len(pre)
    after = [s for s in series if s["t"] >= fault_time and (horizon is None or s["t"] < horizon)]
    w = max(1, int(round(window_s / dt)))
    for i in range(len(after) - w + 1):
        if sum(s["goodput_mbps"] for s in after[i:i + w]) / w >= target:
            return round(after[i]["t"] - fault_time, 3), True
    end = horizon if horizon is not None else (after[-1]["t"] + dt if after else fault_time)
    return round(end - fault_time, 3), False


def degraded_time(series: list[dict], fault_time: float, fault_end: float | None, dt: float,
                  threshold: float = 0.9, window_s: float = 1.0, baseline_s: float = 5.0) -> float:
    """Seconds inside the fault window during which the flow's goodput (1 s
    moving average) was below ``threshold`` x its pre-fault level. Unlike
    recovery time this also captures gradual faults and repeated outages."""
    pre = [s["goodput_mbps"] for s in series if fault_time - baseline_s <= s["t"] < fault_time]
    if not pre:
        return math.nan
    target = threshold * sum(pre) / len(pre)
    w = max(1, int(round(window_s / dt)))
    vals = [s["goodput_mbps"] for s in series]
    bad = 0
    for i, s in enumerate(series):
        if fault_time <= s["t"] < (fault_end if fault_end is not None else math.inf):
            win = vals[max(0, i - w + 1):i + 1]
            bad += sum(win) / len(win) < target
    return round(bad * dt, 3)


def summarize(result: SimResult, focus_flow: str | None = None,
              fault_time: float | None = None, fault_end: float | None = None) -> dict:
    """Aggregate metrics over all flows, plus fault metrics for ``focus_flow``."""
    sent = sum(s["sent"] for s in result.samples)
    delivered = sum(s["delivered"] for s in result.samples)
    lat = [s["latency_ms"] for s in result.samples if s["latency_ms"] is not None]
    lat_w = sum(s["latency_ms"] * s["delivered"] for s in result.samples if s["latency_ms"] is not None)

    by_t = defaultdict(float)
    demand = defaultdict(float)
    for s in result.samples:
        by_t[s["t"]] += s["goodput_mbps"]
        demand[s["t"]] += s["demand_mbps"]
    total_demand = sum(demand.values())

    out = {
        "policy": result.policy,
        "delivery_ratio": delivered / sent if sent else math.nan,
        "mean_latency_ms": lat_w / delivered if delivered else math.nan,
        "p95_latency_ms": _percentile(lat, 0.95),
        "avg_throughput_mbps": sum(by_t.values()) / len(by_t) if by_t else 0.0,
        "throughput_ratio": sum(by_t.values()) / total_demand if total_demand else math.nan,
        "route_changes": sum(1 for c in result.route_changes if c.reason != "initial route"),
    }

    if focus_flow and fault_time is not None:
        series = flow_series(result, focus_flow)
        rt, ok = recovery_time(series, fault_time, result.config.dt, horizon=fault_end)
        out["recovery_time_s"] = rt
        out["recovered"] = ok
        out["degraded_s"] = degraded_time(series, fault_time, fault_end, result.config.dt)
        win = [s for s in series if fault_time <= s["t"] < (fault_end or math.inf)]
        if win:
            out["fault_throughput_ratio"] = sum(s["goodput_mbps"] for s in win) / sum(s["demand_mbps"] for s in win)
            out["fault_delivery_ratio"] = sum(s["delivered"] for s in win) / sum(s["sent"] for s in win)
            wl = [s["latency_ms"] for s in win if s["latency_ms"] is not None]
            out["fault_mean_latency_ms"] = sum(wl) / len(wl) if wl else math.nan
    return out
