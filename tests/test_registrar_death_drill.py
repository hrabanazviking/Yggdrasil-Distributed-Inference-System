"""The registrar's death drill tests — Y-067's acceptance.

The slice must prove: (a) the drill law is data — a
policy naming any kill target but the first node
ever started, allowing a single registrar, or
expecting any effect beyond a missing heartbeat
fails validation; (b) the headline — a settled mesh
whose every node registered through the first node
kills that first node, and the mesh notices nothing
but a missing heartbeat: survivors age the dead
node present → suspect → gone on their own Y-054
trackers with missed beats counted, keep beating
each other present, keep registering, keep syncing,
admit a new node, and serve the dead registrar's own
entry whole to that new node — the report's verdict
is passed with entries_lost and
effects_beyond_missing_heartbeat both zero;
(c) honesty — an entry the registrar held that was
never replicated IS lost at the kill, counted, and
the verdict fails; (d) the kill is aimed by law —
killing a non-first node, a second kill, a kill
with nothing started, and a kill of an unknown node
are refused by name; (e) acts aimed at the dead node
are refused node_down and are not disruptions;
(f) drill acts are judged — duplicate starts, a full
mesh, unknown doors, self syncs, and registry
refusals are named and counted apart; (g) the
report, records, and events validate with honest
counts and no census content, and the founding
README is untouched.

Everything here is SIMULATED — a kill is the
in-process loss of one node's replica and tracker
objects; no process is killed, no socket is opened,
and no LAN is crossed (real carriage was Y-062's
own proof).
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from yggdrasil.observability import validate_event
from yggdrasil.registrar_death_drill import (
    DeathDrill,
    DeathDrillError,
    default_policy,
    validate_policy,
    validate_record,
    validate_report,
)
from yggdrasil.simulator import make_fake_census

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


def model(name="ygg-base-7b"):
    return {
        "name": name,
        "family": "fixture",
        "quants": [{"name": "Q4_K_M", "format": "gguf", "size_gb": 4.0}],
        "capabilities": ["generate", "stream"],
        "runnable_on": [],
    }


def drill(policy=None, clock=None):
    return DeathDrill(policy=policy, now=clock or Clock())


def settled(policy=None, clock=None):
    """Three nodes, each registered through the first
    node (alfheim, the registrar), fully synced, one
    heartbeat round heard by all."""
    found = drill(policy=policy, clock=clock)
    for node_id in ("alfheim", "brage", "solberg"):
        found.start_node(node_id)
    for node_id in ("alfheim", "brage", "solberg"):
        record = found.register(
            node_id,
            via="alfheim",
            census=make_fake_census(node_id, models=("ygg-base-7b",)),
            models=[model()],
        )
        assert record["verdict"] == "accepted"
    found.sync("alfheim", "brage")
    found.sync("alfheim", "solberg")
    found.sync("brage", "solberg")
    found.heartbeat_round()
    return found


# -- the law is data ----------------------------------------------


def test_policy_law_is_schema_constants():
    policy = default_policy()
    assert policy["kill_target"] == "first_started"
    assert policy["single_registrar_allowed"] is False
    assert policy["expected_effect"] == "missing_heartbeat_only"
    validate_policy(policy)
    with pytest.raises(DeathDrillError):
        validate_policy({**policy, "kill_target": "any_node"})
    with pytest.raises(DeathDrillError):
        validate_policy({**policy, "single_registrar_allowed": True})
    with pytest.raises(DeathDrillError):
        validate_policy({**policy, "expected_effect": "mesh_degraded"})
    with pytest.raises(DeathDrillError):
        validate_policy({**policy, "max_nodes": 1})


def test_the_first_node_ever_started_is_the_registrar():
    found = drill()
    assert found.report()["first_node_id"] is None
    assert found.report()["verdict"] == "incomplete"
    found.start_node("brage")
    found.start_node("alfheim")
    assert found.first_node_id == "brage"
    report = found.report()
    assert [n["node_id"] for n in report["nodes"]] == ["brage", "alfheim"]
    assert report["nodes"][0]["started_order"] == 1


# -- the headline: the mesh notices nothing but a missing heartbeat --


def test_the_registrars_death_drill_passes():
    clock = Clock()
    found = settled(clock=clock)
    record = found.kill("alfheim")
    validate_record(record)
    assert (record["verdict"], record["action"]) == ("accepted", "kill")
    assert found.status("alfheim") == "down"

    # The mesh keeps working: a new node joins after
    # the kill, registers through a survivor, and the
    # survivors sync among themselves.
    found.start_node("dain")
    joined = found.register(
        "dain",
        via="brage",
        census=make_fake_census("dain", models=("ygg-base-7b",)),
        models=[model()],
    )
    assert joined["verdict"] == "accepted"
    found.sync("brage", "solberg")
    found.sync("brage", "dain")
    found.sync("solberg", "dain")

    # The dead registrar's own entry is still served
    # whole — to the node that joined after its death.
    served = found.entry("alfheim", via="dain")
    assert served is not None
    assert served["census"]["node_id"] == "alfheim"
    assert [m["name"] for m in served["models"]] == ["ygg-base-7b"]
    assert served["origin_registry"] == "alfheim"

    # Time passes; the survivors keep beating each
    # other, and only the first node's beats are
    # missing.
    clock.advance(100)
    found.heartbeat_round()
    found.sweep()
    report = found.report()
    validate_report(report)
    assert report["verdict"] == "passed"
    assert report["first_node_id"] == "alfheim"
    assert report["kills"] == 1
    assert report["entries_at_kill"] == 3
    assert report["entries_lost"] == 0
    assert report["disruptions"] == 0
    assert report["effects_beyond_missing_heartbeat"] == 0
    assert report["registrations_after_kill"] == 1
    assert report["syncs_after_kill"] == 3
    assert report["joins_after_kill"] == 1
    assert report["lookups_after_kill"] >= 1
    standings = {n["node_id"]: n for n in report["nodes"]}
    assert standings["alfheim"]["status"] == "down"
    assert standings["alfheim"]["entries_held"] == 3  # what it held at the kill
    for survivor in ("brage", "solberg"):
        assert standings[survivor]["verdict_on_first"] == "gone"
        assert standings[survivor]["missed_beats_on_first"] >= 9
        assert standings[survivor]["entries_held"] == 4
    # dain joined after the kill: it never heard the
    # registrar beat, so its tracker holds no verdict
    # on it at all — an honest null, not a verdict —
    # while its registry replica holds the whole truth.
    assert standings["dain"]["verdict_on_first"] is None
    assert standings["dain"]["entries_held"] == 4
    # The survivors' verdicts on each other never moved.
    assert found.tracker("brage").liveness("solberg")["verdict"] == "present"
    assert found.tracker("brage").liveness("dain")["verdict"] == "present"


def test_the_only_notice_is_the_ageing_heartbeat():
    clock = Clock()
    found = settled(clock=clock)
    assert found.tracker("brage").liveness("alfheim")["verdict"] == "present"
    found.kill("alfheim")
    clock.advance(35)  # past suspect (30 s), short of gone (90 s)
    found.heartbeat_round()
    found.sweep()
    standing = found.tracker("brage").liveness("alfheim")
    assert standing["verdict"] == "suspect"
    assert standing["missed_beats"] == 3
    report = found.report()
    assert report["verdict"] == "incomplete"  # not yet gone, proofs not yet done
    assert report["entries_lost"] == 0
    assert report["effects_beyond_missing_heartbeat"] == 0
    # And the registry truth is untouched by the ageing:
    # a liveness verdict is not a withdrawal.
    assert found.entry("alfheim", via="brage") is not None
    assert found.entry("alfheim", via="solberg") is not None


# -- honesty: what was never replicated is lost, and the drill says so --


def test_an_unreplicated_entry_is_lost_and_the_drill_fails():
    found = drill()
    found.start_node("alfheim")
    found.start_node("brage")
    # brage registers through the registrar — and the
    # entry is never synced anywhere before the kill.
    found.register(
        "brage",
        via="alfheim",
        census=make_fake_census("brage"),
        models=[],
    )
    found.kill("alfheim")
    assert found.entry("brage", via="brage") is None  # brage's replica never held it
    report = found.report()
    assert report["entries_at_kill"] == 1
    assert report["entries_lost"] == 1
    assert report["effects_beyond_missing_heartbeat"] == 1
    assert report["verdict"] == "failed"


# -- the kill is aimed by law ---------------------------------------


def test_kill_refusals_are_named_and_counted():
    empty = drill()
    nobody = empty.kill("alfheim")
    assert nobody["reason"] == "no_first_node"
    found = settled()
    wrong = found.kill("brage")
    assert wrong["reason"] == "not_first_node"
    assert found.status("brage") == "up"  # untouched
    ghost = found.kill("never-joined")
    assert ghost["reason"] == "unknown_node"
    assert found.kill("alfheim")["verdict"] == "accepted"
    again = found.kill("alfheim")
    assert again["reason"] == "already_dead"
    report = found.report()
    assert report["kills"] == 1
    assert report["refusals_by_reason"] == {
        "already_dead": 1,
        "not_first_node": 1,
        "unknown_node": 1,
    }


def test_acts_aimed_at_the_dead_node_are_refused_and_are_not_disruptions():
    found = settled()
    found.kill("alfheim")
    via_dead = found.register(
        "brage",
        via="alfheim",
        census=make_fake_census("brage"),
        models=[],
    )
    assert via_dead["reason"] == "node_down"
    sync_dead = found.sync("alfheim", "brage")
    assert sync_dead["reason"] == "node_down"
    beats = found.heartbeat_round()  # the two survivors still beat each other
    assert beats["verdict"] == "accepted"
    with pytest.raises(DeathDrillError, match="node_down"):
        found.registry("alfheim")
    with pytest.raises(DeathDrillError, match="node_down"):
        found.entry("brage", via="alfheim")
    report = found.report()
    assert report["disruptions"] == 0
    assert report["effects_beyond_missing_heartbeat"] == 0
    assert report["registrations_after_kill"] == 0


# -- judged acts ------------------------------------------------------


def test_start_and_sync_refusals_are_named():
    tight = default_policy()
    tight["max_nodes"] = 2
    found = drill(policy=tight)
    found.start_node("alfheim")
    dup = found.start_node("alfheim")
    assert dup["reason"] == "duplicate_node"
    found.start_node("brage")
    full = found.start_node("solberg")
    assert full["reason"] == "mesh_full"
    own = found.sync("alfheim", "alfheim")
    assert own["reason"] == "self_sync"
    ghost = found.sync("alfheim", "never-joined")
    assert ghost["reason"] == "unknown_node"
    report = found.report()
    assert report["nodes_started"] == 2
    assert report["refusals_by_reason"] == {
        "duplicate_node": 1,
        "mesh_full": 1,
        "self_sync": 1,
        "unknown_node": 1,
    }


def test_register_refusals_are_named():
    found = drill()
    found.start_node("alfheim")
    found.start_node("brage")
    ghost_door = found.register(
        "brage", via="never-joined", census=make_fake_census("brage"), models=[]
    )
    assert ghost_door["reason"] == "unknown_node"
    ghost_node = found.register(
        "never-joined",
        via="alfheim",
        census=make_fake_census("never-joined"),
        models=[],
    )
    assert ghost_node["reason"] == "unknown_node"
    mismatched = found.register(
        "brage", via="alfheim", census=make_fake_census("solberg"), models=[]
    )
    assert mismatched["reason"] == "registry_refused"
    assert found.report()["registrations_accepted"] == 0


def test_a_heartbeat_round_with_no_living_pair_is_refused():
    found = drill()
    assert found.heartbeat_round()["reason"] == "no_peers"
    found.start_node("alfheim")
    assert found.heartbeat_round()["reason"] == "no_peers"
    found.start_node("brage")
    round_ = found.heartbeat_round()
    assert round_["verdict"] == "accepted"
    assert found.report()["heartbeats_sent"] == 2  # one each way


# -- counted state ------------------------------------------------------


def test_report_records_and_events_validate_with_honest_counts():
    clock = Clock()
    found = settled(clock=clock)
    found.kill("alfheim")
    found.start_node("dain")
    found.register(
        "dain", via="brage", census=make_fake_census("dain"), models=[model()]
    )
    found.sync("brage", "dain")
    clock.advance(100)
    found.heartbeat_round()
    found.sweep()
    report = found.report()
    validate_report(report)
    assert report["verdict"] == "passed"
    assert report["nodes_started"] == 4
    assert report["registrations_accepted"] == 4
    assert report["syncs"] == 4
    assert report["heartbeats_sent"] > 0
    for record in report["records"]:
        validate_record(record)
    for event in found.events:
        validate_event(event)
    kinds = [e["type"] for e in found.events]
    assert "death_drill.node_started" in kinds
    assert "death_drill.node_killed" in kinds
    # No census content rides any record or report.
    blob = json.dumps(report)
    assert "ram_gb" not in blob


def test_the_founding_readme_is_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
