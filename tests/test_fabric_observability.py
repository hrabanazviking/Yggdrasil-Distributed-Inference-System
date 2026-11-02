"""Fabric observability tests — Y-064's acceptance.

The slice must prove: (a) the watch is data — a
silent door or an unbounded staleness fail
validation by name; (b) true observations land and
the latest word from a node replaces its older one;
(c) counts that cannot be true are refused by name
— delivered plus dropped past sent, and drop reasons
that do not sum to the drops — counted apart, stored
nowhere; (d) rates and drop rates are computed from
counts and windows, per node and for the mesh, never
taken on a node's word; (e) staleness is judged
against the policy at report time and stale nodes
are named; (f) alerts are named — drop_rate_high,
nodes_not_converged, stale_observations, and an
empty door says no_observations instead of health;
(g) real component reports feed the door — a WAN
report's crossings become per-node observations, a
gossip report sets the convergence standing;
(h) reports, observations, and events validate, no
payload content rides any record, and the founding
README is untouched.

Everything here is SIMULATED — the door aggregates
in-process reports; no socket is opened and no LAN
is crossed (real carriage was Y-062's own proof).
"""

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from yggdrasil.fabric import Fabric
from yggdrasil.fabric_observability import (
    FabricObservabilityError,
    FabricObserver,
    default_policy,
    validate_observation,
    validate_policy,
    validate_report,
)
from yggdrasil.gossip import GossipMesh
from yggdrasil.gossip import default_policy as gossip_policy
from yggdrasil.observability import validate_event
from yggdrasil.wan_simulation import WanSimulation, named_profile

ROOT = Path(__file__).resolve().parents[1]
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
EPOCH = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.moment = EPOCH

    def __call__(self):
        return self.moment

    def advance(self, seconds):
        self.moment += timedelta(seconds=seconds)


