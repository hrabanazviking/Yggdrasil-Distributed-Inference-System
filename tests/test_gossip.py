"""Gossip of state tests — Y-055's acceptance.

The slice must prove: (a) registry and health state
propagate epidemically — a registration and a heartbeat
originating at one node reach every node through
fanout-limited anti-entropy rounds, applied by the
receiving stores' own Y-053/Y-054 merge rules;
(b) convergence is measured, not hoped — at 3, 10, and
30 simulated nodes the measurement reports rounds,
virtual seconds, and message overhead, honestly
labelled SIMULATED; (c) the fanout bound is respected
per node per round and counted; (d) a partitioned
subset does not receive the fact, and converges after
the heal; (e) duplicate and stale gossip is harmless —
a replayed beat refreshes nothing, a stale beat
displaces nothing, and a withdrawal tombstone gossiped
across a heal deletes a stale entry still held on the
far side and blocks its resurrection; (f) refusals are
named, counted, and evented (partitioned exchanges,
malformed beats), and a node cannot originate another
node's heartbeat; (g) reports, measurements, and events
validate; (h) the founding README is untouched.

Everything here is in-process on a virtual clock: no
socket is opened and no LAN is crossed, and nothing
here claims one was (real carriage is Y-062's).
"""

import hashlib
from pathlib import Path

import pytest

from yggdrasil.gossip import (
    GossipError,
    GossipMesh,
    VirtualClock,
    default_policy,
    measure_convergence,
    validate_convergence,
    validate_policy,
    validate_report,
)
from yggdrasil.health_endpoint import build_node_health
from yggdrasil.observability import validate_event
from yggdrasil.registry_service import build_registration
from yggdrasil.simulator import make_fake_census

ROOT = Path(__file__).resolve().parents[1]
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
NAMES = ("alfheim", "jotunheim", "midgard")


def model(name="fixture-7b"):
    return {
        "name": name,
        "family": "fixture",
        "quants": [{"name": "Q4_K_M", "format": "gguf", "size_gb": 4.0}],
        "capabilities": ["generate", "stream"],
        "runnable_on": [],
    }


def beat(node_id, seq=0, clock=None, liveness="present"):
    clock = clock or VirtualClock()
    health = {
        "node_id": node_id,
        "at": clock().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "liveness": liveness,
        "queue_depth": 0,
        "models_loaded": [],
    }
    return build_node_health(
        node_id=node_id,
        seq=seq,
        lifecycle_state="joined" if liveness == "present" else "stopped",
        health=health,
        load={
            "in_flight": 0,
            "queue_depth": 0,
            "active_tasks": 0,
            "executions": 0,
            "thermal_c": None,
            "memory_fraction": None,
            "cpu_share_used": None,
        },
        models={"loaded": [], "present": None, "loader_simulated": None},
        governor_state=None,
        census={"measured": False, "measured_at": None, "fingerprint": None},
        uptime_seconds=1.0 if liveness == "present" else None,
    )


def mesh(names=NAMES, policy=None):
    clock = VirtualClock()
    m = GossipMesh(policy=policy, now=clock)
    for name in names:
        m.add_node(name)
    return m, clock


def register_at(m, holder, subject=None):
    subject = subject or holder
    census = make_fake_census(subject, models=("fixture-7b",))
    return m.node(holder).register_local(census, [model()])


# -- the policy is data -------------------------------------------------


def test_default_policy_is_the_benchmarks_gossip():
    policy = default_policy()
    assert policy["fanout"] == 3
    assert policy["round_interval_seconds"] == 5  # Y-049's anti-entropy round
    with pytest.raises(GossipError):
        validate_policy({**policy, "fanout": 0})
    with pytest.raises(GossipError):
        validate_policy({**policy, "round_interval_seconds": 0})


def test_convergence_shape_refuses_an_unlabelled_measurement():
    record = measure_convergence(node_counts=(3,))[0]
    validate_convergence(record)
    assert record["simulated"] is True
    with pytest.raises(GossipError):
        validate_convergence({**record, "simulated": False})


# -- epidemic reach -------------------------------------------------------


