"""Task channel tests — Y-057's acceptance.

The slice must prove: (a) offers ride the fabric's
``tasks.offers`` topic as validated fabric messages
carrying the Y-007 task envelope whole, and duplicate /
over-bound / malformed offers are refused by name and
counted; (b) first claim wins — later, self, unknown,
and closed-task claims are refused by name, and the
claim rides a direct fabric message to the offerer;
(c) stream events ride direct to the offerer in
consecutive seq order from the claimant only, and the
backpressure window is explicit — a full window refuses
by name with the unacknowledged count, delivers
nothing, loses nothing (the same seq resends after an
ack), acks are cumulative/monotonic and cannot run
ahead of delivery; (d) exactly one terminal result,
validated as a Y-007 result envelope with provenance
naming the claimant, closes the task, after which every
act on it is refused; (e) reports, records, and events
validate, counts are honest, and the founding README
is untouched.

Everything here rides the in-process fabric: no socket
is opened and no LAN is crossed, and nothing claims
one was (real carriage is Y-062's).
"""

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.fabric import validate_message
from yggdrasil.observability import validate_event
from yggdrasil.task_channel import (
    OFFERS_TOPIC,
    TaskChannel,
    TaskChannelError,
    default_policy,
    validate_policy,
    validate_record,
    validate_report,
)

ROOT = Path(__file__).resolve().parents[1]
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
FIXED = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)

POLICY = {
    "version": 1,
    "max_open_tasks": 3,
    "stream_window": 2,
    "max_stream_events_per_task": 5,
}


def make_task(**overrides):
    task = {
        "id": str(uuid.uuid4()),
        "kind": "generate.text",
        "caller": "test",
        "payload": {"prompt": "hello"},
    }
    task.update(overrides)
    return task


def make_result(task_id, node_id="midgard", **overrides):
    result = {
        "task_id": task_id,
        "status": "completed",
        "result": "SIMULATED answer",
        "provenance": {
            "node_id": node_id,
            "engine": "fake",
            "model": "simulated-model",
        },
        "timings_ms": {"queue": 0, "run": 1, "total": 1},
    }
    result.update(overrides)
    return result


def channel(policy=None):
    chan = TaskChannel(policy or dict(POLICY), now=lambda: FIXED)
    chan.join("alfheim")
    chan.join("midgard")
    chan.join("vanaheim")
    return chan


def offered(chan=None):
    chan = chan or channel()
    task = make_task()
    chan.offer("alfheim", task)
    return chan, task


def claimed(chan=None):
    chan, task = offered(chan)
    chan.claim(task["id"], "midgard")
    return chan, task


def stream_event(task_id, seq, event="token", data=None):
    record = {"task_id": task_id, "seq": seq, "event": event}
    if data is not None:
        record["data"] = data
    return record


# -- policy & membership --------------------------------------


def test_default_policy_validates_and_is_labelled_a_policy():
    policy = default_policy()
    assert validate_policy(policy) == policy
    assert policy["stream_window"] >= 1
    with pytest.raises(TaskChannelError):
        validate_policy({"version": 1, "max_open_tasks": 0,
                         "stream_window": 1, "max_stream_events_per_task": 1})


def test_join_leave_and_double_join_refused():
    chan = channel()
    with pytest.raises(TaskChannelError):
        chan.join("alfheim")
    chan.leave("vanaheim")
    assert chan.report()["node_ids"] == ["alfheim", "midgard"]
    with pytest.raises(TaskChannelError):
        chan.leave("vanaheim")
    with pytest.raises(TaskChannelError) as exc:
        chan.offer("vanaheim", make_task())
    assert exc.value.reason == "not_joined"


# -- offers ----------------------------------------------------


def test_offer_rides_the_fabric_topic_with_the_envelope_whole():
    chan, task = offered()
    record = chan.record_for(task["id"])
    assert validate_record(record) == record
    assert record["status"] == "offered"
    assert record["offerer"] == "alfheim"
    assert record["claimant"] is None
    inbox = chan.fabric.inbox("midgard")
    offers = [m for m in inbox if m["topic"] == OFFERS_TOPIC]
    assert len(offers) == 1
    assert validate_message(offers[0]) == offers[0]
    assert offers[0]["payload"]["task"] == task
    assert chan.report()["offers"] == 1


def test_offer_duplicate_and_malformed_refused_by_name():
    chan, task = offered()
    with pytest.raises(TaskChannelError) as exc:
        chan.offer("alfheim", task)
    assert exc.value.reason == "duplicate_task"
    with pytest.raises(TaskChannelError) as exc:
        chan.offer("alfheim", {"id": "not-a-uuid"})
    assert exc.value.reason == "malformed_task"
    report = chan.report()
    assert report["offers"] == 1
    assert report["offers_refused"] == 2
    assert report["refused_reasons"]["duplicate_task"] == 1


