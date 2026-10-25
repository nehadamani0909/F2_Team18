import math
import random

import pytest

from irp import SCENARIOS, run_scenario, summarize
from irp.engine import Flow, RoutingEngine, path_links
from irp.monitor import LinkMonitor
from irp.policies import AdaptivePolicy, make_policy
from irp.scenarios import diamond, mesh8
from irp.simulator import degrade, fail, restore, simulate
from irp.topology import Network, overflow_loss, queue_delay_ms


def engine_for(net, policy="adaptive", seed=0):
    return RoutingEngine(net, make_policy(policy), LinkMonitor(net, random.Random(seed)))


# ---- physics -------------------------------------------------------------
def test_queue_and_overflow_model():
    assert queue_delay_ms(0) == 0
    assert queue_delay_ms(0.5) < queue_delay_ms(0.9) < queue_delay_ms(0.99)
    assert overflow_loss(0.8) == 0
    assert overflow_loss(2.0) == pytest.approx(0.5)


# ---- path computation ----------------------------------------------------
def test_dijkstra_finds_shortest_latency_path():
    path, cost = engine_for(mesh8(), "ospf").shortest_path("A", "H", Flow("f", "A", "H", 1))
    assert path == ["A", "B", "E", "H"]
    assert cost == pytest.approx(20)


def test_hopcount_minimises_hops():
    net = Network()
    net.add_link("A", "B", 1, 100)
    net.add_link("B", "C", 1, 100)
    net.add_link("A", "C", 50, 100)
    path, _ = engine_for(net, "hopcount").shortest_path("A", "C", Flow("f", "A", "C", 1))
    assert path == ["A", "C"]


def test_adaptive_prefers_reliable_path_in_motivating_example():
    flow = Flow("f", "A", "D", 20)
    assert engine_for(diamond(), "ospf").shortest_path("A", "D", flow)[0] == ["A", "B", "D"]
    assert engine_for(diamond(), "adaptive").shortest_path("A", "D", flow)[0] == ["A", "C", "D"]


def test_loss_cost_is_additive_along_path():
    # -ln(1-p) summed over links == -ln(prod(1-p)): end-to-end loss is minimised exactly.
    pol = AdaptivePolicy()
    p1, p2 = 0.1, 0.2
    w = pol.weights.loss
    assert w * (-math.log(1 - p1) - math.log(1 - p2)) == pytest.approx(w * -math.log((1 - p1) * (1 - p2)))


def test_adaptive_avoids_link_without_enough_bandwidth():
    net = Network()
    net.add_link("A", "B", 5, 10)     # fast but only 10 Mb/s
    net.add_link("A", "C", 10, 100)
    net.add_link("C", "B", 10, 100)
    path, _ = engine_for(net).shortest_path("A", "B", Flow("f", "A", "B", 40))
    assert path == ["A", "C", "B"]


def test_unreachable_destination():
    net = Network()
    net.add_link("A", "B", 1, 100)
    net.add_router("Z")
    assert engine_for(net).shortest_path("A", "Z", Flow("f", "A", "Z", 1)) == (None, math.inf)


# ---- monitoring / failure detection -------------------------------------
def test_dead_timer_declares_link_down_after_missed_probes():
    net = mesh8()
    mon = LinkMonitor(net, random.Random(0), dead_after=3)
    net.link("B-E").state.up = False
    for k in range(2):
        mon.probe(k * 0.5, {})
        assert mon.est["B-E"].up
    assert mon.probe(1.0, {}) == ["B-E"]
    assert not mon.est["B-E"].up
    assert mon.est["B-E"].flaps == 1


def test_missed_probes_raise_loss_estimate():
    net = mesh8()
    mon = LinkMonitor(net, random.Random(0))
    net.link("B-E").state.up = False
    mon.probe(0, {})
    assert mon.est["B-E"].loss > 0.25


