"""Heartbeats and liveness tests — Y-054's acceptance.

The slice must prove: (a) the ageing policy is data with
its ordering law enforced — interval before the suspect
line before the gone line, lines that cross refused by
name on top of the schema; (b) a fresh heartbeat ages a
peer present, and silence on the observer's own clock
ages it suspect at the policy's suspect line and gone
at its gone line — never before, with age, missed
beats, and the policy lines carried in the record;
(c) freshness is by sequence — a newer beat refreshes, a
duplicate (same sequence) is counted apart and refreshes
nothing, a replayed older beat is refused stale and a
long-dead peer cannot be kept present by replays;
(d) a peer's own gone heartbeat ages it gone at once on
the self_reported_gone basis, and a newer present beat
is its return; (e) refusals are named and counted —
malformed beat, the observer's own heartbeat, a beat
claiming suspect (a peer's verdict, never a self-report),
and a full peer table; (f) sweeps record verdict
transitions as counted Y-013 events, plain queries
record nothing, and reports validate with the
present/suspect/gone counts of their moment;
(g) the node agent seam — an unwired agent judges no
peer and its status carries None, a wired agent hears,
ages, and reports through the tracker; (h) the founding
README is untouched.

Ageing here is judged in-process on an injected clock:
no socket is opened and no LAN is crossed, and nothing
here claims one was (real carriage is Y-062's).
"""

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from yggdrasil.health_endpoint import build_node_health
from yggdrasil.liveness import (
    LivenessError,
    LivenessTracker,
    default_policy,
    validate_policy,
    validate_record,
    validate_report,
)
from yggdrasil.node_agent import NodeAgent, NodeAgentError, NodeConfig
from yggdrasil.observability import validate_event

ROOT = Path(__file__).resolve().parents[1]
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
EPOCH = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
OBSERVER = "midgard"


class Clock:
    def __init__(self):
        self.moment = EPOCH

    def __call__(self):
        return self.moment

    def advance(self, seconds):
        self.moment += timedelta(seconds=seconds)