def test_registration_originating_at_one_node_reaches_all():
    m, _clock = mesh()
    register_at(m, "alfheim")
    assert m.node("midgard").registry.entry("alfheim") is None
    assert m.run_until_converged(max_rounds=10) is True
    for name in NAMES:
        entry = m.node(name).registry.entry("alfheim")
        assert entry is not None
        assert [x["name"] for x in entry["models"]] == ["fixture-7b"]


def test_heartbeat_originating_at_one_node_reaches_all_trackers():
    m, clock = mesh()
    m.node("alfheim").publish_beat(beat("alfheim", clock=clock))
    assert m.run_until_converged(max_rounds=10) is True
    for name in ("jotunheim", "midgard"):
        record = m.node(name).liveness.liveness("alfheim")
        assert record["verdict"] == "present"
        assert record["last_seq"] == 0
        assert m.node(name).beat_for("alfheim")["seq"] == 0


def test_extra_rounds_after_convergence_change_nothing():
    m, clock = mesh()
    register_at(m, "alfheim")
    m.node("jotunheim").publish_beat(beat("jotunheim", clock=clock))
    assert m.run_until_converged(max_rounds=10) is True
    before = {n: m.node(n).registry.entries() for n in NAMES}
    m.run_round()
    assert m.converged() is True
    assert {n: m.node(n).registry.entries() for n in NAMES} == before


# -- fanout discipline ------------------------------------------------------


def test_fanout_bound_respected_per_node_per_round():
    policy = {**default_policy(), "fanout": 2}
    names = tuple(f"node-{i:02d}" for i in range(10))
    m, _clock = mesh(names=names, policy=policy)
    register_at(m, "node-00", subject="node-00")
    for _ in range(3):
        summary = m.run_round()
        assert summary["max_initiated"] <= 2
    report = m.report()
    assert report["max_fanout_used"] == 2
    assert m.converged() is True


# -- partition and heal -------------------------------------------------------


def test_partitioned_subset_converges_only_after_heal():
    m, clock = mesh()
    m.cut(["midgard"])
    register_at(m, "alfheim")
    m.node("alfheim").publish_beat(beat("alfheim", clock=clock))
    assert m.run_until_converged(max_rounds=3) is False
    assert m.node("midgard").registry.entry("alfheim") is None
    assert m.node("midgard").beat_for("alfheim") is None
    assert m.node("jotunheim").registry.entry("alfheim") is not None
    m.heal()
    assert m.run_until_converged(max_rounds=10) is True
    assert m.node("midgard").registry.entry("alfheim") is not None
    assert m.node("midgard").liveness.liveness("alfheim")["verdict"] == "present"


def test_exchange_across_a_partition_is_refused_by_name():
    m, _clock = mesh()
    m.cut(["midgard"])
    assert m.exchange("alfheim", "midgard") is False
    report = m.report()
    assert report["exchanges_refused"] == 1
    assert report["refused_reasons"] == {"partitioned_peer": 1}
    refused = [e for e in m.events if e["type"] == "gossip.exchange_refused"]
    assert len(refused) == 1
    validate_event(refused[0])


# -- duplicate and stale gossip is harmless ------------------------------------


def test_duplicate_beat_refreshes_nothing():
    m, clock = mesh()
    node = m.node("jotunheim")
    original = beat("alfheim", seq=4, clock=clock)
    assert node.receive_beat(original) is True
    clock.advance(45)
    assert node.receive_beat(original) is False  # the same beat, replayed
    record = node.liveness.liveness("alfheim")
    assert record["age_seconds"] == 45  # aged from the first hearing only
    assert node.beats_duplicate == 1
    assert m.report()["beats_duplicate"] == 1


def test_stale_beat_displaces_nothing():
    m, clock = mesh()
    node = m.node("jotunheim")
    assert node.receive_beat(beat("alfheim", seq=7, clock=clock)) is True
    assert node.receive_beat(beat("alfheim", seq=2, clock=clock)) is False
    assert node.beat_for("alfheim")["seq"] == 7
    assert node.liveness.liveness("alfheim")["last_seq"] == 7
    assert node.beats_stale == 1
    assert m.report()["refused_reasons"] == {"stale_sequence": 1}


