"""Clock discipline tests — Y-068's acceptance.

The slice must prove: (a) the discipline is law as
data — a policy trusting a single clock, judging by
anything but the median, corroborating with fewer
than two observers, or whose trusted line crosses
its severe line fails validation; (b) the headline —
a node's skew is the median of at least three
observers' measurements, a trusted node's timestamp
enters temporal logic only corrected by that median,
and a skewed node's likewise; (c) no single clock is
ever trusted — one observer's report, however
plausible, leaves a node uncorroborated and its
timestamps refused, and one observer flooding samples
still casts only one (latest) vote; (d) the median
resists outliers — one wild vote cannot drag a
corroborated standing the way a mean would be
dragged; (e) a severe node's timestamps are refused
severely_skewed by name; (f) freshness is by sequence
per observer — stale and duplicate samples are
refused by name and refresh nothing; (g) judged acts
— duplicate and full enrolments, unknown doors,
self-observation, malformed times and beats — are
named and counted apart; (h) a validated Y-030
heartbeat feeds the watch without rewiring; (i) the
mesh consensus offset is the median of corroborated
non-severe nodes only; (j) the report, records, and
events validate with honest counts, and the founding
README is untouched.

Everything here is SIMULATED — times are reported
values judged in-process; no clock is read over a
LAN and no node's clock is set.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from yggdrasil.clock_discipline import (
    ClockError,
    ClockWatch,
    default_policy,
    validate_policy,
    validate_record,
    validate_report,
)
from yggdrasil.health_endpoint import build_node_health
from yggdrasil.observability import validate_event

ROOT = Path(__file__).resolve().parents[1]
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
EPOCH = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)
OBSERVERS = ("brage", "solberg", "dain")


class Clock:
    def __init__(self):
        self.moment = EPOCH

    def __call__(self):
        return self.moment


def fmt(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def watch(policy=None, nodes=("alfheim",) + OBSERVERS):
    found = ClockWatch(policy=policy, now=Clock())
    for node_id in nodes:
        assert found.enrol(node_id)["verdict"] == "accepted"
    return found


def report_skew(found, node_id, skew, observers=OBSERVERS, seq=1):
    """Every named observer measures the same skew for
    one node: it claimed EPOCH+skew, each observer's
    own clock read EPOCH when the claim was heard."""
    records = []
    for observer in observers:
        records.append(
            found.observe(
                observer, node_id,
                fmt(EPOCH + timedelta(seconds=skew)), fmt(EPOCH), seq,
            )
        )
    return records


def beat(node_id="alfheim", seq=0, at=None):
    moment = at or EPOCH
    health = {
        "node_id": node_id,
        "at": fmt(moment),
        "liveness": "present",
        "queue_depth": 0,
        "models_loaded": [],
    }
    return build_node_health(
        node_id=node_id,
        seq=seq,
        lifecycle_state="joined",
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
        uptime_seconds=1.0,
    )


# -- the law is data ----------------------------------------------


def test_policy_law_is_schema_constants():
    policy = default_policy()
    assert policy["single_clock_trusted"] is False
    assert policy["consensus_method"] == "median"
    assert policy["min_observers"] == 3
    validate_policy(policy)
    with pytest.raises(ClockError):
        validate_policy({**policy, "single_clock_trusted": True})
    with pytest.raises(ClockError):
        validate_policy({**policy, "consensus_method": "mean"})
    with pytest.raises(ClockError):
        validate_policy({**policy, "min_observers": 1})
    with pytest.raises(ClockError):
        validate_policy({**policy, "severe_skew_seconds": 5})
    with pytest.raises(ClockError):
        validate_policy({**policy, "max_trusted_skew_seconds": 60})


# -- the headline: corroborated skew, corrected timestamps ----------


def test_a_trusted_nodes_timestamp_is_corrected_by_the_median():
    found = watch()
    for record in report_skew(found, "alfheim", 2):
        validate_record(record)
        assert record["verdict"] == "accepted"
        assert record["skew_seconds"] == 2
    standing = found.standing("alfheim")
    assert standing["verdict"] == "trusted"
    assert standing["trusted"] is True
    assert standing["median_skew_seconds"] == 2
    assert standing["observers"] == 3
    judged = found.judge("alfheim", fmt(EPOCH + timedelta(hours=1)))
    assert judged["verdict"] == "accepted"
    assert judged["corrected_at"] == fmt(EPOCH + timedelta(hours=1, seconds=-2))
    report = found.report()
    assert report["judgements"] == 1
    assert report["judgements_corrected"] == 1
    assert report["trusted_nodes"] == 1


def test_a_skewed_nodes_timestamp_is_corrected_not_believed():
    found = watch()
    report_skew(found, "alfheim", 30)
    standing = found.standing("alfheim")
    assert standing["verdict"] == "skewed"
    assert standing["trusted"] is False
    judged = found.judge("alfheim", fmt(EPOCH + timedelta(hours=2)))
    assert judged["verdict"] == "accepted"
    assert judged["skew_seconds"] == 30
    assert judged["corrected_at"] == fmt(EPOCH + timedelta(hours=2, seconds=-30))
    assert found.report()["skewed_nodes"] == 1


def test_a_behind_clock_is_corrected_forwards():
    found = watch()
    report_skew(found, "alfheim", -4)
    assert found.standing("alfheim")["median_skew_seconds"] == -4
    judged = found.judge("alfheim", fmt(EPOCH))
    assert judged["corrected_at"] == fmt(EPOCH + timedelta(seconds=4))


# -- no single clock is ever trusted --------------------------------


def test_one_observer_cannot_corroborate_a_node():
    found = watch()
    report_skew(found, "alfheim", 1, observers=("brage",))
    standing = found.standing("alfheim")
    assert standing["verdict"] == "uncorroborated"
    assert standing["observers"] == 1
    judged = found.judge("alfheim", fmt(EPOCH))
    assert judged["verdict"] == "refused"
    assert judged["reason"] == "uncorroborated"
    # Two observers are still short of the policy's three.
    report_skew(found, "alfheim", 1, observers=("solberg",), seq=1)
    assert found.standing("alfheim")["verdict"] == "uncorroborated"


def test_one_observer_casts_one_latest_vote_however_often_it_reports():
    found = watch()
    for seq in (1, 2, 3):
        found.observe("brage", "alfheim", fmt(EPOCH + timedelta(seconds=100)),
                      fmt(EPOCH), seq)
    standing = found.standing("alfheim")
    assert standing["samples"] == 3
    assert standing["observers"] == 1
    assert standing["verdict"] == "uncorroborated"
    # Its latest vote replaces the earlier ones.
    found.observe("brage", "alfheim", fmt(EPOCH + timedelta(seconds=2)),
                  fmt(EPOCH), 4)
    assert found.standing("alfheim")["median_skew_seconds"] == 2


def test_the_median_resists_a_wild_outlier_vote():
    found = watch()
    found.observe("brage", "alfheim", fmt(EPOCH + timedelta(seconds=2)),
                  fmt(EPOCH), 1)
    found.observe("solberg", "alfheim", fmt(EPOCH + timedelta(seconds=3)),
                  fmt(EPOCH), 1)
    found.observe("dain", "alfheim", fmt(EPOCH + timedelta(seconds=1000)),
                  fmt(EPOCH), 1)
    standing = found.standing("alfheim")
    assert standing["median_skew_seconds"] == 3  # a mean would read ~335
    assert standing["max_abs_skew_seconds"] == 1000
    assert standing["verdict"] == "trusted"


# -- severe skew is refused, never corrected into logic -------------


def test_a_severe_nodes_timestamps_are_refused_by_name():
    found = watch()
    report_skew(found, "alfheim", 120)
    assert found.standing("alfheim")["verdict"] == "severe"
    judged = found.judge("alfheim", fmt(EPOCH))
    assert judged["verdict"] == "refused"
    assert judged["reason"] == "severely_skewed"
    assert judged["corrected_at"] is None
    report = found.report()
    assert report["severe_nodes"] == 1
    assert report["judgements"] == 0
    assert report["refusals_by_reason"] == {"severely_skewed": 1}


# -- freshness is by sequence, per observer ---------------------------


def test_stale_and_duplicate_samples_refresh_nothing():
    found = watch()
    found.observe("brage", "alfheim", fmt(EPOCH + timedelta(seconds=2)),
                  fmt(EPOCH), 5)
    stale = found.observe("brage", "alfheim", fmt(EPOCH + timedelta(seconds=50)),
                          fmt(EPOCH), 4)
    assert stale["reason"] == "stale_sequence"
    duplicate = found.observe(
        "brage", "alfheim", fmt(EPOCH + timedelta(seconds=50)), fmt(EPOCH), 5
    )
    assert duplicate["reason"] == "duplicate_sequence"
    standing = found.standing("alfheim")
    assert standing["samples"] == 1
    assert standing["median_skew_seconds"] == 2
    # Another observer's sequence is its own line.
    other = found.observe("solberg", "alfheim", fmt(EPOCH + timedelta(seconds=2)),
                          fmt(EPOCH), 1)
    assert other["verdict"] == "accepted"


# -- judged acts --------------------------------------------------------


def test_enrol_and_observe_refusals_are_named():
    tight = default_policy()
    tight["max_nodes"] = 2
    found = ClockWatch(policy=tight, now=Clock())
    found.enrol("alfheim")
    dup = found.enrol("alfheim")
    assert dup["reason"] == "duplicate_node"
    found.enrol("brage")
    full = found.enrol("solberg")
    assert full["reason"] == "mesh_full"

    wide = watch()
    ghost_node = wide.observe("brage", "never-enrolled", fmt(EPOCH), fmt(EPOCH), 1)
    assert ghost_node["reason"] == "unknown_node"
    ghost_observer = wide.observe(
        "never-enrolled", "alfheim", fmt(EPOCH), fmt(EPOCH), 1
    )
    assert ghost_observer["reason"] == "unknown_observer"
    own = wide.observe("alfheim", "alfheim", fmt(EPOCH), fmt(EPOCH), 1)
    assert own["reason"] == "self_observation"
    bad = wide.observe("brage", "alfheim", "not-a-time", fmt(EPOCH), 1)
    assert bad["reason"] == "malformed_sample"
    bad_judge = wide.judge("alfheim", "not-a-time")
    assert bad_judge["reason"] == "malformed_sample"
    ghost_judge = wide.judge("never-enrolled", fmt(EPOCH))
    assert ghost_judge["reason"] == "unknown_node"
    with pytest.raises(ClockError, match="unknown_node"):
        wide.standing("never-enrolled")
    report = wide.report()
    assert report["refusals_by_reason"] == {
        "malformed_sample": 2,
        "self_observation": 1,
        "unknown_node": 2,
        "unknown_observer": 1,
    }


# -- real beats feed the watch ------------------------------------------


def test_a_validated_heartbeat_feeds_the_watch():
    found = watch()
    record = found.observe_beat(
        "brage", beat(at=EPOCH + timedelta(seconds=7)), fmt(EPOCH)
    )
    assert record["verdict"] == "accepted"
    assert record["action"] == "observe_beat"
    assert record["skew_seconds"] == 7
    again = found.observe_beat(
        "brage", beat(at=EPOCH + timedelta(seconds=7)), fmt(EPOCH)
    )
    assert again["reason"] == "duplicate_sequence"  # the beat's own seq
    broken = found.observe_beat("brage", {"node_id": "alfheim"}, fmt(EPOCH))
    assert broken["reason"] == "malformed_beat"
    assert found.standing("alfheim")["samples"] == 1


# -- the mesh consensus ---------------------------------------------------


def test_consensus_offset_counts_only_corroborated_non_severe_nodes():
    found = watch(nodes=("alfheim", "brage", "solberg", "dain", "eik"))
    # alfheim trusted at +2, brage skewed at +30: both corroborated.
    report_skew(found, "alfheim", 2, observers=("brage", "solberg", "dain"))
    report_skew(found, "brage", 30, observers=("alfheim", "solberg", "dain"))
    # solberg severe at +120: corroborated but quarantined from consensus.
    report_skew(found, "solberg", 120, observers=("alfheim", "brage", "dain"))
    # dain heard from one observer only: uncorroborated, excluded.
    report_skew(found, "dain", 4, observers=("alfheim",))
    report = found.report()
    validate_report(report)
    assert report["consensus_offset_seconds"] == 16  # median of 2 and 30
    assert report["trusted_nodes"] == 1
    assert report["skewed_nodes"] == 1
    assert report["severe_nodes"] == 1
    assert report["uncorroborated_nodes"] == 2  # dain, and eik (never heard)


def test_an_empty_watch_reports_no_consensus_and_no_trust():
    found = ClockWatch(now=Clock())
    report = found.report()
    validate_report(report)
    assert report["consensus_offset_seconds"] is None
    assert report["nodes"] == []
    found.enrol("alfheim")
    standing = found.standing("alfheim")
    assert standing["verdict"] == "unknown"
    assert standing["median_skew_seconds"] is None
    assert found.judge("alfheim", fmt(EPOCH))["reason"] == "uncorroborated"


# -- counted state ---------------------------------------------------------


def test_report_records_and_events_validate_with_honest_counts():
    found = watch()
    report_skew(found, "alfheim", 2)
    found.judge("alfheim", fmt(EPOCH + timedelta(minutes=5)))
    report = found.report()
    validate_report(report)
    assert report["nodes_enrolled"] == 4
    assert report["samples_accepted"] == 3
    assert report["simulated"] is True
    for record in report["records"]:
        validate_record(record)
    for event in found.events:
        validate_event(event)
    kinds = [e["type"] for e in found.events]
    assert "clock.node_enrolled" in kinds
    assert "clock.sample_recorded" in kinds
    assert "clock.timestamp_judged" in kinds
    blob = json.dumps(report)
    assert "payload" not in blob


def test_the_founding_readme_is_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
