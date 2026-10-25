"""Experimental evaluation: adaptive protocol vs. baselines across scenarios.

Runs every (scenario, policy, seed) combination, aggregates the metrics with
95% confidence intervals, tests adaptive vs. the OSPF-like baseline with
Welch's t-test, and writes CSV tables, a Markdown report and figures.
"""
from __future__ import annotations

import math
import warnings
from pathlib import Path

import pandas as pd
from scipy import stats

from . import run_scenario
from .metrics import flow_series, summarize
from .scenarios import SCENARIOS

POLICY_ORDER = ["adaptive", "ospf", "latency", "hopcount"]
POLICY_LABEL = {"adaptive": "Adaptive (ours)", "ospf": "OSPF-like static",
                "latency": "Latency-only", "hopcount": "Hop count"}
# Categorical slots 1-4 of the validated reference palette, fixed per policy.
POLICY_COLOR = {"adaptive": "#2a78d6", "ospf": "#eb6834", "latency": "#1baf7a", "hopcount": "#eda100"}

# metric -> (label, higher_is_better)
METRICS = {
    "delivery_ratio": ("Delivery ratio", True),
    "throughput_ratio": ("Throughput / demand", True),
    "mean_latency_ms": ("Mean latency (ms)", False),
    "p95_latency_ms": ("p95 latency (ms)", False),
    "route_changes": ("Route changes", False),
    "fault_throughput_ratio": ("Throughput during fault (f1)", True),
    "recovery_time_s": ("Recovery time (s)", False),
    "degraded_s": ("Time degraded during fault (s)", False),
}


def run_matrix(seeds: int = 10, scenarios=None, policies=None, progress=None) -> pd.DataFrame:
    rows = []
    scenarios = scenarios or list(SCENARIOS)
    policies = policies or POLICY_ORDER
    for name in scenarios:
        sc = SCENARIOS[name]
        for policy in policies:
            for seed in range(seeds):
                r = run_scenario(name, policy, seed=seed, record_links=False)
                m = summarize(r, sc.focus_flow, sc.fault_time, sc.fault_end)
                rows.append({"scenario": name, "seed": seed, **m})
            if progress:
                progress(name, policy)
    return pd.DataFrame(rows)


def _ci95(x: pd.Series) -> float:
    x = x.dropna()
    if len(x) < 2:
        return 0.0
    return float(stats.t.ppf(0.975, len(x) - 1) * x.std(ddof=1) / math.sqrt(len(x)))


def aggregate(raw: pd.DataFrame) -> pd.DataFrame:
    cols = [m for m in METRICS if m in raw]
    g = raw.groupby(["scenario", "policy"], sort=False)[cols]
    mean = g.mean().add_suffix("_mean")
    ci = g.agg(_ci95).add_suffix("_ci95")
    out = pd.concat([mean, ci], axis=1)
    return out[[c for m in cols for c in (f"{m}_mean", f"{m}_ci95")]].reset_index()


def compare(raw: pd.DataFrame, ours: str = "adaptive", baseline: str = "ospf", alpha: float = 0.05) -> pd.DataFrame:
    """Welch's t-test of ``ours`` vs ``baseline`` per scenario and metric."""
    rows = []
    for scenario, df in raw.groupby("scenario", sort=False):
        a, b = df[df.policy == ours], df[df.policy == baseline]
        for metric, (label, higher_better) in METRICS.items():
            if metric not in df or a[metric].isna().all():
                continue
            x, y = a[metric].dropna(), b[metric].dropna()
            diff = x.mean() - y.mean()
            if x.std(ddof=1) == 0 and y.std(ddof=1) == 0:
                p = 0.0 if abs(diff) > 1e-12 else 1.0
            else:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    p = float(stats.ttest_ind(x, y, equal_var=False).pvalue)
            better = diff > 0 if higher_better else diff < 0
            verdict = "no significant difference"
            if p < alpha and abs(diff) > 1e-9:
                verdict = "better" if better else "worse"
            rows.append({"scenario": scenario, "metric": metric, "label": label,
                         ours: x.mean(), baseline: y.mean(), "diff": diff, "p_value": p,
                         "verdict": verdict})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- figures ---
_INK, _INK2, _MUTED, _GRID, _SURFACE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#fcfcfb"