def test_withdrawal_tombstone_gossips_and_deletes_a_stale_entry_after_heal():
    m, _clock = mesh()
    register_at(m, "alfheim")
    assert m.run_until_converged(max_rounds=10) is True
    m.cut(["midgard"])
    assert m.node("alfheim").withdraw("alfheim", seq=2) is True
    m.run_round()
    assert m.node("jotunheim").registry.entry("alfheim") is None
    assert m.node("midgard").registry.entry("alfheim") is not None  # stale, held
    m.heal()
    assert m.run_until_converged(max_rounds=10) is True
    for name in NAMES:
        assert m.node(name).registry.entry("alfheim") is None
        assert m.node(name).registry.tombstones() == {"alfheim": 2}


def test_tombstone_blocks_resurrection_by_the_old_registration():
    m, clock = mesh()
    census = make_fake_census("alfheim", models=("fixture-7b",))
    old = build_registration("alfheim", seq=1, census=census, models=[model()], at=clock())
    m.node("alfheim").register(old)
    assert m.run_until_converged(max_rounds=10) is True
    m.node("jotunheim").withdraw("alfheim", seq=2)
    assert m.run_until_converged(max_rounds=10) is True
    assert m.node("midgard").register(old) is None
    assert m.node("midgard").registry.report()["refused_reasons"] == {
        "stale_sequence": 1
    }


# -- refusals at the doors -------------------------------------------------------


def test_malformed_beat_and_foreign_origin_refused():
    m, clock = mesh()
    node = m.node("alfheim")
    assert node.receive_beat({"node_id": "jotunheim"}) is False
    assert node.refused_reasons == {"malformed_beat": 1}
    with pytest.raises(GossipError):
        node.publish_beat(beat("jotunheim", clock=clock))
    with pytest.raises(GossipError):
        m.exchange("alfheim", "alfheim")
    with pytest.raises(GossipError):
        m.node("ghost")


def test_mesh_bound_and_duplicate_node_refused_by_name():
    policy = {**default_policy(), "max_nodes": 2}
    clock = VirtualClock()
    m = GossipMesh(policy=policy, now=clock)
    m.add_node("alfheim")
    with pytest.raises(GossipError):
        m.add_node("alfheim")
    m.add_node("jotunheim")
    with pytest.raises(GossipError):
        m.add_node("midgard")


# -- reports, events, determinism ---------------------------------------------------


def test_report_counts_the_mesh_and_events_validate():
    m, clock = mesh()
    register_at(m, "alfheim")
    m.node("alfheim").publish_beat(beat("alfheim", clock=clock))
    assert m.run_until_converged(max_rounds=10) is True
    report = m.report()
    validate_report(report)
    assert report["converged"] is True
    assert report["simulated"] is True
    assert report["messages_exchanged"] == 2 * report["exchanges"]
    assert report["elapsed_seconds"] == report["rounds_run"] * 5
    assert all(n["entry_count"] == 1 for n in report["nodes"])
    assert all(n["beats_held"] == 1 for n in report["nodes"])
    types = [e["type"] for e in m.events]
    assert "gossip.round_completed" in types
    assert "gossip.converged" in types
    for event in m.events:
        validate_event(event)
        assert event["source"]["component"] == "fabric"


def test_replay_is_deterministic():
    def run():
        m, clock = mesh()
        register_at(m, "alfheim")
        m.node("midgard").publish_beat(beat("midgard", clock=clock))
        m.run_until_converged(max_rounds=10)
        return m.report()

    assert run() == run()


# -- the roadmap's measurement: 3, 10, and 30 nodes -------------------------------


def test_convergence_measured_at_3_10_and_30_nodes():
    measurements = measure_convergence()
    assert [r["node_count"] for r in measurements] == [3, 10, 30]
    for record in measurements:
        validate_convergence(record)
        assert record["converged"] is True
        assert record["simulated"] is True
        assert record["registry_entries_held"] == record["node_count"]
        assert record["health_subjects"] == record["node_count"]
        assert record["rounds"] >= 1
        assert record["elapsed_seconds"] == (
            record["rounds"] * record["round_interval_seconds"]
        )
        assert record["messages_exchanged"] == 2 * record["exchanges"]
        assert record["fanout"] == 3


# -- the founding document ----------------------------------------------------------


def test_readme_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