def test_offer_past_open_bound_refused_channel_full():
    chan = channel()
    tasks = [make_task() for _ in range(3)]
    for task in tasks:
        chan.offer("alfheim", task)
    with pytest.raises(TaskChannelError) as exc:
        chan.offer("alfheim", make_task())
    assert exc.value.reason == "channel_full"
    # Closing one frees a place at the bound.
    chan.claim(tasks[0]["id"], "midgard")
    chan.send_result(tasks[0]["id"], "midgard", make_result(tasks[0]["id"]))
    chan.offer("alfheim", make_task())
    assert chan.report()["open_tasks"] == 3


# -- claims ------------------------------------------------------


def test_first_claim_wins_and_rides_direct_to_offerer():
    chan, task = offered()
    record = chan.claim(task["id"], "midgard")
    assert record["status"] == "claimed"
    assert record["claimant"] == "midgard"
    inbox = chan.fabric.inbox("alfheim")
    claims = [m for m in inbox if m["payload"].get("channel") == "task_claim"]
    assert len(claims) == 1
    assert claims[0]["kind"] == "direct"
    assert claims[0]["from_node"] == "midgard"
    with pytest.raises(TaskChannelError) as exc:
        chan.claim(task["id"], "vanaheim")
    assert exc.value.reason == "already_claimed"


def test_claim_refusals_named():
    chan, task = offered()
    with pytest.raises(TaskChannelError) as exc:
        chan.claim(task["id"], "alfheim")
    assert exc.value.reason == "self_claim"
    with pytest.raises(TaskChannelError) as exc:
        chan.claim(str(uuid.uuid4()), "midgard")
    assert exc.value.reason == "unknown_task"
    report = chan.report()
    assert report["claims"] == 0
    assert report["claims_refused"] == 2


# -- streams & backpressure ---------------------------------------


def test_stream_rides_direct_in_order_to_offerer():
    chan, task = claimed()
    chan.send_stream(task["id"], "midgard", stream_event(task["id"], 0, "started"))
    record = chan.send_stream(
        task["id"], "midgard", stream_event(task["id"], 1, "token", "a")
    )
    assert record["stream_events"] == 2
    assert record["last_seq"] == 1
    assert [e["seq"] for e in chan.stream_for(task["id"])] == [0, 1]
    inbox = chan.fabric.inbox("alfheim")
    carried = [m for m in inbox if m["payload"].get("channel") == "task_stream"]
    assert len(carried) == 2
    assert carried[1]["payload"]["event"]["data"] == "a"


def test_stream_refusals_named():
    chan, task = offered()
    with pytest.raises(TaskChannelError) as exc:
        chan.send_stream(task["id"], "midgard", stream_event(task["id"], 0))
    assert exc.value.reason == "not_claimed"
    chan.claim(task["id"], "midgard")
    with pytest.raises(TaskChannelError) as exc:
        chan.send_stream(task["id"], "vanaheim", stream_event(task["id"], 0))
    assert exc.value.reason == "not_claimant"
    with pytest.raises(TaskChannelError) as exc:
        chan.send_stream(task["id"], "midgard", stream_event(task["id"], 3))
    assert exc.value.reason == "out_of_order"
    other = make_task()
    with pytest.raises(TaskChannelError) as exc:
        chan.send_stream(task["id"], "midgard", stream_event(other["id"], 0))
    assert exc.value.reason == "task_mismatch"
    with pytest.raises(TaskChannelError) as exc:
        chan.send_stream(task["id"], "midgard", {"task_id": task["id"]})
    assert exc.value.reason == "malformed_event"


def test_backpressure_window_is_explicit_and_lossless():
    chan, task = claimed()
    chan.send_stream(task["id"], "midgard", stream_event(task["id"], 0))
    chan.send_stream(task["id"], "midgard", stream_event(task["id"], 1))
    # Window (2) full: the next event is refused by name,
    # delivered nowhere, and the refusal says how deep
    # the window stands.
    with pytest.raises(TaskChannelError) as exc:
        chan.send_stream(task["id"], "midgard", stream_event(task["id"], 2))
    assert exc.value.reason == "backpressure"
    assert "2 unacknowledged" in str(exc.value)
    assert chan.stream_for(task["id"])[-1]["seq"] == 1
    report = chan.report()
    assert report["backpressure_refusals"] == 1
    assert report["stream_refused"] == 1
    # The offerer acks; the very same seq now carries.
    chan.acknowledge(task["id"], "alfheim", 1)
    record = chan.send_stream(task["id"], "midgard", stream_event(task["id"], 2))
    assert record["stream_events"] == 3
    assert record["acked_seq"] == 1


