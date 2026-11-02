"""Partition behaviour tests — Y-058's acceptance.

The slice must prove: (a) while whole, the mesh runs
Y-057's discipline — one claim, one terminal result,
one answer delivered to the offerer over the fabric —
and refusals are named and counted; (b) a cut is
lawful data (naming everyone or nobody or a stranger
refused, no double cut, no joining mid-split), and
while split each half keeps working: offers are seen
only on the offerer's side, claims are judged per
side, and both sides may claim the same pre-cut task;
(c) duplicate executions reconcile by task id on
healing under the one winner rule — the offerer's own
side wins, so a provisional answer is never retracted;
the loser is named in the record and in
``duplicates_for``, counted, and never delivered —
the offerer's fabric inbox holds exactly one
``task_result`` for the task, ever; (d) a cross-seam
execution alone is held undelivered while split and
becomes the answer at healing; a duplicate arriving
after healing from a legitimate side claimant is
reconciled on arrival; (e) CRDT replicas diverge
honestly under the cut (each side's writes land on
its own replicas) and healing merges them — every
replica ends holding identical mergeable state, the
stamp order settles a same-node contest the same way
everywhere, and models offered on the far side are
present after the heal; (f) reports, records, and
events validate, counts are honest, and the founding
README is untouched.

Everything here is the in-process model: no socket is
opened and no LAN is crossed, and nothing claims one
was (real carriage is Y-062's).
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from yggdrasil.crdt_stores import make_stamp
from yggdrasil.observability import validate_event
from yggdrasil.partition import (
    PartitionError,
    PartitionMesh,
    default_policy,
    validate_policy,
    validate_record,
    validate_report,
)
from yggdrasil.simulator import make_fake_census

ROOT = Path(__file__).resolve().parents[1]
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
EPOCH = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)

NODES = ("alfheim", "midgard", "vanaheim", "jotunheim")
NAMED = ("alfheim", "midgard")  # offerer's side in most tests
REST = ("vanaheim", "jotunheim")


class Clock:
    def __init__(self):
        self.moment = EPOCH

    def __call__(self):
        return self.moment

    def advance(self, seconds):
        self.moment += timedelta(seconds=seconds)


def make_task(**overrides):
    task = {
        "id": str(uuid.uuid4()),
        "kind": "generate.text",
        "caller": "test",
        "payload": {"prompt": "hello"},
    }
    task.update(overrides)
    return task


def make_result(task_id, node_id, **overrides):
    result = {
        "task_id": task_id,
        "status": "completed",
        "result": f"SIMULATED answer from {node_id}",
        "provenance": {
            "node_id": node_id,
            "engine": "fake",
            "model": "simulated-model",
        },
        "timings_ms": {"queue": 0, "run": 1, "total": 1},
    }
    result.update(overrides)
    return result


def census(node_id, *, ram_gb=8.0):
    found = make_fake_census(node_id)
    found["ram_gb"] = ram_gb
    return found


def mesh(clock=None, policy=None):
    m = PartitionMesh(policy, now=clock or Clock())
    for node in NODES:
        m.join(node)
    return m


def results_in_inbox(m, node_id):
    return [
        msg for msg in m.fabric.inbox(node_id)
        if isinstance(msg.get("payload"), dict)
        and msg["payload"].get("channel") == "task_result"
    ]


# -- the law as data --------------------------------------


def test_policy_is_law_and_validates():
    policy = default_policy()
    assert policy["duplicate_answers_allowed"] is False
    assert policy["merge_on_heal"] is True
    assert validate_policy(policy) == policy
    with pytest.raises(PartitionError):
        validate_policy({**policy, "duplicate_answers_allowed": True})
    with pytest.raises(PartitionError):
        validate_policy({**policy, "winner_rule": "loudest_first"})
    with pytest.raises(PartitionError):
        PartitionMesh({**policy, "merge_on_heal": False})


# -- while whole: one claim, one answer ---------------------


def test_whole_mesh_one_answer_delivered_once():
    m = mesh()
    task = make_task()
    record = m.offer("alfheim", task)
    assert record["status"] == "open"
    record = m.claim(task["id"], "midgard")
    assert record["claimants"] == ["midgard"]
    record = m.send_result(task["id"], "midgard", make_result(task["id"], "midgard"))
    assert record["status"] == "answered"
    assert record["answer_claimant"] == "midgard"
    assert record["executions"] == 1
    assert record["provisional"] is False
    assert m.answer_for(task["id"])["provenance"]["node_id"] == "midgard"
    inbox = results_in_inbox(m, "alfheim")
    assert len(inbox) == 1
    assert inbox[0]["payload"]["result"]["provenance"]["node_id"] == "midgard"
    validate_record(record)


def test_whole_mesh_refusals_are_named():
    m = mesh()
    task = make_task()
    m.offer("alfheim", task)
    with pytest.raises(PartitionError) as exc:
        m.offer("alfheim", task)
    assert exc.value.reason == "duplicate_task"
    with pytest.raises(PartitionError) as exc:
        m.claim(task["id"], "alfheim")
    assert exc.value.reason == "self_claim"
    with pytest.raises(PartitionError) as exc:
        m.claim(str(uuid.uuid4()), "midgard")
    assert exc.value.reason == "unknown_task"
    m.claim(task["id"], "midgard")
    with pytest.raises(PartitionError) as exc:
        m.claim(task["id"], "vanaheim")
    assert exc.value.reason == "already_claimed"
    with pytest.raises(PartitionError) as exc:
        m.send_result(task["id"], "vanaheim", make_result(task["id"], "vanaheim"))
    assert exc.value.reason == "not_claimant"
    with pytest.raises(PartitionError) as exc:
        m.send_result(task["id"], "midgard", make_result(task["id"], "vanaheim"))
    assert exc.value.reason == "provenance_mismatch"
    report = m.report()
    assert report["offers_refused"] == 1
    assert report["claims_refused"] == 3
    assert report["results_refused"] == 2
    validate_report(report)


def test_claimants_second_result_is_refused_not_double_counted():
    m = mesh()
    task = make_task()
    m.offer("alfheim", task)
    m.claim(task["id"], "midgard")
    m.send_result(task["id"], "midgard", make_result(task["id"], "midgard"))
    with pytest.raises(PartitionError) as exc:
        m.send_result(task["id"], "midgard", make_result(task["id"], "midgard"))
    assert exc.value.reason == "duplicate_result"
    assert m.report()["duplicates_reconciled"] == 0
    assert len(results_in_inbox(m, "alfheim")) == 1


# -- the cut itself ------------------------------------------


def test_cut_law():
    m = mesh()
    with pytest.raises(PartitionError):
        m.heal()
    with pytest.raises(PartitionError) as exc:
        m.cut(NODES)
    assert exc.value.reason == "cut_all"
    with pytest.raises(PartitionError):
        m.cut(())
    with pytest.raises(PartitionError) as exc:
        m.cut(("alfheim", "niflheim"))
    assert exc.value.reason == "not_joined"
    m.cut(NAMED)
    assert m.partitioned is True
    report = m.report()
    assert report["side_named"] == sorted(NAMED)
    assert report["side_rest"] == sorted(REST)
    with pytest.raises(PartitionError) as exc:
        m.cut(REST)
    assert exc.value.reason == "already_partitioned"
    with pytest.raises(PartitionError) as exc:
        m.join("niflheim")
    assert exc.value.reason == "partitioned"
    healed = m.heal()
    assert healed["partitioned"] is False
    assert healed["cuts"] == 1 and healed["heals"] == 1


# -- halves keep working --------------------------------------


def test_halves_work_and_see_only_their_own_offers():
    m = mesh()
    shared = make_task()
    m.offer("alfheim", shared)  # offered while whole: seen by all
    m.cut(NAMED)
    # both sides may claim the task each side saw
    assert m.claim(shared["id"], "midgard")["claimants"] == ["midgard"]
    record = m.claim(shared["id"], "vanaheim")
    assert sorted(record["claimants"]) == ["midgard", "vanaheim"]
    # but within a side, first claim still wins
    with pytest.raises(PartitionError) as exc:
        m.claim(shared["id"], "jotunheim")
    assert exc.value.reason == "already_claimed"
    # an offer made on one side is unknown on the other
    side_task = make_task()
    m.offer("vanaheim", side_task)
    with pytest.raises(PartitionError) as exc:
        m.claim(side_task["id"], "alfheim")
    assert exc.value.reason == "unknown_task"
    assert m.claim(side_task["id"], "jotunheim")["status"] == "claimed"
    m.heal()
    # after healing the far side knows the task too
    assert m.record_for(side_task["id"])["offerer"] == "vanaheim"


def test_cross_seam_claim_waits_in_the_fabric_until_heal():
    m = mesh()
    task = make_task()
    m.offer("alfheim", task)
    m.cut(NAMED)
    m.claim(task["id"], "vanaheim")  # claim message crosses the seam
    claims = [
        msg for msg in m.fabric.inbox("alfheim")
        if isinstance(msg.get("payload"), dict)
        and msg["payload"].get("channel") == "task_claim"
    ]
    assert claims == []  # held by the fabric, not delivered, not lost
    m.heal()
    claims = [
        msg for msg in m.fabric.inbox("alfheim")
        if isinstance(msg.get("payload"), dict)
        and msg["payload"].get("channel") == "task_claim"
    ]
    assert len(claims) == 1
    assert claims[0]["payload"]["claimant"] == "vanaheim"


# -- reconciliation: never silently double-answered ------------


def test_duplicate_execution_offerer_side_wins_and_is_never_retracted():
    clock = Clock()
    m = mesh(clock)
    task = make_task()
    m.offer("alfheim", task)
    m.cut(NAMED)
    m.claim(task["id"], "midgard")     # offerer's side
    m.claim(task["id"], "vanaheim")    # far side, in ignorance
    # the far side finishes FIRST — it still loses, because
    # the offerer's own side's answer is the one the
    # offerer already holds, and answers are not retracted
    m.send_result(task["id"], "vanaheim", make_result(task["id"], "vanaheim"))
    assert m.answer_for(task["id"]) is None  # held across the seam
    assert results_in_inbox(m, "alfheim") == []
    clock.advance(60)
    record = m.send_result(
        task["id"], "midgard", make_result(task["id"], "midgard")
    )
    assert record["provisional"] is True
    assert record["answer_claimant"] == "midgard"
    assert m.answer_for(task["id"])["provenance"]["node_id"] == "midgard"
    # fabric inboxes drain on read — accumulate every
    # delivery from here on and count the total
    delivered = results_in_inbox(m, "alfheim")
    assert len(delivered) == 1
    report = m.heal()
    record = m.record_for(task["id"])
    assert record["status"] == "reconciled"
    assert record["provisional"] is False
    assert record["answer_claimant"] == "midgard"
    assert record["executions"] == 2
    assert record["duplicate_claimants"] == ["vanaheim"]
    assert report["duplicates_reconciled"] == 1
    # the answer never changed and was never delivered twice
    assert m.answer_for(task["id"])["provenance"]["node_id"] == "midgard"
    delivered += results_in_inbox(m, "alfheim")
    assert len(delivered) == 1
    assert delivered[0]["payload"]["result"]["provenance"]["node_id"] == "midgard"
    duplicates = m.duplicates_for(task["id"])
    assert [d["claimant"] for d in duplicates] == ["vanaheim"]
    assert duplicates[0]["result"]["provenance"]["node_id"] == "vanaheim"


def test_lone_cross_seam_execution_becomes_the_answer_at_heal():
    m = mesh()
    task = make_task()
    m.offer("alfheim", task)
    m.cut(NAMED)
    m.claim(task["id"], "vanaheim")
    m.send_result(task["id"], "vanaheim", make_result(task["id"], "vanaheim"))
    assert m.answer_for(task["id"]) is None
    assert results_in_inbox(m, "alfheim") == []
    m.heal()
    assert m.answer_for(task["id"])["provenance"]["node_id"] == "vanaheim"
    inbox = results_in_inbox(m, "alfheim")
    assert len(inbox) == 1
    assert inbox[0]["payload"]["result"]["provenance"]["node_id"] == "vanaheim"
    record = m.record_for(task["id"])
    assert record["status"] == "answered"
    assert record["executions"] == 1
    assert m.report()["duplicates_reconciled"] == 0


def test_late_duplicate_after_heal_reconciles_on_arrival():
    m = mesh()
    task = make_task()
    m.offer("alfheim", task)
    m.cut(NAMED)
    m.claim(task["id"], "midgard")
    m.claim(task["id"], "vanaheim")
    m.send_result(task["id"], "midgard", make_result(task["id"], "midgard"))
    m.heal()  # only one execution exists so far; it is confirmed
    assert m.report()["duplicates_reconciled"] == 0
    # the far side's execution finishes after the heal —
    # it is reconciled by task id on arrival, not delivered
    record = m.send_result(
        task["id"], "vanaheim", make_result(task["id"], "vanaheim")
    )
    assert record["status"] == "reconciled"
    assert record["duplicate_claimants"] == ["vanaheim"]
    assert m.answer_for(task["id"])["provenance"]["node_id"] == "midgard"
    assert len(results_in_inbox(m, "alfheim")) == 1
    assert m.report()["duplicates_reconciled"] == 1
    # a result from a node that never claimed is still refused
    with pytest.raises(PartitionError) as exc:
        m.send_result(
            task["id"], "jotunheim", make_result(task["id"], "jotunheim")
        )
    assert exc.value.reason == "not_claimant"


# -- CRDTs: diverge honestly, merge on healing ------------------


def test_crdt_replicas_diverge_then_merge_on_heal():
    m = mesh()
    for node in NODES:
        m.register(node, census(node))
    m.sync_stores()
    assert m.report()["crdt_converged"] is True
    m.cut(NAMED)
    # each side keeps registering on its own replicas
    m.register("alfheim", census("alfheim", ram_gb=16.0))
    m.offer_model("vanaheim", "model-y")
    # a carried write for alfheim lands on the far side
    # with a stamp that outranks alfheim's own local tick
    m.store("jotunheim").put_node(
        census("alfheim", ram_gb=32.0),
        stamp=make_stamp("alfheim", lamport=50, wall_time=EPOCH),
    )
    assert m.report()["crdt_converged"] is False
    assert m.store("alfheim").node("alfheim")["census"]["ram_gb"] == 16.0
    assert m.store("jotunheim").node("alfheim")["census"]["ram_gb"] == 32.0
    with pytest.raises(PartitionError) as exc:
        m.sync_stores()
    assert exc.value.reason == "partitioned"
    m.heal()
    report = m.report()
    assert report["crdt_converged"] is True
    # the stamp order settled the contest the same way
    # on every replica: the lamport-50 write wins whole
    for node in NODES:
        replica = m.store(node)
        assert replica.node("alfheim")["census"]["ram_gb"] == 32.0
        assert "model-y" in replica.node("vanaheim")["models"]
    assert m.store("alfheim").nodes_with_model("model-y") == ["vanaheim"]


def test_register_refuses_a_census_naming_another_node():
    m = mesh()
    with pytest.raises(PartitionError) as exc:
        m.register("alfheim", census("midgard"))
    assert exc.value.reason == "census_mismatch"


# -- shapes, events, honesty -------------------------------------


def test_report_records_and_events_validate():
    m = mesh()
    task = make_task()
    m.offer("alfheim", task)
    m.cut(NAMED)
    m.claim(task["id"], "midgard")
    m.claim(task["id"], "vanaheim")
    m.send_result(task["id"], "midgard", make_result(task["id"], "midgard"))
    m.send_result(task["id"], "vanaheim", make_result(task["id"], "vanaheim"))
    report = m.heal()
    validate_report(report)
    assert report["simulated"] is True
    assert report["tasks"][0]["status"] == "reconciled"
    for event in m.events:
        validate_event(event)
    types = [e["type"] for e in m.events]
    assert "partition.cut" in types
    assert "partition.healed" in types
    assert "partition.duplicate_reconciled" in types


def test_readme_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