def beat(node_id="alfheim", seq=0, liveness="present", at=None):
    moment = at or EPOCH
    health = {
        "node_id": node_id,
        "at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
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


def tracker(clock=None, policy=None, observer=OBSERVER):
    return LivenessTracker(
        observer_node_id=observer, policy=policy, now=clock or Clock()
    )


# -- the policy's ordering law ------------------------------------


def test_default_policy_lines_in_order():
    policy = default_policy()
    assert policy["heartbeat_interval_seconds"] == 10
    assert policy["suspect_after_seconds"] == 30
    assert policy["gone_after_seconds"] == 90


def test_policy_crossed_lines_refused_by_name():
    policy = default_policy()
    with pytest.raises(LivenessError):
        validate_policy({**policy, "suspect_after_seconds": 90})
    with pytest.raises(LivenessError):
        validate_policy({**policy, "heartbeat_interval_seconds": 30})
    with pytest.raises(LivenessError):
        validate_policy({**policy, "gone_after_seconds": 0})


# -- fresh beats and ageing -----------------------------------------


def test_fresh_beat_ages_peer_present():
    clock = Clock()
    tr = tracker(clock)
    record = tr.observe(beat(seq=0))
    validate_record(record)
    assert record["verdict"] == "present"
    assert record["basis"] == "fresh"
    assert record["age_seconds"] == 0
    assert record["missed_beats"] == 0
    assert record["last_seq"] == 0
    assert record["simulated"] is True


def test_silence_ages_present_suspect_gone_on_the_observers_clock():
    clock = Clock()
    tr = tracker(clock)
    tr.observe(beat(seq=0))
    clock.advance(29)
    assert tr.liveness("alfheim")["verdict"] == "present"
    clock.advance(1)  # exactly the suspect line
    record = tr.liveness("alfheim")
    assert record["verdict"] == "suspect"
    assert record["basis"] == "silence"
    assert record["age_seconds"] == 30
    assert record["missed_beats"] == 3
    assert record["suspect_after_seconds"] == 30
    assert record["gone_after_seconds"] == 90
    clock.advance(59)
    assert tr.liveness("alfheim")["verdict"] == "suspect"
    clock.advance(1)  # exactly the gone line
    record = tr.liveness("alfheim")
    assert record["verdict"] == "gone"
    assert record["missed_beats"] == 9


def test_custom_policy_lines_age_by_its_own_numbers():
    clock = Clock()
    policy = {
        "version": 1,
        "heartbeat_interval_seconds": 5,
        "suspect_after_seconds": 12,
        "gone_after_seconds": 40,
        "max_peers": 4,
    }
    tr = tracker(clock, policy)
    tr.observe(beat(seq=0))
    clock.advance(12)
    assert tr.liveness("alfheim")["verdict"] == "suspect"
    clock.advance(28)
    assert tr.liveness("alfheim")["verdict"] == "gone"


def test_newer_beat_refreshes_a_suspect_peer():
    clock = Clock()
    tr = tracker(clock)
    tr.observe(beat(seq=0))
    clock.advance(45)
    assert tr.liveness("alfheim")["verdict"] == "suspect"
    record = tr.observe(beat(seq=1))
    assert record["verdict"] == "present"
    assert record["age_seconds"] == 0
    assert record["beats_accepted"] == 2


# -- freshness by sequence --------------------------------------------


def test_duplicate_beat_refreshes_nothing():
    clock = Clock()
    tr = tracker(clock)
    tr.observe(beat(seq=3))
    clock.advance(45)
    record = tr.observe(beat(seq=3))  # the same beat, replayed
    assert record["verdict"] == "suspect"
    assert record["age_seconds"] == 45
    report = tr.report()
    assert report["beats_duplicate"] == 1
    assert report["beats_accepted"] == 1


def test_stale_beat_refused_and_the_corpse_stays_gone():
    clock = Clock()
    tr = tracker(clock)
    tr.observe(beat(seq=7))
    clock.advance(120)
    assert tr.liveness("alfheim")["verdict"] == "gone"
    assert tr.observe(beat(seq=2)) is None
    assert tr.liveness("alfheim")["verdict"] == "gone"
    report = tr.report()
    assert report["refused_reasons"] == {"stale_sequence": 1}


# -- the graceful goodbye and the return --------------------------------


def test_own_gone_beat_ages_gone_at_once_and_return_revives():
    clock = Clock()
    tr = tracker(clock)
    tr.observe(beat(seq=0))
    record = tr.observe(beat(seq=1, liveness="gone"))
    assert record["verdict"] == "gone"
    assert record["basis"] == "self_reported_gone"
    assert record["age_seconds"] == 0
    clock.advance(5)
    assert tr.liveness("alfheim")["verdict"] == "gone"
    record = tr.observe(beat(seq=2))
    assert record["verdict"] == "present"
    assert record["basis"] == "fresh"


# -- refusals named and counted -------------------------------------------


def test_refusals_are_named_counted_and_evented():
    clock = Clock()
    tr = tracker(clock)
    assert tr.observe({"node_id": "alfheim"}) is None  # malformed
    assert tr.observe(beat(node_id=OBSERVER, seq=0)) is None  # own
    suspect = beat(seq=0)
    suspect["liveness"] = "suspect"
    suspect["health"]["liveness"] = "suspect"
    assert tr.observe(suspect) is None  # suspect self-report
    report = tr.report()
    assert report["beats_refused"] == 3
    assert report["refused_reasons"] == {
        "malformed_beat": 1,
        "own_heartbeat": 1,
        "suspect_self_report": 1,
    }
    refused = [e for e in tr.events if e["type"] == "liveness.beat_refused"]
    assert len(refused) == 3
    for event in refused:
        validate_event(event)
        assert event["source"]["component"] == "fabric"


def test_peer_table_full_refuses_by_name():
    clock = Clock()
    policy = {**default_policy(), "max_peers": 1}
    tr = tracker(clock, policy)
    tr.observe(beat(node_id="alfheim", seq=0))
    assert tr.observe(beat(node_id="jotunheim", seq=0)) is None
    assert tr.report()["refused_reasons"] == {"peer_table_full": 1}
    # a tracked peer still beats freely at the bound
    assert tr.observe(beat(node_id="alfheim", seq=1)) is not None


def test_unknown_peer_is_none_never_a_verdict():
    tr = tracker()
    assert tr.liveness("never-heard") is None
    assert tr.peers() == []


# -- sweeps, transitions, reports -------------------------------------------


def test_sweep_records_transitions_queries_record_nothing():
    clock = Clock()
    tr = tracker(clock)
    tr.observe(beat(seq=0))
    clock.advance(45)
    tr.liveness("alfheim")  # a plain query records nothing
    assert [e["type"] for e in tr.events] == []
    records = tr.sweep()
    assert records[0]["verdict"] == "suspect"
    changed = [e for e in tr.events if e["type"] == "liveness.verdict_changed"]
    assert len(changed) == 1
    assert changed[0]["payload"]["from_verdict"] == "present"
    assert changed[0]["payload"]["to_verdict"] == "suspect"
    validate_event(changed[0])
    clock.advance(60)
    tr.sweep()
    report = tr.report()
    assert report["transitions"] == 2
    assert report["gone_count"] == 1


def test_report_counts_the_moment():
    clock = Clock()
    tr = tracker(clock)
    tr.observe(beat(node_id="alfheim", seq=0))
    tr.observe(beat(node_id="jotunheim", seq=0))
    clock.advance(45)  # both silent into suspect
    tr.observe(beat(node_id="jotunheim", seq=1))  # jotunheim returns fresh
    report = tr.report()
    validate_report(report)
    assert report["present_count"] == 1
    assert report["suspect_count"] == 1
    assert report["gone_count"] == 0
    assert report["beats_accepted"] == 3
    assert report["observer_node_id"] == OBSERVER
    assert report["simulated"] is True


def test_replay_determinism():
    def run():
        clock = Clock()
        tr = tracker(clock)
        tr.observe(beat(seq=0))
        clock.advance(31)
        swept = tr.sweep()
        tr.observe(beat(seq=1))
        clock.advance(95)
        return tr.report(), swept

    first, first_swept = run()
    second, second_swept = run()
    assert first == second
    assert first_swept == second_swept


# -- the node agent seam -----------------------------------------------------


def _agent(tmp_path, **kwargs):
    config = NodeConfig.from_dict(
        {
            "version": 1,
            "node_id": OBSERVER,
            "device_class": "vm",
            "key_store_dir": str(tmp_path / "keys"),
        }
    )
    return NodeAgent(config, **kwargs)


def test_agent_unwired_judges_no_peer(tmp_path):
    agent = _agent(tmp_path)
    assert agent.liveness_report() is None
    assert agent.peer_liveness("alfheim") is None
    assert agent.status()["liveness"] is None
    with pytest.raises(NodeAgentError):
        agent.observe_peer_heartbeat(beat(seq=0))
    with pytest.raises(NodeAgentError):
        agent.sweep_liveness()


def test_agent_wired_hears_ages_and_reports(tmp_path):
    clock = Clock()
    tr = tracker(clock)
    agent = _agent(tmp_path, liveness_tracker=tr)
    record = agent.observe_peer_heartbeat(beat(seq=0))
    assert record["verdict"] == "present"
    clock.advance(100)
    records = agent.sweep_liveness()
    assert records[0]["verdict"] == "gone"
    assert agent.peer_liveness("alfheim")["verdict"] == "gone"
    status = agent.status()
    assert status["liveness"]["gone_count"] == 1
    assert agent.liveness_tracker is tr


# -- the founding document -----------------------------------------------------


def test_readme_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