# ---- route updates -------------------------------------------------------
@pytest.mark.parametrize("policy", ["ospf", "adaptive", "hopcount", "latency"])
def test_reroutes_around_failure_and_never_uses_dead_link(policy):
    sc = SCENARIOS["link_failure"]
    r = run_scenario("link_failure", policy, seed=3, record_links=False)
    during = [s for s in r.samples if s["flow"] == "f1" and 18 <= s["t"] < 40]
    assert during and all("B→E" not in s["path"] and "E→B" not in s["path"] for s in during)
    assert summarize(r, sc.focus_flow, sc.fault_time, sc.fault_end)["recovered"]


def test_hysteresis_blocks_marginal_switches():
    net = diamond()
    eng = engine_for(net)
    flow = Flow("f", "A", "D", 20)
    eng.update(0, [flow])
    assert eng.routes["f"] == ["A", "C", "D"]
    # Repair B-D just enough that A→B→D becomes ~8% cheaper: below the 15% hysteresis.
    lo, hi = 0.0, 0.15
    for _ in range(50):
        eng.monitor.est["B-D"].loss = mid = (lo + hi) / 2
        cur, alt = eng.path_cost(["A", "C", "D"], flow), eng.path_cost(["A", "B", "D"], flow)
        lo, hi = (mid, hi) if (cur - alt) / cur > 0.08 else (lo, mid)
    assert 0 < (cur - alt) / cur < 0.15
    assert eng.update(10, [flow]) == []
    assert eng.routes["f"] == ["A", "C", "D"]


def test_hold_down_limits_switch_rate():
    net = diamond()
    eng = engine_for(net)
    flow = Flow("f", "A", "D", 20)
    eng.update(0, [flow])
    eng.monitor.est["B-D"].loss = 0.0
    eng.monitor.est["A-C"].loss = 0.3
    assert eng.update(1.0, [flow]) == []          # inside hold-down (3 s)
    changes = eng.update(5.0, [flow])
    assert changes and changes[0].new_path == ["A", "B", "D"]
    assert "loss on A-C" in changes[0].reason


def test_route_change_reason_names_failed_link():
    r = run_scenario("link_failure", "ospf", seed=0, record_links=False)
    reasons = [c.reason for c in r.route_changes if c.flow == "f1"]
    assert any("B-E declared down" in x for x in reasons)


def test_flow_does_not_avoid_its_own_load():
    # A single 60 Mb/s flow on a 100 Mb/s link must not flee its own traffic.
    net = Network()
    net.add_link("A", "B", 5, 100)
    net.add_link("A", "C", 6, 100)
    net.add_link("C", "B", 6, 100)
    r = simulate(net, [Flow("f", "A", "B", 60)], make_policy("adaptive"), duration_s=30, seed=1)
    assert [c.reason for c in r.route_changes] == ["initial route"]


# ---- simulator / metrics -------------------------------------------------
def test_events_change_true_state():
    r = simulate(diamond(), [Flow("f", "A", "D", 10)], make_policy("ospf"),
                 [degrade(1.0, "B-D", loss=0.5), fail(2.0, "A-C"), restore(3.0, "A-C")],
                 duration_s=4, seed=0)
    log = {(row["t"], row["link"]): row for row in r.link_log}
    assert log[(0.5, "B-D")]["loss"] == pytest.approx(0.15)
    assert log[(1.5, "B-D")]["loss"] == pytest.approx(0.5)
    assert not log[(2.5, "A-C")]["up"] and log[(3.5, "A-C")]["up"]


def test_simulation_is_deterministic_per_seed():
    a = run_scenario("chaos", "adaptive", seed=4, record_links=False)
    b = run_scenario("chaos", "adaptive", seed=4, record_links=False)
    assert a.samples == b.samples


def test_overloaded_link_caps_goodput_at_capacity():
    net = Network()
    net.add_link("A", "B", 5, 10, loss=0)
    r = simulate(net, [Flow("f", "A", "B", 40)], make_policy("ospf"), duration_s=10, seed=0)
    m = summarize(r)
    assert m["avg_throughput_mbps"] == pytest.approx(10, rel=0.05)


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_adaptive_never_worse_reliability_than_static(name):
    sc = SCENARIOS[name]
    ours = summarize(run_scenario(name, "adaptive", seed=0, record_links=False))
    base = summarize(run_scenario(name, "ospf", seed=0, record_links=False))
    assert ours["delivery_ratio"] >= base["delivery_ratio"] - 0.005
