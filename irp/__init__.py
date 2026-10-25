"""Intelligent Routing Protocol: adaptive multi-metric routing with a simulator."""
from .engine import Flow, RouteChange, RoutingEngine
from .metrics import summarize
from .policies import POLICIES, AdaptivePolicy, AdaptiveWeights, make_policy
from .scenarios import SCENARIOS, Scenario
from .simulator import Event, SimConfig, SimResult, Simulation, degrade, fail, restore, simulate
from .topology import Network


def run_scenario(name: str, policy: str = "adaptive", seed: int = 0, policy_kwargs=None,
                 extra_events=None, **config) -> SimResult:
    scenario = SCENARIOS[name]
    net, flows, events = scenario.build(seed)
    config.setdefault("duration_s", scenario.duration_s)
    return simulate(net, flows, make_policy(policy, **(policy_kwargs or {})),
                    events + list(extra_events or []), seed=seed, **config)