def _style(ax):
    ax.set_facecolor(_SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(_GRID)
    ax.tick_params(colors=_MUTED, labelcolor=_INK2, labelsize=8)
    ax.grid(axis="y", color=_GRID, linewidth=0.6)
    ax.set_axisbelow(True)


# Which f1 measurement each time-series panel shows (latency faults don't hurt goodput).
_PANEL_METRIC = {"latency_spike": ("latency_ms", "f1 latency (ms)", 180)}


def plot_timeseries(out: Path, seed: int = 0) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    names = [n for n, s in SCENARIOS.items() if s.fault_time is not None]
    cols = 3
    rows = math.ceil(len(names) / cols)
    fig, axes = plt.subplots(rows, cols, figsize=(13, 3.4 * rows), facecolor=_SURFACE)
    # Baselines first and thin, ours last and thicker, so it stays visible where lines overlap.
    draw_order = ["hopcount", "latency", "ospf", "adaptive"]
    for ax, name in zip(axes.flat, names):
        sc = SCENARIOS[name]
        col, ylabel, ymax = _PANEL_METRIC.get(name, ("goodput_mbps", "f1 goodput (Mb/s)", 45))
        _style(ax)
        ax.axvspan(sc.fault_time, sc.fault_end, color=_GRID, alpha=0.5, linewidth=0)
        for policy in draw_order:
            r = run_scenario(name, policy, seed=seed, record_links=False)
            s = pd.DataFrame(flow_series(r, sc.focus_flow))
            smooth = s[col].astype(float).rolling(5, min_periods=1).mean()
            ax.plot(s["t"], smooth, color=POLICY_COLOR[policy], label=POLICY_LABEL[policy],
                    linewidth=2.2 if policy == "adaptive" else 1.4)
        ax.set_title(sc.title, color=_INK, fontsize=10, loc="left")
        ax.set_xlabel("time (s)", color=_INK2, fontsize=8)
        ax.set_ylabel(ylabel, color=_INK2, fontsize=8)
        ax.set_ylim(0, ymax)
    for ax in axes.flat[len(names):]:
        ax.axis("off")
    handles, labels = axes.flat[0].get_legend_handles_labels()
    order = [labels.index(POLICY_LABEL[p]) for p in POLICY_ORDER]
    fig.legend([handles[k] for k in order], [labels[k] for k in order], loc="upper center",
               ncol=4, frameon=False, fontsize=9, labelcolor=_INK2, bbox_to_anchor=(0.5, 1.0))
    fig.text(0.5, 0.935, f"Seed {seed}, 0.5 s moving average. Shaded = fault window; f1 demand = 40 Mb/s. "
             "Where lines coincide, the policies behaved identically.", ha="center", color=_MUTED, fontsize=8)
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    path = out / "fig_timeseries.png"
    fig.savefig(path, dpi=140, facecolor=_SURFACE)
    plt.close(fig)
    return path


def plot_metric(summary: pd.DataFrame, metric: str, out: Path, scenarios=None) -> Path:
    """Dot plot (mean ± 95% CI) -- dots, not bars, so the axis can zoom to the
    range where the policies actually differ."""
    import textwrap

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    label, higher_better = METRICS[metric]
    df = summary.dropna(subset=[f"{metric}_mean"])
    scenarios = scenarios or [s for s in SCENARIOS if s in set(df.scenario)]
    fig, ax = plt.subplots(figsize=(11, 3.8), facecolor=_SURFACE)
    _style(ax)
    n = len(POLICY_ORDER)
    step = 0.16
    for i, policy in enumerate(POLICY_ORDER):
        d = df[df.policy == policy].set_index("scenario").reindex(scenarios)
        xs = [k + (i - (n - 1) / 2) * step for k in range(len(scenarios))]
        ax.errorbar(xs, d[f"{metric}_mean"], yerr=d[f"{metric}_ci95"], fmt="o", markersize=7,
                    color=POLICY_COLOR[policy], ecolor=_INK2, elinewidth=0.8, capsize=2,
                    markeredgecolor=_SURFACE, markeredgewidth=1.5, label=POLICY_LABEL[policy])
    for k in range(1, len(scenarios)):
        ax.axvline(k - 0.5, color=_GRID, linewidth=0.6)
    ax.set_xlim(-0.5, len(scenarios) - 0.5)
    ax.set_xticks(range(len(scenarios)))
    ax.set_xticklabels([textwrap.fill(SCENARIOS[s].title, 14) for s in scenarios], fontsize=8)
    ax.tick_params(axis="x", length=0)
    ax.set_title(f"{label} — {'higher' if higher_better else 'lower'} is better (mean ± 95% CI)",
                 color=_INK, fontsize=10, loc="left")
    from matplotlib.lines import Line2D
    handles = [Line2D([], [], marker="o", linestyle="", markersize=7, color=POLICY_COLOR[p],
                      label=POLICY_LABEL[p]) for p in POLICY_ORDER]
    ax.legend(handles=handles, ncol=4, frameon=False, fontsize=8, labelcolor=_INK2,
              loc="lower right", bbox_to_anchor=(1, 1.0), borderaxespad=0.3)
    fig.tight_layout()
    path = out / f"fig_{metric}.png"
    fig.savefig(path, dpi=140, facecolor=_SURFACE)
    plt.close(fig)
    return path


# ----------------------------------------------------------------- report ---
def _fmt(v, metric):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    if "ratio" in metric:
        return f"{v:.3f}"
    if metric == "route_changes":
        return f"{v:.1f}"
    return f"{v:.2f}"


def write_report(raw: pd.DataFrame, summary: pd.DataFrame, cmp: pd.DataFrame,
                 out: Path, seeds: int, figures: list[Path]) -> Path:
    lines = ["# Experimental evaluation", "",
             f"Each scenario was run with **{seeds} random seeds** per policy (60 s simulated, 100 ms ticks, "
             "0.5 s probe interval, dead timer = 3 missed probes for every policy). "
             "Values are means; ± is the 95% confidence interval. "
             "Significance: Welch's t-test, adaptive vs. OSPF-like static routing, α = 0.05.", ""]

    lines += ["## Verdict per scenario (adaptive vs. OSPF-like)", "",
              "| Scenario | Reliability (delivery) | Throughput | Time degraded (s) | Recovery time (s) | Latency (ms) |",
              "|---|---|---|---|---|---|"]
    pick = lambda sc, m: cmp[(cmp.scenario == sc) & (cmp.metric == m)]
    sym = {"better": "✅ better", "worse": "❌ worse", "no significant difference": "≈ same"}
    for sc in SCENARIOS:
        cells = []
        for m in ["delivery_ratio", "throughput_ratio", "degraded_s", "recovery_time_s", "mean_latency_ms"]:
            row = pick(sc, m)
            if row.empty:
                cells.append("n/a")
                continue
            r = row.iloc[0]
            cells.append(f"{sym[r.verdict]} ({_fmt(r.adaptive, m)} vs {_fmt(r.ospf, m)})")
        lines.append(f"| {SCENARIOS[sc].title} | " + " | ".join(cells) + " |")
    lines.append("")

    for metric, (label, hib) in METRICS.items():
        if f"{metric}_mean" not in summary or summary[f"{metric}_mean"].isna().all():
            continue
        lines += [f"## {label} ({'higher' if hib else 'lower'} is better)", "",
                  "| Scenario | " + " | ".join(POLICY_LABEL[p] for p in POLICY_ORDER) + " |",
                  "|---|" + "---|" * len(POLICY_ORDER)]
        for sc in SCENARIOS:
            cells = []
            for p in POLICY_ORDER:
                row = summary[(summary.scenario == sc) & (summary.policy == p)]
                if row.empty or math.isnan(row.iloc[0][f"{metric}_mean"]):
                    cells.append("–")
                else:
                    r = row.iloc[0]
                    cells.append(f"{_fmt(r[f'{metric}_mean'], metric)} ± {_fmt(r[f'{metric}_ci95'], metric)}")
            if any(c != "–" for c in cells):
                lines.append(f"| {SCENARIOS[sc].title} | " + " | ".join(cells) + " |")
        lines.append("")

    lines += ["## Figures", ""] + [f"![{f.stem}]({f.name})" for f in figures] + [""]
    lines += ["## Metric definitions", "",
              "- **Delivery ratio** – delivered / sent packets, all flows.",
              "- **Throughput / demand** – delivered goodput divided by offered load, all flows.",
              "- **Throughput during fault** – the same, for flow f1 only, inside the fault window.",
              "- **Recovery time** – seconds from the fault until f1's goodput is back to ≥ 90% of its pre-fault "
              "level for 1 s. If it never recovers inside the fault window, the full window length is counted.",
              "- **Time degraded** – seconds inside the fault window during which f1's goodput (1 s moving "
              "average) was below 90% of its pre-fault level. Captures gradual faults and repeated outages, "
              "where a single recovery time is misleading.",
              "- **Route changes** – route switches after the initial route installation, all flows.", ""]
    path = out / "REPORT.md"
    path.write_text("\n".join(lines))
    return path


def evaluate(out_dir: str = "results", seeds: int = 10, verbose: bool = True) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    log = (lambda *a: print(*a, flush=True)) if verbose else (lambda *a: None)

    raw = run_matrix(seeds, progress=lambda s, p: log(f"  ran {s:15s} {p}"))
    summary = aggregate(raw)
    cmp = compare(raw)
    raw.to_csv(out / "raw_runs.csv", index=False)
    summary.to_csv(out / "summary.csv", index=False)
    cmp.to_csv(out / "comparison.csv", index=False)

    log("  plotting…")
    figures = [plot_timeseries(out),
               plot_metric(summary, "throughput_ratio", out),
               plot_metric(summary, "delivery_ratio", out),
               plot_metric(summary, "degraded_s", out),
               plot_metric(summary, "mean_latency_ms", out)]
    report = write_report(raw, summary, cmp, out, seeds, figures)
    log(f"  wrote {report}")
    return report