def fmt(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def observation(node_id="alfheim", *, at=None, **overrides):
    record = {
        "version": 1,
        "node_id": node_id,
        "at": fmt(at or EPOCH),
        "window_seconds": 10,
        "sent": 100,
        "delivered": 95,
        "dropped": 5,
        "dropped_reasons": {"lost_after_retries": 5},
        "throttled": 2,
        "gossip_rounds": 3,
        "converged": True,
        "registry_entries": 7,
        "simulated": True,
    }
    record.update(overrides)
    return record


# -- the watch is data ---------------------------------------


def test_policy_is_data_and_a_silent_door_fails():
    policy = default_policy()
    assert validate_policy(policy) == policy
    assert policy["alerts_enabled"] is True
    with pytest.raises(FabricObservabilityError):
        validate_policy({**policy, "alerts_enabled": False})
    with pytest.raises(FabricObservabilityError):
        validate_policy({**policy, "stale_after_seconds": 0})
    with pytest.raises(FabricObservabilityError):
        validate_policy({**policy, "max_drop_rate": 1.5})
    with pytest.raises(FabricObservabilityError):
        validate_observation(observation(simulated=False))


# -- intake ---------------------------------------------------


def test_observation_lands_and_latest_word_replaces():
    door = FabricObserver(now=Clock())
    door.observe(observation(sent=100, delivered=95, dropped=5))
    door.observe(observation(sent=40, delivered=40, dropped=0, dropped_reasons={}))
    report = door.report()
    assert validate_report(report) == report
    assert report["observations"] == 2
    assert len(report["nodes"]) == 1
    assert report["sent"] == 40
    assert report["nodes"][0]["sent"] == 40


def test_impossible_counts_refused_by_name_and_counted_apart():
    door = FabricObserver(now=Clock())
    with pytest.raises(FabricObservabilityError, match="counts_inconsistent"):
        door.observe(observation(delivered=96, dropped=5))
    with pytest.raises(FabricObservabilityError, match="reasons_incomplete"):
        door.observe(observation(dropped_reasons={"lost_after_retries": 4}))
    with pytest.raises(FabricObservabilityError):
        door.observe(observation(node_id="BAD NODE"))
    report = door.report()
    assert report["refused_observations"] == 3
    assert report["nodes"] == []
    assert report["alerts"] == ["no_observations"]


# -- computed rates --------------------------------------------


def test_rates_and_drop_rates_are_computed_not_reported():
    door = FabricObserver(now=Clock())
    door.observe(observation("alfheim", sent=100, delivered=95, dropped=5))
    door.observe(
        observation(
            "brage",
            sent=50,
            delivered=50,
            dropped=0,
            dropped_reasons={},
            window_seconds=25,
        )
    )
    report = door.report()
    by_node = {n["node_id"]: n for n in report["nodes"]}
    assert by_node["alfheim"]["sent_per_second"] == 10.0
    assert by_node["alfheim"]["drop_rate"] == 0.05
    assert by_node["brage"]["sent_per_second"] == 2.0
    assert by_node["brage"]["drop_rate"] == 0.0
    assert report["sent_per_second"] == 12.0
    assert report["sent"] == 150
    assert report["delivered"] == 145
    assert report["dropped"] == 5
    assert report["throttled"] == 4
    assert report["dropped_reasons"] == {"lost_after_retries": 5}
    assert report["drop_rate"] == round(5 / 150, 6)
    # an idle node reports a zero rate, never a division error
    door.observe(observation("celia", sent=0, delivered=0, dropped=0, dropped_reasons={}))
    idle = {n["node_id"]: n for n in door.report()["nodes"]}["celia"]
    assert idle["sent_per_second"] == 0.0
    assert idle["drop_rate"] == 0.0


# -- staleness and alerts ---------------------------------------


def test_staleness_is_judged_at_report_time_and_names_nodes():
    clock = Clock()
    door = FabricObserver(now=clock)
    door.observe(observation("alfheim"))
    assert door.report()["nodes"][0]["stale"] is False
    clock.advance(default_policy()["stale_after_seconds"] + 1)
    report = door.report()
    assert report["nodes"][0]["stale"] is True
    assert report["nodes"][0]["alerts"] == ["stale"]
    assert report["stale_nodes"] == ["alfheim"]
    assert "stale_observations" in report["alerts"]


def test_alerts_are_named_for_drops_and_non_convergence():
    door = FabricObserver(now=Clock())
    door.observe(
        observation(
            "alfheim",
            sent=100,
            delivered=80,
            dropped=20,
            dropped_reasons={"lost_after_retries": 20},
            converged=False,
        )
    )
    report = door.report()
    node = report["nodes"][0]
    assert node["alerts"] == ["drop_rate_high", "not_converged"]
    assert report["alerts"] == ["drop_rate_high", "nodes_not_converged"]
    assert report["all_converged"] is False
    assert report["converged_nodes"] == 0
    assert report["reporting_nodes"] == 1


def test_converged_mesh_reports_all_converged():
    door = FabricObserver(now=Clock())
    door.observe(observation("alfheim"))
    door.observe(observation("brage"))
    report = door.report()
    assert report["all_converged"] is True
    assert report["converged_nodes"] == 2
    assert report["alerts"] == []


# -- real reports feed the door ----------------------------------


def test_wan_report_crossings_become_per_node_observations():
    fabric = Fabric(now=lambda: EPOCH)
    fabric.attach("alfheim")
    messages = [fabric.publish("alfheim", "registry.updates", {"n": i}) for i in range(6)]
    sim = WanSimulation(named_profile("lan"), seed=3, now=lambda: EPOCH)
    for message in messages:
        sim.send(message, "brage")
    door = FabricObserver(now=Clock())
    taken = door.observe_wan_report(sim.report(), window_seconds=10)
    assert [row["node_id"] for row in taken] == ["alfheim"]
    report = door.report()
    node = report["nodes"][0]
    assert node["sent"] == 6
    assert node["delivered"] == 6
    assert node["dropped"] == 0
    assert node["sent_per_second"] == 0.6
    assert node["converged"] is None
    assert report["all_converged"] is None
    assert report["convergence"]["reported"] is False


def test_wan_report_drops_are_counted_apart_by_reason():
    fabric = Fabric(now=lambda: EPOCH)
    fabric.attach("alfheim")
    messages = [fabric.publish("alfheim", "registry.updates", {"n": i}) for i in range(4)]
    profile = {
        "version": 1,
        "name": "dead_air",
        "base_latency_ms": 1.0,
        "jitter_ms": 0.0,
        "loss_rate": 1.0,
        "max_attempts": 2,
        "retry_backoff_ms": 1.0,
        "simulated": True,
    }
    sim = WanSimulation(profile, seed=1, now=lambda: EPOCH)
    for message in messages:
        sim.send(message, "brage")
    door = FabricObserver(now=Clock())
    door.observe_wan_report(sim.report(), window_seconds=10)
    report = door.report()
    assert report["dropped"] == 4
    assert report["dropped_reasons"] == {"lost_after_retries": 4}
    assert report["nodes"][0]["drop_rate"] == 1.0
    assert "drop_rate_high" in report["alerts"]


def test_gossip_report_sets_the_convergence_standing():
    mesh = GossipMesh(policy=gossip_policy(), now=lambda: EPOCH)
    for node_id in ("alfheim", "brage", "celia"):
        mesh.add_node(node_id)
    mesh.run_round()
    door = FabricObserver(now=Clock())
    standing = door.observe_gossip_report(mesh.report())
    assert standing["reported"] is True
    assert standing["node_count"] == 3
    report = door.report()
    assert validate_report(report) == report
    assert report["convergence"]["reported"] is True
    assert report["convergence"]["rounds_run"] == standing["rounds_run"]


# -- records hygiene ----------------------------------------------


def test_events_validate_and_no_payload_rides():
    door = FabricObserver(now=Clock())
    door.observe(observation())
    assert door.events
    for event in door.events:
        assert validate_event(event) == event
    report = door.report()
    assert "payload" not in str(report).lower()
    assert report["simulated"] is True


def test_readme_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
