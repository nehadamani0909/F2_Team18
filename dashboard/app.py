"""Streamlit dashboard for the Intelligent Routing Protocol.

    streamlit run dashboard/app.py      (or: python -m irp dashboard)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from irp import SCENARIOS, run_scenario, summarize  # noqa: E402
from irp.evaluation import METRICS, POLICY_COLOR, POLICY_LABEL, POLICY_ORDER  # noqa: E402
from irp.policies import AdaptiveWeights  # noqa: E402
from irp.simulator import degrade, fail, restore  # noqa: E402

# Status palette (reserved for link health; always paired with a text label).
STATUS = {"Healthy": "#0ca30c", "Degraded": "#fab219", "Severe": "#ec835a", "Down": "#d03b3b"}
ROUTE_COLOR = "#6250d6"
MUTED = "#898781"

st.set_page_config(page_title="Intelligent Routing Protocol", page_icon="🛰️", layout="wide")


# ------------------------------------------------------------------ helpers --
def custom_events(specs, scenario_name):
    net = SCENARIOS[scenario_name].topology()
    events = []
    for link, kind, value, t0, t1 in specs:
        nominal = net.link(link).nominal
        if kind == "fail":
            events += [fail(t0, link), restore(t1, link)]
        else:
            attr = {"latency": "latency_ms", "loss": "loss", "bandwidth": "bandwidth_mbps",
                    "background": "background_mbps"}[kind]
            events += [degrade(t0, link, f"{link} {kind} → {value:g}", **{attr: value}),
                       degrade(t1, link, f"{link} {kind} restored", **{attr: getattr(nominal, attr)})]
    return events


@st.cache_data(show_spinner=False, max_entries=64)
def simulate_cached(scenario, policy, seed, weights, hysteresis, hold_down, specs):
    kwargs = {}
    if policy == "adaptive":
        kwargs = {"weights": AdaptiveWeights(*weights), "hysteresis": hysteresis, "hold_down_s": hold_down}
    r = run_scenario(scenario, policy, seed=seed, policy_kwargs=kwargs,
                     extra_events=custom_events(specs, scenario))
    sc = SCENARIOS[scenario]
    m = summarize(r, sc.focus_flow, sc.fault_time, sc.fault_end)
    return {
        "samples": pd.DataFrame(r.samples),
        "links": pd.DataFrame(r.link_log),
        "changes": pd.DataFrame([{
            "time (s)": c.time, "flow": c.flow,
            "from": "→".join(c.old_path) if c.old_path else "—",
            "to": "→".join(c.new_path) if c.new_path else "—",
            "reason": c.reason} for c in r.route_changes]),
        "events": [(e.time, e.label) for e in r.events],
        "metrics": m,
        "network": r.network,
    }


def health(row, nominal_latency):
    if not row.up:
        return "Down"
    if row.loss > 0.05 or row.util > 0.95:
        return "Severe"
    if row.loss > 0.01 or row.util > 0.8 or row.latency_ms > 2 * nominal_latency:
        return "Degraded"
    return "Healthy"


def topology_figure(res, t, flow, title):
    net = res["network"]
    links = res["links"]
    snap = links[links.t == links.t[links.t <= t].max()].set_index("link")
    s = res["samples"]
    srow = s[(s.flow == flow) & (s.t <= t)].tail(1)
    path = srow.path.iloc[0].split("→") if len(srow) and srow.path.iloc[0] else []
    on_route = {"-".join(sorted(p)) for p in zip(path, path[1:])}

    fig = go.Figure()
    for lid in on_route:  # route halo underneath
        a, b = lid.split("-")
        (x0, y0), (x1, y1) = net.pos[a], net.pos[b]
        fig.add_trace(go.Scatter(x=[x0, x1], y=[y0, y1], mode="lines", hoverinfo="skip",
                                 line=dict(color=ROUTE_COLOR, width=18), opacity=0.5, showlegend=False))
    mx, my, hover = [], [], []
    for lid, link in net.links.items():
        row = snap.loc[lid]
        status = health(row, link.nominal.latency_ms)
        (x0, y0), (x1, y1) = net.pos[link.a], net.pos[link.b]
        fig.add_trace(go.Scatter(
            x=[x0, x1], y=[y0, y1], mode="lines", hoverinfo="skip", showlegend=False,
            line=dict(color=STATUS[status], width=4, dash="dot" if status == "Down" else "solid")))
        mx.append((x0 + x1) / 2)
        my.append((y0 + y1) / 2)
        hover.append(
            f"<b>{lid}</b> — {status}<br>latency {row.latency_ms:.1f} ms (nominal {link.nominal.latency_ms:g})"
            f"<br>loss {row.loss:.1%}<br>load {row.load_mbps:.0f} / {row.capacity_mbps:.0f} Mb/s "
            f"({row.util:.0%})<br>believed {'up' if row.est_up else 'DOWN'}, est. loss {row.est_loss:.1%}")
    fig.add_trace(go.Scatter(x=mx, y=my, mode="markers", marker=dict(size=18, opacity=0),
                             hovertext=hover, hoverinfo="text", showlegend=False))
    nodes = net.routers
    fig.add_trace(go.Scatter(
        x=[net.pos[n][0] for n in nodes], y=[net.pos[n][1] for n in nodes], mode="markers+text",
        text=nodes, textposition="middle center", hoverinfo="text", showlegend=False,
        hovertext=[f"router {n}" for n in nodes],
        textfont=dict(color="white", size=14),
        marker=dict(size=34, color=[ROUTE_COLOR if n in path else "#52514e" for n in nodes],
                    line=dict(color="white", width=2))))
    for name, color in STATUS.items():  # legend entries carry the label, not color alone
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="lines", name=name,
                                 line=dict(color=color, width=4, dash="dot" if name == "Down" else "solid")))
    fig.add_trace(go.Scatter(x=[None], y=[None], mode="lines", name=f"{flow} route",
                             line=dict(color=ROUTE_COLOR, width=10), opacity=0.5))
    fig.update_layout(
        title=dict(text=f"{title}<br><sup>{flow}: {' → '.join(path) or 'no route'}</sup>", x=0, font=dict(size=15)),
        height=380, margin=dict(l=10, r=10, t=60, b=10),
        xaxis=dict(visible=False), yaxis=dict(visible=False, scaleanchor="x"),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", y=-0.02, x=0, font=dict(size=11)), hoverlabel=dict(font_size=12))
    return fig


def timeseries_figure(results, flow, column, ylabel, t, sc, events):
    fig = go.Figure()
    if sc.fault_time is not None:
        fig.add_vrect(x0=sc.fault_time, x1=sc.fault_end, fillcolor=MUTED, opacity=0.12, line_width=0)
    for tt, _ in events:
        fig.add_vline(x=tt, line=dict(color=MUTED, width=1, dash="dot"))
    for policy, res in results.items():
        s = res["samples"]
        s = s[s.flow == flow]
        y = s[column].astype(float).rolling(5, min_periods=1).mean()
        fig.add_trace(go.Scatter(x=s.t, y=y, mode="lines", name=POLICY_LABEL[policy],
                                 line=dict(color=POLICY_COLOR[policy], width=2),
                                 hovertemplate="t=%{x:.1f}s<br>%{y:.1f}<extra>" + POLICY_LABEL[policy] + "</extra>"))
    fig.add_vline(x=t, line=dict(color=ROUTE_COLOR, width=2))
    fig.update_layout(height=280, margin=dict(l=10, r=10, t=30, b=10), hovermode="x unified",
                      yaxis_title=ylabel, xaxis_title="time (s)", yaxis_rangemode="tozero",
                      legend=dict(orientation="h", y=1.15, x=0),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
    fig.update_xaxes(showgrid=False)
    fig.update_yaxes(gridcolor="rgba(137,135,129,0.25)")
    return fig


# ------------------------------------------------------------------ sidebar --
st.sidebar.title("🛰️ Experiment")
names = list(SCENARIOS)
scenario = st.sidebar.selectbox("Scenario", names, format_func=lambda n: SCENARIOS[n].title,
                                index=names.index("link_failure"))
sc = SCENARIOS[scenario]
st.sidebar.caption(sc.description)
policy = st.sidebar.selectbox("Routing policy", POLICY_ORDER, format_func=POLICY_LABEL.get)
baseline = st.sidebar.selectbox("Compare against", [p for p in POLICY_ORDER if p != policy],
                                format_func=POLICY_LABEL.get,
                                index=[p for p in POLICY_ORDER if p != policy].index("ospf") if policy != "ospf" else 0)
seed = st.sidebar.number_input("Random seed", 0, 10_000, 0)

with st.sidebar.expander("Adaptive cost weights"):
    st.caption("cost = w_lat·(latency+queue) + w_loss·−ln(1−p) + w_bw·ref/avail + w_stab·flaps")
    w = AdaptiveWeights()
    w_lat = st.slider("w_latency (per ms)", 0.0, 5.0, w.latency, 0.1)
    w_loss = st.slider("w_loss", 0.0, 1000.0, w.loss, 10.0)
    w_bw = st.slider("w_bandwidth", 0.0, 20.0, w.bandwidth, 0.5)
    w_stab = st.slider("w_stability (per flap)", 0.0, 100.0, w.stability, 5.0)
    hysteresis = st.slider("Hysteresis (min. improvement to switch)", 0.0, 0.5, 0.15, 0.01)
    hold_down = st.slider("Hold-down (s)", 0.0, 10.0, 3.0, 0.5)
weights = (w_lat, w_loss, w_bw, w_stab, w.ref_bw_mbps)

with st.sidebar.expander("Inject extra faults"):
    st.session_state.setdefault("faults", [])
    links = sorted(sc.topology().links)
    f_link = st.selectbox("Link", links)
    f_kind = st.selectbox("Fault", ["fail", "latency", "loss", "bandwidth", "background"])
    unit = {"latency": "ms", "loss": "probability", "bandwidth": "Mb/s", "background": "Mb/s"}.get(f_kind)
    default = {"latency": 100.0, "loss": 0.1, "bandwidth": 10.0, "background": 80.0}.get(f_kind, 0.0)
    f_value = st.number_input(f"Value ({unit})", value=default) if unit else 0.0
    f_t0, f_t1 = st.slider("Active from / to (s)", 0.0, sc.duration_s, (20.0, 30.0), 0.5)
    c1, c2 = st.columns(2)
    if c1.button("Add fault"):
        st.session_state.faults.append((f_link, f_kind, float(f_value), f_t0, f_t1))
    if c2.button("Clear all"):
        st.session_state.faults = []
    for spec in st.session_state.faults:
        st.caption(f"• {spec[0]} {spec[1]} {spec[2]:g} @ {spec[3]:g}–{spec[4]:g}s")
specs = tuple(st.session_state.faults)

# ------------------------------------------------------------------- main ----
st.title("Intelligent Routing Protocol")
st.caption("Adaptive multi-metric routing that reacts to latency, bandwidth, loss, congestion and failures.")

tab_live, tab_eval, tab_about = st.tabs(["Experiment", "Evaluation results", "How it works"])

with tab_live:
    with st.spinner("Simulating…"):
        results = {p: simulate_cached(scenario, p, int(seed), weights, hysteresis, hold_down, specs)
                   for p in (policy, baseline)}
    ours, base = results[policy], results[baseline]

    flows = sorted(ours["samples"].flow.unique())
    flow = st.radio("Flow", flows, horizontal=True, index=flows.index(sc.focus_flow) if sc.focus_flow in flows else 0)

    lower_better = {k for k, (_, hib) in METRICS.items() if not hib}
    show = ["delivery_ratio", "throughput_ratio", "mean_latency_ms", "route_changes", "degraded_s"]
    cols = st.columns(len(show))
    for col, key in zip(cols, show):
        if key not in ours["metrics"]:
            col.metric(METRICS[key][0], "n/a", help="Only defined for scenarios with a fault window.")
            continue
        a, b = ours["metrics"][key], base["metrics"][key]
        fmt = "{:.3f}" if "ratio" in key else "{:.1f}"
        col.metric(METRICS[key][0], fmt.format(a), delta=fmt.format(a - b) + f" vs {baseline}",
                   delta_color="off" if abs(a - b) < 1e-9 else "inverse" if key in lower_better else "normal")

    default_t = float(sc.fault_time + 5) if sc.fault_time is not None else 20.0
    t = st.slider("Time (s) — scrub to watch the routes change", 0.0, sc.duration_s - 0.1,
                  min(default_t, sc.duration_s - 0.1), 0.1)

    left, right = st.columns(2)
    left.plotly_chart(topology_figure(ours, t, flow, POLICY_LABEL[policy]), width="stretch")
    right.plotly_chart(topology_figure(base, t, flow, POLICY_LABEL[baseline]), width="stretch")

    events = ours["events"]
    g1, g2 = st.columns(2)
    g1.plotly_chart(timeseries_figure(results, flow, "goodput_mbps", f"{flow} goodput (Mb/s)", t, sc, events),
                    width="stretch")
    g2.plotly_chart(timeseries_figure(results, flow, "latency_ms", f"{flow} latency (ms)", t, sc, events),
                    width="stretch")

    st.subheader("Route changes")
    rc1, rc2 = st.columns(2)
    for col, p in ((rc1, policy), (rc2, baseline)):
        col.markdown(f"**{POLICY_LABEL[p]}**")
        col.dataframe(results[p]["changes"], hide_index=True, width="stretch")

    e1, e2 = st.columns([1, 2])
    e1.subheader("Events")
    e1.dataframe(pd.DataFrame(events, columns=["time (s)", "event"]), hide_index=True, width="stretch")
    e2.subheader(f"Link conditions at t = {t:.1f}s")
    lk = ours["links"]
    snap = lk[lk.t == lk.t[lk.t <= t].max()].copy()
    snap["status"] = [health(r, ours["network"].links[r.link].nominal.latency_ms) for r in snap.itertuples()]
    e2.dataframe(snap[["link", "status", "up", "latency_ms", "loss", "load_mbps", "capacity_mbps", "util",
                       "est_up", "est_loss"]].round(3), hide_index=True, width="stretch")

with tab_eval:
    res_dir = ROOT / "results"
    report = res_dir / "REPORT.md"
    c1, c2 = st.columns([1, 3])
    seeds = c1.number_input("Seeds per configuration", 2, 50, 10)
    if c1.button("Run full evaluation"):
        from irp.evaluation import evaluate
        with st.spinner(f"Running {len(SCENARIOS) * len(POLICY_ORDER) * seeds} simulations…"):
            evaluate(str(res_dir), int(seeds), verbose=False)
    if report.exists():
        text = report.read_text()
        body = text.split("## Figures")[0]
        st.markdown(body)
        for img in sorted(res_dir.glob("fig_*.png")):
            st.image(str(img))
        st.download_button("Download raw runs (CSV)", (res_dir / "raw_runs.csv").read_bytes(), "raw_runs.csv")
    else:
        st.info("No results yet — click **Run full evaluation** or run `python -m irp evaluate`.")

with tab_about:
    readme = ROOT / "README.md"
    st.markdown(readme.read_text() if readme.exists() else "See README.md")
