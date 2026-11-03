"""Load shape tests — Y-072's acceptance.

The slice must prove: (a) the law is data — a
plan claiming invented counts or hardware proof
fails validation, and a plan whose own numbers
cross (a window that does not divide by the
heartbeat interval, a size the gossip policy
cannot hold, liveness lines that cross) is
refused by the measurer; (b) a measurement counts
the join and the steady window apart, off the
components' own reports — the join is exactly
Y-055's own convergence measurement for the same
size and policy; (c) the published cost grows
with node count, in totals and per node; (d) the
byte sizes are the real records' canonical
sizes; (e) a fresh door replays a measurement
identically; (f) acts at the door are judged by
name — size_not_planned, duplicate_size,
malformed_size — and refused acts measure
nothing; (g) a shape that never converged is
published converged: false with its counts
standing, never hidden; (h) the report, shapes,
records, and events validate with honest counts,
and the founding README is untouched.

Everything here is SIMULATED in the slice's own
word — meshes are in-process simulator citizens
on a virtual clock; no socket was opened and no
LAN was crossed.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.gossip import default_policy as gossip_default_policy
from yggdrasil.gossip import measure_convergence
from yggdrasil.health_endpoint import build_node_health
from yggdrasil.load_shapes import (
    LoadShapes,
    LoadShapesError,
    default_policy,
    measure_load_shapes,
    validate_policy,
    validate_record,
    validate_report,
    validate_shape,
)
from yggdrasil.observability import validate_event

ROOT = Path(__file__).resolve().parents[1]
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
EPOCH = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)


class Clock:
    def __call__(self):
        return EPOCH


def small_policy(node_counts=(3,), window_seconds=20, **overrides):
    policy = default_policy()
    policy["node_counts"] = list(node_counts)
    policy["window_seconds"] = window_seconds
    policy.update(overrides)
    return validate_policy(policy)


def door(policy=None):
    return LoadShapes(policy=policy or small_policy(), now=Clock())


def canonical_bytes(record) -> int:
    return len(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


# -- the law is data ----------------------------------------------


def test_policy_law_is_schema_constants():
    policy = default_policy()
    assert policy["counts_come_from_components"] is True
    assert policy["hardware_claim_allowed"] is False
    assert policy["node_counts"] == [3, 10, 30]
    assert policy["window_seconds"] == 60
    assert policy["max_rounds"] == 100
    assert policy["gossip"]["fanout"] == 3
    assert policy["gossip"]["round_interval_seconds"] == 5
    assert policy["liveness"]["heartbeat_interval_seconds"] == 10
    validate_policy(policy)
    for broken in (
        {"counts_come_from_components": False},
        {"hardware_claim_allowed": True},
        {"node_counts": []},
        {"node_counts": [3, 3]},
        {"node_counts": [0]},
        {"window_seconds": 5},
        {"max_rounds": -1},
    ):
        with pytest.raises(LoadShapesError):
            validate_policy({**policy, **broken})


def test_policy_crossing_laws_are_refused():
    policy = default_policy()
    policy["node_counts"] = [3]
    # A window that ends mid-beat would publish a
    # fraction of a beat as a whole cost.
    with pytest.raises(LoadShapesError):
        validate_policy({**policy, "window_seconds": 25})
    # A planned size the gossip policy cannot hold
    # could never be measured at all.
    too_small = validate_policy(default_policy())
    too_small["gossip"] = {**too_small["gossip"], "max_nodes": 30}
    validate_policy(too_small)
    too_small["gossip"] = {**too_small["gossip"], "max_nodes": 29}
    with pytest.raises(LoadShapesError):
        validate_policy(too_small)
    # Liveness lines that cross are unlawful inside
    # the embedded policy, by Y-054's own law.
    crossed = validate_policy(default_policy())
    crossed["liveness"] = {
        **crossed["liveness"],
        "suspect_after_seconds": 90,
        "gone_after_seconds": 30,
    }
    with pytest.raises(LoadShapesError):
        validate_policy(crossed)


# -- the measurement ------------------------------------------------


def test_a_measurement_counts_the_join_and_the_window_apart():
    found = door()
    record = found.measure(3)
    validate_record(record)
    assert record["verdict"] == "accepted"
    report = found.report()
    validate_report(report)
    (shape,) = report["shapes"]
    validate_shape(shape)
    assert shape["node_count"] == 3
    assert shape["simulated"] is True
    assert shape["converged"] is True
    # The join: Y-055's deterministic ring at fanout
    # 3 over 3 nodes converges in one round.
    assert shape["join_rounds"] == 1
    assert shape["join_exchanges"] == 6
    assert shape["join_messages"] == 12
    assert shape["join_elapsed_seconds"] == 5
    # The window: 20 virtual seconds at a 10 s
    # heartbeat is two fresh beats per node.
    assert shape["window_seconds"] == 20
    assert shape["window_beats_published"] == 6
    assert shape["window_messages"] > 0
    assert shape["window_exchanges"] > 0
    # The phases sum to the totals, and the record
    # carries the total it published.
    assert shape["total_messages"] == (
        shape["join_messages"] + shape["window_messages"]
    )
    assert shape["total_exchanges"] == (
        shape["join_exchanges"] + shape["window_exchanges"]
    )
    assert record["total_messages"] == shape["total_messages"]
    assert shape["exchanges_refused"] == 0
    assert shape["beats_stored"] > 0
    # The rates are the totals over the measured
    # elapsed time, recomputed here from the shape.
    elapsed = shape["join_elapsed_seconds"] + shape["window_elapsed_seconds"]
    assert shape["messages_per_node"] == round(shape["total_messages"] / 3, 6)
    assert shape["messages_per_node_per_second"] == round(
        shape["total_messages"] / 3 / elapsed, 6
    )


def test_the_join_is_gossips_own_convergence_measurement():
    found = door()
    found.measure(3)
    (shape,) = found.report()["shapes"]
    (measured,) = measure_convergence((3,), policy=gossip_default_policy())
    assert shape["join_rounds"] == measured["rounds"]
    assert shape["join_exchanges"] == measured["exchanges"]
    assert shape["join_messages"] == measured["messages_exchanged"]
    assert shape["join_elapsed_seconds"] == measured["elapsed_seconds"]


def test_the_byte_sizes_are_the_real_records_sizes():
    found = door()
    found.measure(3)
    (shape,) = found.report()["shapes"]
    health = {
        "node_id": "node-01",
        "at": "2026-10-31T12:00:00Z",
        "liveness": "present",
        "queue_depth": 0,
        "models_loaded": [],
    }
    beat = build_node_health(
        node_id="node-01",
        seq=2,
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
    assert shape["heartbeat_bytes"] == canonical_bytes(beat)
    assert shape["registry_entry_bytes"] > 0
    # One node's whole state offer — every entry,
    # tombstone, and held beat — is larger than any
    # single record it carries.
    assert shape["state_offer_bytes"] > (
        shape["heartbeat_bytes"] + shape["registry_entry_bytes"]
    )


def test_overhead_grows_with_node_count():
    found = door(policy=small_policy(node_counts=(3, 10)))
    records = found.measure_all()
    assert [r["verdict"] for r in records] == ["accepted", "accepted"]
    report = found.report()
    small, large = report["shapes"]
    assert (small["node_count"], large["node_count"]) == (3, 10)
    assert large["window_beats_published"] == 20
    assert small["window_beats_published"] == 6
    assert large["total_messages"] > small["total_messages"]
    assert large["total_exchanges"] > small["total_exchanges"]
    assert large["messages_per_node"] > small["messages_per_node"]
    assert large["beats_stored"] > small["beats_stored"]
    assert large["state_offer_bytes"] > small["state_offer_bytes"]
    assert report["sizes_measured"] == 2
    assert report["total_messages"] == (
        small["total_messages"] + large["total_messages"]
    )


def test_a_fresh_door_replays_a_measurement_identically():
    first = door()
    second = door()
    first.measure(3)
    second.measure(3)
    assert first.report() == second.report()


def test_the_default_plan_measures_its_first_size():
    found = LoadShapes(now=Clock())
    record = found.measure(3)
    assert record["verdict"] == "accepted"
    (shape,) = found.report()["shapes"]
    # The default window is six heartbeats.
    assert shape["window_beats_published"] == 18
    assert shape["converged"] is True
    assert shape["join_messages"] == 12


# -- judged acts --------------------------------------------------------


def test_refusals_are_named_and_measure_nothing():
    found = door()
    unplanned = found.measure(10)
    assert unplanned["verdict"] == "refused"
    assert unplanned["reason"] == "size_not_planned"
    for bad in (0, -2, "3", None, True, 2.5):
        record = found.measure(bad)
        assert record["verdict"] == "refused"
        assert record["reason"] == "malformed_size"
    assert found.measure(3)["verdict"] == "accepted"
    again = found.measure(3)
    assert again["reason"] == "duplicate_size"
    report = found.report()
    assert report["sizes_measured"] == 1
    assert report["refusals_by_reason"] == {
        "duplicate_size": 1,
        "malformed_size": 6,
        "size_not_planned": 1,
    }


def test_an_unconverged_shape_is_published_not_hidden():
    found = door(policy=small_policy(max_rounds=0))
    record = found.measure(3)
    assert record["verdict"] == "accepted"
    assert record["total_messages"] == 0
    (shape,) = found.report()["shapes"]
    validate_shape(shape)
    assert shape["converged"] is False
    assert shape["join_rounds"] == 0
    assert shape["join_messages"] == 0
    # The beats were still published on schedule —
    # the window's cost is counted even when no
    # round was allowed to carry it anywhere.
    assert shape["window_beats_published"] == 6
    assert shape["total_messages"] == 0
    assert shape["messages_per_node_per_second"] == 0.0


# -- the published table ---------------------------------------------------


def test_measure_load_shapes_publishes_the_whole_table():
    report = measure_load_shapes(policy=small_policy(), now=Clock())
    validate_report(report)
    assert report["sizes_measured"] == 1
    assert report["shapes"][0]["node_count"] == 3
    assert report["policy"]["node_counts"] == [3]
    assert report["simulated"] is True


def test_report_records_and_events_validate_with_honest_counts():
    found = door()
    found.measure(99)
    found.measure(3)
    report = found.report()
    validate_report(report)
    assert report["sizes_measured"] == 1
    assert report["total_messages"] == report["shapes"][0]["total_messages"]
    assert report["total_exchanges"] == report["shapes"][0]["total_exchanges"]
    assert report["refusals_by_reason"] == {"size_not_planned": 1}
    for record in report["records"]:
        validate_record(record)
    kinds = [e["type"] for e in found.events]
    assert kinds == ["load_shapes.measure_refused", "load_shapes.measured"]
    for own in found.events:
        validate_event(own)


def test_the_founding_readme_is_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
