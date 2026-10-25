"""Command-line interface.

    python -m irp demo                         # the A→B→D vs A→C→D example + a failure
    python -m irp scenarios                    # list experiments
    python -m irp run link_failure -p adaptive # run one experiment, print route log
    python -m irp evaluate --seeds 10          # full evaluation → results/
    python -m irp dashboard                    # launch the Streamlit dashboard
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from . import SCENARIOS, run_scenario, summarize
from .engine import Flow, RoutingEngine
from .monitor import LinkMonitor
from .policies import POLICIES, make_policy
from .scenarios import diamond


def _print_metrics(m: dict) -> None:
    for k, v in m.items():
        if k == "policy":
            continue
        print(f"    {k:24s} {v:.3f}" if isinstance(v, float) else f"    {k:24s} {v}")


def cmd_demo(args) -> None:
    import random
    print("== Path choice on the motivating example ==")
    print("   A→B→D : 20 ms, 15% loss      A→C→D : 35 ms, ~1% loss\n")
    net = diamond()
    flow = Flow("f1", "A", "D", 20.0)
    for name in ["ospf", "adaptive"]:
        engine = RoutingEngine(net, make_policy(name), LinkMonitor(net, random.Random(0)))
        path, cost = engine.shortest_path("A", "D", flow)
        print(f"  {name:9s} chooses {'→'.join(path)}  (path cost {cost:.1f})")
        if name == "adaptive":
            for lid in ["A-B", "B-D", "A-C", "C-D"]:
                c, parts = engine.link_cost(lid, flow)
                detail = ", ".join(f"{k}={v:.1f}" for k, v in parts.items())
                print(f"      {lid}: {c:6.1f}  ({detail})")

    print("\n== Link failure: B-E fails at 15 s, restored at 40 s ==")
    for name in ["ospf", "adaptive"]:
        sc = SCENARIOS["link_failure"]
        r = run_scenario("link_failure", name, seed=0, record_links=False)
        print(f"\n  [{name}]")
        for c in r.route_changes:
            print("   ", c.describe())
        _print_metrics(summarize(r, sc.focus_flow, sc.fault_time, sc.fault_end))


def cmd_scenarios(args) -> None:
    for name, sc in SCENARIOS.items():
        print(f"{name:16s} {sc.title}\n{'':16s} {sc.description}\n")


def cmd_run(args) -> None:
    sc = SCENARIOS[args.scenario]
    r = run_scenario(args.scenario, args.policy, seed=args.seed, record_links=False)
    print(f"{sc.title} — policy={args.policy} seed={args.seed}\n")
    print("Events:")
    for e in r.events:
        print(f"  t={e.time:6.1f}s  {e.label}")
    print("\nRoute changes:")
    for c in r.route_changes:
        print("  " + c.describe())
    print("\nMetrics:")
    _print_metrics(summarize(r, sc.focus_flow, sc.fault_time, sc.fault_end))


def cmd_evaluate(args) -> None:
    from .evaluation import evaluate
    print(f"Running evaluation with {args.seeds} seeds per (scenario, policy)…")
    evaluate(args.out, args.seeds)


def cmd_dashboard(args) -> None:
    app = Path(__file__).resolve().parent.parent / "dashboard" / "app.py"
    sys.exit(subprocess.call([sys.executable, "-m", "streamlit", "run", str(app)]))


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="irp", description="Intelligent Routing Protocol")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo").set_defaults(fn=cmd_demo)
    sub.add_parser("scenarios").set_defaults(fn=cmd_scenarios)
    r = sub.add_parser("run")
    r.add_argument("scenario", choices=list(SCENARIOS))
    r.add_argument("-p", "--policy", default="adaptive", choices=list(POLICIES))
    r.add_argument("-s", "--seed", type=int, default=0)
    r.set_defaults(fn=cmd_run)
    e = sub.add_parser("evaluate")
    e.add_argument("--seeds", type=int, default=10)
    e.add_argument("--out", default="results")
    e.set_defaults(fn=cmd_evaluate)
    sub.add_parser("dashboard").set_defaults(fn=cmd_dashboard)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