def test_ack_rules_monotonic_and_not_ahead():
    chan, task = claimed()
    chan.send_stream(task["id"], "midgard", stream_event(task["id"], 0))
    with pytest.raises(TaskChannelError) as exc:
        chan.acknowledge(task["id"], "alfheim", 5)
    assert exc.value.reason == "ack_ahead"
    with pytest.raises(TaskChannelError) as exc:
        chan.acknowledge(task["id"], "midgard", 0)
    assert exc.value.reason == "not_offerer"
    chan.acknowledge(task["id"], "alfheim", 0)
    with pytest.raises(TaskChannelError) as exc:
        chan.acknowledge(task["id"], "alfheim", 0)
    assert exc.value.reason == "ack_not_forward"


def test_stream_total_bound_refused():
    chan = channel(
        {"version": 1, "max_open_tasks": 3, "stream_window": 10,
         "max_stream_events_per_task": 2}
    )
    chan, task = claimed(chan)
    chan.send_stream(task["id"], "midgard", stream_event(task["id"], 0))
    chan.send_stream(task["id"], "midgard", stream_event(task["id"], 1))
    with pytest.raises(TaskChannelError) as exc:
        chan.send_stream(task["id"], "midgard", stream_event(task["id"], 2))
    assert exc.value.reason == "stream_too_long"


# -- results --------------------------------------------------------


def test_result_closes_task_and_rides_direct():
    chan, task = claimed()
    chan.send_stream(task["id"], "midgard", stream_event(task["id"], 0, "started"))
    result = make_result(task["id"])
    record = chan.send_result(task["id"], "midgard", result)
    assert record["status"] == "closed"
    assert record["result_status"] == "completed"
    assert chan.result_for(task["id"]) == result
    inbox = chan.fabric.inbox("alfheim")
    carried = [m for m in inbox if m["payload"].get("channel") == "task_result"]
    assert len(carried) == 1
    assert carried[0]["payload"]["result"] == result
    # Everything after closure is refused by name.
    for act in (
        lambda: chan.claim(task["id"], "vanaheim"),
        lambda: chan.send_stream(task["id"], "midgard", stream_event(task["id"], 1)),
        lambda: chan.send_result(task["id"], "midgard", result),
        lambda: chan.acknowledge(task["id"], "alfheim", 0),
    ):
        with pytest.raises(TaskChannelError) as exc:
            act()
        assert exc.value.reason == "task_closed"


def test_result_refusals_named():
    chan, task = offered()
    result = make_result(task["id"])
    with pytest.raises(TaskChannelError) as exc:
        chan.send_result(task["id"], "midgard", result)
    assert exc.value.reason == "not_claimed"
    chan.claim(task["id"], "midgard")
    with pytest.raises(TaskChannelError) as exc:
        chan.send_result(task["id"], "vanaheim", result)
    assert exc.value.reason == "not_claimant"
    dressed = make_result(task["id"], node_id="vanaheim")
    with pytest.raises(TaskChannelError) as exc:
        chan.send_result(task["id"], "midgard", dressed)
    assert exc.value.reason == "provenance_mismatch"
    other = make_result(str(uuid.uuid4()))
    with pytest.raises(TaskChannelError) as exc:
        chan.send_result(task["id"], "midgard", other)
    assert exc.value.reason == "task_mismatch"
    with pytest.raises(TaskChannelError) as exc:
        chan.send_result(task["id"], "midgard", {"task_id": task["id"]})
    assert exc.value.reason == "malformed_result"
    assert chan.result_for(task["id"]) is None


def test_failed_result_also_closes():
    chan, task = claimed()
    failed = make_result(
        task["id"], status="failed", result=None,
        error={"code": "engine_error", "detail": "boom"},
    )
    record = chan.send_result(task["id"], "midgard", failed)
    assert record["result_status"] == "failed"
    assert chan.report()["open_tasks"] == 0


# -- report, events, honesty -----------------------------------------


def test_report_and_events_validate_with_honest_counts():
    chan, task = claimed()
    chan.send_stream(task["id"], "midgard", stream_event(task["id"], 0))
    chan.send_result(task["id"], "midgard", make_result(task["id"]))
    report = chan.report()
    assert validate_report(report) == report
    assert report["simulated"] is True
    assert report["offers"] == 1
    assert report["claims"] == 1
    assert report["stream_sent"] == 1
    assert report["results"] == 1
    assert report["open_tasks"] == 0
    assert report["tasks"][0]["task_id"] == task["id"]
    for event in chan.events:
        assert validate_event(event) == event
    types = [e["type"] for e in chan.events]
    assert "task_channel.task_offered" in types
    assert "task_channel.task_claimed" in types
    assert "task_channel.task_closed" in types


def test_unknown_task_reads_refused():
    chan = channel()
    for act in (
        lambda: chan.stream_for(str(uuid.uuid4())),
        lambda: chan.result_for(str(uuid.uuid4())),
        lambda: chan.record_for(str(uuid.uuid4())),
    ):
        with pytest.raises(TaskChannelError) as exc:
            act()
        assert exc.value.reason == "unknown_task"


def test_readme_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5


