"""Rate and size discipline tests — Y-060's acceptance.

The slice must prove: (a) the discipline is data —
oversized traffic never allowed, quota breaches
always throttle, every bound positive, and a weaker
policy fails validation; (b) traffic inside every
quota is admitted and counted, per sender; (c) each
quota names its own breach — gossip, task, total,
and bytes — and a breach throttles the sender at
this door for the cooldown: all of its later traffic
is throttled by the name throttled, throttled traffic
extends neither the cooldown nor the window, and a
quiet neighbour at the same door is untouched;
(d) size discipline is absolute — a single oversized
message is throttled however empty the window stands,
and earns no cooldown; (e) recovery is real — when
cooldown and window have both passed, the sender is
admitted again and the lift is evented; if the
window is still full after the cooldown, the next
message breaches again; (f) raw fabric messages are
classified and sized by the stated rule, malformed
judgements are throttled and counted, reports,
records, and events validate, and the founding
README is untouched.

The gate is an in-process door — no socket is opened
and no LAN is crossed, and nothing claims one was
(real carriage is Y-062's).
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from yggdrasil.observability import validate_event
from yggdrasil.rate_discipline import (
    RateError,
    RateGate,
    classify_message,
    default_policy,
    message_size_bytes,
    validate_policy,
    validate_record,
    validate_report,
)

ROOT = Path(__file__).resolve().parents[1]
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
EPOCH = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)

SMALL_POLICY = {
    "version": 1,
    "window_seconds": 10,
    "max_messages_per_window": 5,
    "max_gossip_per_window": 3,
    "max_task_per_window": 2,
    "max_bytes_per_window": 1000,
    "max_message_bytes": 100,
    "throttle_cooldown_seconds": 20,
    "throttle_on_quota": True,
    "oversized_allowed": False,
}


class Clock:
    def __init__(self):
        self.moment = EPOCH

    def __call__(self):
        return self.moment

    def advance(self, seconds):
        self.moment += timedelta(seconds=seconds)


def make_gate(clock=None, **overrides):
    policy = {**SMALL_POLICY, **overrides}
    return RateGate(policy, now=clock or Clock())


def fabric_message(from_node="alfheim", *, kind="publish", topic="registry.updates"):
    return {
        "version": 1,
        "message_id": str(uuid.uuid4()),
        "kind": kind,
        "from_node": from_node,
        "to_node": "midgard" if kind == "direct" else None,
        "topic": None if kind == "direct" else topic,
        "payload": {"note": "hello"},
        "seq": 1,
        "sent_at": "2026-11-01T12:00:00Z",
        "transport": "minimal-gossip",
        "simulated": True,
        "signature": None,
    }


# -- the discipline as data --------------------------------


def test_policy_is_law_and_validates():
    policy = default_policy()
    assert policy["oversized_allowed"] is False
    assert policy["throttle_on_quota"] is True
    assert policy["window_seconds"] == 60
    assert policy["max_gossip_per_window"] == 120
    assert policy["max_task_per_window"] == 60
    assert validate_policy(policy) == policy
    assert validate_policy(SMALL_POLICY) == SMALL_POLICY
    with pytest.raises(RateError):
        validate_policy({**policy, "oversized_allowed": True})
    with pytest.raises(RateError):
        validate_policy({**policy, "throttle_on_quota": False})
    with pytest.raises(RateError):
        validate_policy({**policy, "max_gossip_per_window": 0})
    with pytest.raises(RateError):
        validate_policy({**policy, "max_message_bytes": -1})


# -- inside the quotas ---------------------------------------


def test_traffic_inside_every_quota_is_admitted_and_counted():
    gate = make_gate()
    for _ in range(3):
        record = gate.admit("alfheim", "gossip", 10)
        validate_record(record)
        assert record["verdict"] == "admitted"
        assert record["reason"] is None
    assert gate.admit("alfheim", "task", 10)["verdict"] == "admitted"
    assert gate.admit("midgard", "other", 10)["verdict"] == "admitted"
    assert gate.require_admitted("midgard", "gossip", 5)["verdict"] == "admitted"
    report = gate.report()
    validate_report(report)
    assert report["seen"] == 6 and report["admitted"] == 6
    assert report["throttled"] == 0 and report["throttled_reasons"] == {}
    by_node = {n["node_id"]: n for n in report["nodes"]}
    assert by_node["alfheim"]["admitted"] == 4
    assert by_node["alfheim"]["bytes_admitted"] == 40
    assert by_node["alfheim"]["window_messages"] == 4
    assert by_node["alfheim"]["throttled_until"] is None


# -- quota breaches, named ------------------------------------


def test_gossip_quota_breach_throttles_the_sender():
    clock = Clock()
    gate = make_gate(clock)
    for _ in range(3):
        assert gate.admit("alfheim", "gossip", 10)["verdict"] == "admitted"
    breach = gate.admit("alfheim", "gossip", 10)
    assert breach["verdict"] == "throttled"
    assert breach["reason"] == "quota_gossip"
    assert gate.throttled_until("alfheim") is not None
    # while throttled, ALL of the sender's traffic is
    # throttled — tasks and small messages included
    assert gate.admit("alfheim", "task", 5)["reason"] == "throttled"
    assert gate.admit("alfheim", "gossip", 5)["reason"] == "throttled"
    with pytest.raises(RateError) as exc:
        gate.require_admitted("alfheim", "other", 5)
    assert exc.value.reason == "throttled"
    report = gate.report()
    assert report["throttled_reasons"] == {"quota_gossip": 1, "throttled": 3}
    by_node = {n["node_id"]: n for n in report["nodes"]}
    # throttled traffic entered no window
    assert by_node["alfheim"]["window_messages"] == 3


def test_task_and_total_quotas_name_their_own_breaches():
    gate = make_gate()
    gate.admit("alfheim", "task", 10)
    gate.admit("alfheim", "task", 10)
    assert gate.admit("alfheim", "task", 10)["reason"] == "quota_task"

    gate = make_gate(max_gossip_per_window=10, max_task_per_window=10)
    for _ in range(5):
        assert gate.admit("vanaheim", "other", 10)["verdict"] == "admitted"
    assert gate.admit("vanaheim", "other", 10)["reason"] == "quota_total"


def test_bytes_quota_names_its_breach():
    gate = make_gate(max_bytes_per_window=25)
    assert gate.admit("alfheim", "other", 10)["verdict"] == "admitted"
    assert gate.admit("alfheim", "other", 10)["verdict"] == "admitted"
    # 20 admitted bytes + 10 more would pass the 25-byte bound
    assert gate.admit("alfheim", "other", 10)["reason"] == "quota_bytes"
    report = gate.report()
    by_node = {n["node_id"]: n for n in report["nodes"]}
    assert by_node["alfheim"]["window_bytes"] == 20


def test_a_flooding_node_never_throttles_a_quiet_neighbour():
    gate = make_gate()
    for _ in range(4):
        gate.admit("alfheim", "gossip", 10)
    assert gate.throttled_until("alfheim") is not None
    # midgard's allowance at the same door is untouched
    assert gate.admit("midgard", "gossip", 10)["verdict"] == "admitted"
    assert gate.admit("midgard", "task", 10)["verdict"] == "admitted"
    assert gate.throttled_until("midgard") is None


# -- size discipline --------------------------------------------


def test_oversized_is_throttled_always_and_earns_no_cooldown():
    gate = make_gate()
    record = gate.admit("alfheim", "gossip", 101)
    assert record["verdict"] == "throttled"
    assert record["reason"] == "oversized"
    # exactly at the cap is admitted; the oversized
    # message earned no throttle cooldown
    assert gate.throttled_until("alfheim") is None
    assert gate.admit("alfheim", "gossip", 100)["verdict"] == "admitted"
    report = gate.report()
    assert report["throttled_reasons"] == {"oversized": 1}
    by_node = {n["node_id"]: n for n in report["nodes"]}
    assert by_node["alfheim"]["window_messages"] == 1


# -- throttle lifetime --------------------------------------------


def test_throttled_traffic_extends_neither_cooldown_nor_window():
    clock = Clock()
    gate = make_gate(clock)
    for _ in range(4):
        gate.admit("alfheim", "gossip", 10)
    until = gate.throttled_until("alfheim")
    clock.advance(5)
    for _ in range(3):
        assert gate.admit("alfheim", "gossip", 10)["reason"] == "throttled"
    assert gate.throttled_until("alfheim") == until
    report = gate.report()
    by_node = {n["node_id"]: n for n in report["nodes"]}
    assert by_node["alfheim"]["window_messages"] == 3  # admitted only


def test_recovery_after_cooldown_and_window_and_lift_is_evented():
    clock = Clock()
    gate = make_gate(clock)
    for _ in range(4):
        gate.admit("alfheim", "gossip", 10)
    assert gate.admit("alfheim", "gossip", 10)["reason"] == "throttled"
    # cooldown (20 s) outlasts the window (10 s): even
    # with an empty window the sender is still throttled
    clock.advance(15)
    assert gate.admit("alfheim", "gossip", 10)["reason"] == "throttled"
    # past both, the door admits again and events the lift
    clock.advance(6)
    assert gate.admit("alfheim", "gossip", 10)["verdict"] == "admitted"
    assert gate.throttled_until("alfheim") is None
    types = [e["type"] for e in gate.events]
    assert "rate.throttle_lifted" in types


def test_a_still_full_window_breaches_again_after_the_cooldown():
    clock = Clock()
    gate = make_gate(clock, window_seconds=60, throttle_cooldown_seconds=5)
    for _ in range(3):
        gate.admit("alfheim", "gossip", 10)
    assert gate.admit("alfheim", "gossip", 10)["reason"] == "quota_gossip"
    clock.advance(6)  # cooldown over, window still holds all 3
    assert gate.admit("alfheim", "gossip", 10)["reason"] == "quota_gossip"
    assert gate.throttled_until("alfheim") is not None


# -- fabric messages at the door -----------------------------------


def test_fabric_messages_are_classified_and_sized_by_the_stated_rule():
    gate = make_gate(max_message_bytes=100000)
    offer = fabric_message(topic="tasks.offers")
    assert classify_message(offer) == "task"
    record = gate.admit_message(offer)
    assert record["traffic_class"] == "task"
    assert record["size_bytes"] == message_size_bytes(offer)
    assert record["verdict"] == "admitted"
    news = fabric_message(topic="registry.updates")
    assert classify_message(news) == "gossip"
    assert gate.admit_message(news)["traffic_class"] == "gossip"
    direct = fabric_message(kind="direct")
    assert classify_message(direct) == "task"
    assert gate.admit_message(direct)["traffic_class"] == "task"
    # a task-topic flood spends the task quota, by name
    gate = make_gate(max_message_bytes=100000)
    big = fabric_message(topic="tasks.offers")
    big["payload"] = {"note": "x" * 10}
    for _ in range(2):
        assert gate.admit_message(big)["verdict"] == "admitted"
    assert gate.admit_message(big)["reason"] == "quota_task"


def test_malformed_judgements_are_throttled_and_counted():
    gate = make_gate()
    assert gate.admit(None, "gossip", 10)["reason"] == "malformed"
    assert gate.admit("alfheim", "shouting", 10)["reason"] == "malformed"
    assert gate.admit("alfheim", "gossip", -1)["reason"] == "malformed"
    assert gate.admit("alfheim", "gossip", True)["reason"] == "malformed"
    assert gate.admit("Not A Node!", "gossip", 10)["reason"] == "malformed"
    assert gate.admit_message("not a message")["reason"] == "malformed"
    assert gate.admit_message({"kind": "publish"})["reason"] == "malformed"
    record = gate.admit_message({"from_node": "alfheim"})
    assert record["reason"] == "malformed"
    assert record["sender"] == "alfheim"
    report = gate.report()
    validate_report(report)  # malformed senders never break the report
    assert report["throttled_reasons"] == {"malformed": 8}
    assert report["admitted"] == 0


# -- shapes, events, honesty -----------------------------------------


def test_report_records_and_events_validate():
    gate = make_gate()
    gate.admit("alfheim", "gossip", 10)
    gate.admit("alfheim", "gossip", 10**6)
    for _ in range(3):
        gate.admit("alfheim", "gossip", 10)
    report = gate.report()
    validate_report(report)
    assert report["simulated"] is True
    assert report["seen"] == report["admitted"] + report["throttled"]
    assert report["throttled_reasons"] == {"oversized": 1, "quota_gossip": 1}
    for event in gate.events:
        validate_event(event)
    types = [e["type"] for e in gate.events]
    assert "rate.message_admitted" in types
    assert "rate.message_throttled" in types
    assert "rate.node_throttled" in types


def test_readme_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
