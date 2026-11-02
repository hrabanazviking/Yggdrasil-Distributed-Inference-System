"""Store-and-forward tests — Y-061's acceptance.

The slice must prove: (a) the discipline is data —
expiry always evented, delivery always once, the
default TTL never past the maximum, every bound
positive, and a weaker policy fails validation;
(b) a task for a sleeping node is held whole under
its TTL — the queueing's own TTL or the policy
default — and waking delivers it exactly once, in
queue order, to its recipient only; (c) expiry is
an event, not a silence — a task whose TTL runs
out is never delivered, its expiry is evented,
counted, and written as a notice its sender can
read; (d) the room is bounded per side — whole
room, per recipient, per sender — with each bound
refusing by name and a refused task held nowhere;
(e) duplicate and malformed queueings are refused
by name, a sender may take its held task back and
no one else may, and a cancelled task is never
delivered; (f) reports, records, notices, and
events validate, and the founding README is
untouched.

The mailbox is an in-process holding — no socket
is opened, no LAN is crossed, and no sleeping
hardware was woken (real carriage is Y-062's).
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from yggdrasil.observability import validate_event
from yggdrasil.store_and_forward import (
    StoreAndForward,
    StoreForwardError,
    default_policy,
    validate_notice,
    validate_policy,
    validate_record,
    validate_report,
)

ROOT = Path(__file__).resolve().parents[1]
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
EPOCH = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)

SMALL_POLICY = {
    "version": 1,
    "default_ttl_seconds": 600,
    "max_ttl_seconds": 3600,
    "max_held_tasks": 4,
    "max_held_per_recipient": 2,
    "max_held_per_sender": 3,
    "expiry_evented": True,
    "deliver_once": True,
}


class Clock:
    def __init__(self):
        self.moment = EPOCH

    def __call__(self):
        return self.moment

    def advance(self, seconds):
        self.moment += timedelta(seconds=seconds)


def make_box(clock=None, **overrides):
    policy = {**SMALL_POLICY, **overrides}
    return StoreAndForward(policy, now=clock or Clock())


def make_task(**overrides):
    task = {
        "id": str(uuid.uuid4()),
        "kind": "generate.text",
        "caller": "test",
        "payload": {"prompt": "hello"},
    }
    task.update(overrides)
    return task


# -- the discipline as data --------------------------------


def test_policy_is_law_and_validates():
    policy = default_policy()
    assert policy["expiry_evented"] is True
    assert policy["deliver_once"] is True
    assert policy["default_ttl_seconds"] == 3600
    assert policy["max_ttl_seconds"] == 86400
    assert validate_policy(policy) == policy
    assert validate_policy(SMALL_POLICY) == SMALL_POLICY
    with pytest.raises(StoreForwardError):
        validate_policy({**policy, "expiry_evented": False})
    with pytest.raises(StoreForwardError):
        validate_policy({**policy, "deliver_once": False})
    with pytest.raises(StoreForwardError):
        validate_policy({**policy, "max_held_tasks": 0})
    with pytest.raises(StoreForwardError):
        validate_policy(
            {**policy, "default_ttl_seconds": 90000, "max_ttl_seconds": 3600}
        )


# -- holding and waking --------------------------------------


def test_a_task_waits_whole_and_waking_delivers_it_once():
    clock = Clock()
    box = make_box(clock)
    task = make_task()
    record = box.queue("alfheim", "midgard", task)
    validate_record(record)
    assert record["status"] == "held"
    assert record["ttl_seconds"] == 600  # the policy default
    assert record["expires_at"] == "2026-11-01T12:10:00Z"
    assert record["delivered_at"] is None
    assert [r["task_id"] for r in box.held_for("midgard")] == [task["id"]]
    handed = box.deliver("midgard")
    assert handed == [task]  # the envelope itself, whole
    # exactly once: a second waking hands over nothing
    assert box.deliver("midgard") == []
    record = box.record_for(task["id"])
    assert record["status"] == "delivered"
    assert record["delivered_at"] == "2026-11-01T12:00:00Z"
    report = box.report()
    validate_report(report)
    assert report["queued"] == 1 and report["delivered"] == 1
    assert report["held"] == 0


def test_delivery_is_in_queue_order_and_to_the_recipient_only():
    box = make_box(max_held_per_recipient=10, max_held_tasks=10)
    first, second, other = make_task(), make_task(), make_task()
    box.queue("alfheim", "midgard", first)
    box.queue("vanaheim", "midgard", second)
    box.queue("alfheim", "vanaheim", other)
    assert box.deliver("midgard") == [first, second]
    assert box.deliver("vanaheim") == [other]
    assert box.report()["delivered"] == 3


def test_a_named_ttl_overrides_the_default():
    box = make_box()
    record = box.queue("alfheim", "midgard", make_task(), ttl_seconds=60)
    assert record["ttl_seconds"] == 60
    assert record["expires_at"] == "2026-11-01T12:01:00Z"


# -- expiry: an event, not a silence ---------------------------


def test_expiry_is_an_event_with_a_notice_not_a_silence():
    clock = Clock()
    box = make_box(clock)
    task = make_task()
    box.queue("alfheim", "midgard", task, ttl_seconds=60)
    clock.advance(59)
    assert box.expire() == []  # still inside its TTL
    clock.advance(1)  # the TTL has run out
    expired = box.expire()
    assert [r["task_id"] for r in expired] == [task["id"]]
    assert expired[0]["status"] == "expired"
    # never delivered afterwards
    assert box.deliver("midgard") == []
    # the sender can read the notice; another node cannot
    notices = box.expiry_notices_for("alfheim")
    assert len(notices) == 1
    validate_notice(notices[0])
    assert notices[0]["task_id"] == task["id"]
    assert notices[0]["recipient"] == "midgard"
    assert notices[0]["reason"] == "ttl_expired"
    assert notices[0]["expired_at"] == "2026-11-01T12:01:00Z"
    assert box.expiry_notices_for("vanaheim") == []
    types = [e["type"] for e in box.events]
    assert "store_forward.task_expired" in types
    report = box.report()
    assert report["expired"] == 1 and report["held"] == 0
    assert report["notices"] == notices


def test_expiry_is_swept_by_delivery_and_a_waking_in_time_still_delivers():
    clock = Clock()
    box = make_box(clock)
    stale, fresh = make_task(), make_task()
    box.queue("alfheim", "midgard", stale, ttl_seconds=30)
    box.queue("alfheim", "midgard", fresh, ttl_seconds=300)
    clock.advance(60)
    # no explicit expire(): the waking call itself
    # sweeps the stale task out and delivers the fresh
    assert box.deliver("midgard") == [fresh]
    assert box.record_for(stale["id"])["status"] == "expired"
    assert len(box.expiry_notices_for("alfheim")) == 1


# -- bounds, per side -------------------------------------------


def test_ttl_out_of_bounds_is_refused_by_name():
    box = make_box()
    for bad in (0, -5, 3601, True, "soon"):
        with pytest.raises(StoreForwardError) as exc:
            box.queue("alfheim", "midgard", make_task(), ttl_seconds=bad)
        assert exc.value.reason == "ttl_out_of_bounds"
    # exactly at the maximum is held
    record = box.queue("alfheim", "midgard", make_task(), ttl_seconds=3600)
    assert record["status"] == "held"
    report = box.report()
    assert report["refused_reasons"] == {"ttl_out_of_bounds": 5}
    assert report["held"] == 1  # the refused tasks are held nowhere


def test_recipient_bound_refuses_by_name_and_spares_other_recipients():
    box = make_box()
    box.queue("alfheim", "midgard", make_task())
    box.queue("vanaheim", "midgard", make_task())
    with pytest.raises(StoreForwardError) as exc:
        box.queue("alfheim", "midgard", make_task())
    assert exc.value.reason == "recipient_full"
    # another recipient's waiting room is untouched
    assert box.queue("alfheim", "vanaheim", make_task())["status"] == "held"


def test_sender_and_room_bounds_refuse_by_name():
    box = make_box(max_held_per_recipient=10)
    for recipient in ("midgard", "vanaheim", "jotunheim"):
        box.queue("alfheim", recipient, make_task())
    with pytest.raises(StoreForwardError) as exc:
        box.queue("alfheim", "svartalfheim", make_task())
    assert exc.value.reason == "sender_full"

    box = make_box(max_held_per_sender=10, max_held_per_recipient=10)
    for sender in ("alfheim", "vanaheim", "jotunheim", "midgard"):
        box.queue(sender, "niflheim", make_task())
    with pytest.raises(StoreForwardError) as exc:
        box.queue("alfheim", "muspelheim", make_task())
    assert exc.value.reason == "mailbox_full"


def test_duplicate_and_malformed_queueings_are_refused_by_name():
    box = make_box()
    task = make_task()
    box.queue("alfheim", "midgard", task)
    with pytest.raises(StoreForwardError) as exc:
        box.queue("vanaheim", "midgard", task)  # same id, any sender
    assert exc.value.reason == "duplicate_task"
    box.deliver("midgard")
    with pytest.raises(StoreForwardError) as exc:
        box.queue("alfheim", "midgard", task)  # an id stays spent
    assert exc.value.reason == "duplicate_task"
    with pytest.raises(StoreForwardError) as exc:
        box.queue("alfheim", "midgard", {"id": "not-a-task"})
    assert exc.value.reason == "malformed_task"
    report = box.report()
    assert report["refused_reasons"] == {
        "duplicate_task": 2,
        "malformed_task": 1,
    }
    assert report["queued"] == 1


# -- taking a task back -------------------------------------------


def test_a_sender_may_take_its_task_back_and_no_one_else_may():
    box = make_box()
    task = make_task()
    box.queue("alfheim", "midgard", task)
    with pytest.raises(StoreForwardError) as exc:
        box.cancel(task["id"], "vanaheim")
    assert exc.value.reason == "not_sender"
    record = box.cancel(task["id"], "alfheim")
    assert record["status"] == "cancelled"
    # a cancelled task is never delivered
    assert box.deliver("midgard") == []
    with pytest.raises(StoreForwardError) as exc:
        box.cancel(task["id"], "alfheim")
    assert exc.value.reason == "not_held"
    with pytest.raises(StoreForwardError) as exc:
        box.cancel(str(uuid.uuid4()), "alfheim")
    assert exc.value.reason == "unknown_task"
    report = box.report()
    assert report["cancelled"] == 1
    assert report["refused_reasons"] == {"not_sender": 1, "not_held": 1, "unknown_task": 1}


# -- shapes, events, honesty -----------------------------------------


def test_report_records_and_events_validate():
    clock = Clock()
    box = make_box(clock)
    kept, dying = make_task(), make_task()
    box.queue("alfheim", "midgard", kept, ttl_seconds=300)
    box.queue("alfheim", "vanaheim", dying, ttl_seconds=30)
    clock.advance(60)
    assert box.deliver("midgard") == [kept]
    report = box.report()
    validate_report(report)
    assert report["simulated"] is True
    assert report["queued"] == 2
    assert report["delivered"] == 1 and report["expired"] == 1
    assert report["held"] == 0
    for event in box.events:
        validate_event(event)
    types = [e["type"] for e in box.events]
    assert "store_forward.task_queued" in types
    assert "store_forward.task_delivered" in types
    assert "store_forward.task_expired" in types


def test_readme_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
