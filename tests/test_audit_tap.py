"""Audit tap tests — Y-071's acceptance.

The slice must prove: (a) the law is data — a
policy standing on by default, waiving consent,
allowing writes, or going quiet fails validation;
(b) the tap is born OFF — events handed to an off
tap are refused tap_off, retained nowhere, and
the feed reads empty; (c) consent is real —
turning on takes a named operator and a stated
reason, the consent stands loud in the report
while the tap is on, and switching is evented
and recorded at both ends; (d) while on, events
flow through whole and exact and in order, and a
living Y-050 fabric's own events feed the tap
without rewiring; (e) the tap is read-only in
both directions — neither a source nor a reader
can alter the retained feed through a dict it
holds; (f) its bounds are counted, never silent —
a type outside the filter is refused
type_filtered, overflow drops the oldest and
counts it, malformed events are refused by name
and the tap survives; (g) turning off keeps the
feed readable and the records whole, and the
refusals at the switch (already_on, not_on,
operator_missing) are named; (h) the report,
records, and events validate with honest counts,
and the founding README is untouched.

Everything here is SIMULATED in the slice's own
word — events are judged in-process; no socket
was opened and no LAN was crossed.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.audit_tap import (
    AuditTap,
    AuditTapError,
    default_policy,
    validate_policy,
    validate_record,
    validate_report,
)
from yggdrasil.fabric import Fabric
from yggdrasil.observability import make_event, validate_event

ROOT = Path(__file__).resolve().parents[1]
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
EPOCH = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)
OPERATOR = "volmarr"
REASON = "chasing a task that vanished mid-stream"


class Clock:
    def __call__(self):
        return EPOCH


def event(seq, type="fabric.node_attached", node_id="alfheim", payload=None):
    return make_event(
        type=type,
        node_id=node_id,
        component="fabric",
        seq=seq,
        at=EPOCH,
        payload=payload if payload is not None else {"node": node_id},
    )


def tap(policy=None, on=True):
    found = AuditTap(policy=policy, now=Clock())
    if on:
        assert found.turn_on(OPERATOR, REASON)["verdict"] == "accepted"
    return found


# -- the law is data ----------------------------------------------


def test_policy_law_is_schema_constants():
    policy = default_policy()
    assert policy["default_state"] == "off"
    assert policy["consent_required"] is True
    assert policy["read_only"] is True
    assert policy["announce_when_on"] is True
    assert policy["type_filter"] == []
    validate_policy(policy)
    for broken in (
        {"default_state": "on"},
        {"consent_required": False},
        {"read_only": False},
        {"announce_when_on": False},
        {"max_events": 0},
        {"type_filter": ["Fabric Events"]},
    ):
        with pytest.raises(AuditTapError):
            validate_policy({**policy, **broken})


# -- born off --------------------------------------------------------


def test_an_off_tap_retains_nothing():
    found = tap(on=False)
    assert found.is_on is False
    record = found.observe(event(1))
    validate_record(record)
    assert record["verdict"] == "refused"
    assert record["reason"] == "tap_off"
    assert found.feed() == []
    report = found.report()
    validate_report(report)
    assert report["state"] == "off"
    assert report["enabled_by"] is None
    assert report["events_retained"] == 0
    assert report["events_seen"] == 0
    assert report["refusals_by_reason"] == {"tap_off": 1}


# -- consent ------------------------------------------------------------


def test_turning_on_takes_a_named_operator_and_a_stated_reason():
    found = tap(on=False)
    for operator, reason in ((None, REASON), ("", REASON), (OPERATOR, None),
                             (OPERATOR, ""), (None, None)):
        record = found.turn_on(operator, reason)
        assert record["verdict"] == "refused"
        assert record["reason"] == "consent_missing"
        assert found.is_on is False
    assert found.report()["refusals_by_reason"] == {"consent_missing": 5}
    accepted = found.turn_on(OPERATOR, REASON)
    assert accepted["verdict"] == "accepted"
    assert found.is_on is True
    again = found.turn_on(OPERATOR, REASON)
    assert again["reason"] == "already_on"


def test_the_consent_stands_loud_in_the_report_while_on():
    found = tap()
    report = found.report()
    assert report["state"] == "on"
    assert report["enabled_by"] == OPERATOR
    assert report["enabled_at"] == "2026-11-02T12:00:00Z"
    assert report["consent_reason"] == REASON
    kinds = [e["type"] for e in found.events]
    assert "audit_tap.enabled" in kinds
    for own in found.events:
        validate_event(own)
    enable_records = [r for r in report["records"] if r["action"] == "enable"]
    assert enable_records[0]["operator_id"] == OPERATOR


# -- the feed ---------------------------------------------------------------


def test_events_flow_whole_exact_and_in_order():
    found = tap()
    first = event(1)
    second = event(2, type="fabric.message_queued", payload={"sender": "alfheim",
                                                             "target": "brage",
                                                             "kind": "direct"})
    third = event(3, type="fabric.node_detached", node_id="brage")
    for candidate in (first, second, third):
        assert found.observe(candidate)["verdict"] == "accepted"
    feed = found.feed()
    assert feed == [first, second, third]
    assert [e["type"] for e in found.feed(type="fabric.node_attached")] == [
        "fabric.node_attached"
    ]
    assert found.feed(limit=2) == [first, second]
    assert found.feed(limit=0) == []
    report = found.report()
    assert report["events_seen"] == 3
    assert report["events_retained"] == 3
    assert report["events_by_type"] == {
        "fabric.message_queued": 1,
        "fabric.node_attached": 1,
        "fabric.node_detached": 1,
    }
    assert report["reads"] == 4
    assert report["events_returned"] == 3 + 1 + 2 + 0


def test_the_feed_is_read_only_in_both_directions():
    found = tap()
    original = event(1, payload={"node": "alfheim", "depth": 1})
    found.observe(original)
    # The source mutating its own dict cannot reach the feed.
    original["payload"]["depth"] = 999
    original["type"] = "fabric.forged"
    held = found.feed()[0]
    assert held["type"] == "fabric.node_attached"
    assert held["payload"]["depth"] == 1
    # A reader mutating what it was handed cannot reach it either.
    held["payload"]["depth"] = 555
    held["source"]["node_id"] = "mallory"
    again = found.feed()[0]
    assert again["payload"]["depth"] == 1
    assert again["source"]["node_id"] == "alfheim"
    assert json.dumps(found.report()).find("mallory") == -1


# -- bounds, counted never silent ----------------------------------------------


def test_a_type_outside_the_filter_is_refused_by_name():
    policy = default_policy()
    policy["type_filter"] = ["fabric.node_attached"]
    found = tap(policy=policy)
    kept = found.observe(event(1))
    assert kept["verdict"] == "accepted"
    filtered = found.observe(event(2, type="fabric.node_detached"))
    assert filtered["verdict"] == "refused"
    assert filtered["reason"] == "type_filtered"
    report = found.report()
    assert report["events_retained"] == 1
    assert report["filtered_out"] == 1
    assert report["refusals_by_reason"] == {"type_filtered": 1}
    assert [e["type"] for e in found.feed()] == ["fabric.node_attached"]


def test_overflow_drops_the_oldest_and_counts_it():
    policy = default_policy()
    policy["max_events"] = 3
    found = tap(policy=policy)
    for seq in range(1, 6):
        assert found.observe(event(seq))["verdict"] == "accepted"
    report = found.report()
    assert report["events_seen"] == 5
    assert report["events_retained"] == 3
    assert report["overflow_dropped"] == 2
    assert report["events_by_type"] == {"fabric.node_attached": 3}
    assert [e["seq"] for e in found.feed()] == [3, 4, 5]


def test_malformed_events_are_refused_and_the_tap_survives():
    found = tap()
    for broken in ({}, "not-an-event", {"type": "fabric.node_attached"}, None):
        record = found.observe(broken)
        assert record["verdict"] == "refused"
        assert record["reason"] == "malformed_event"
    assert found.observe(event(1))["verdict"] == "accepted"
    report = found.report()
    assert report["events_retained"] == 1
    assert report["refusals_by_reason"] == {"malformed_event": 4}


# -- the switch, at both ends ----------------------------------------------------


def test_turning_off_keeps_the_feed_and_the_records():
    found = tap()
    found.observe(event(1))
    nameless = found.turn_off("")
    assert nameless["reason"] == "operator_missing"
    assert found.is_on is True
    off = found.turn_off(OPERATOR)
    assert off["verdict"] == "accepted"
    assert found.is_on is False
    again = found.turn_off(OPERATOR)
    assert again["reason"] == "not_on"
    # Nothing observed after the switch is retained...
    assert found.observe(event(2))["reason"] == "tap_off"
    # ...but what was gathered stays readable, and the
    # whole consent history stands in the records.
    assert [e["seq"] for e in found.feed()] == [1]
    report = found.report()
    assert report["state"] == "off"
    assert report["enabled_by"] is None
    assert report["consent_reason"] is None
    actions = [r["action"] for r in report["records"]]
    assert actions == ["enable", "observe", "disable", "disable", "disable",
                       "observe"]
    kinds = [e["type"] for e in found.events]
    assert "audit_tap.disabled" in kinds
    # And the tap can be consented on again, with a new reason standing.
    assert found.turn_on(OPERATOR, "second look")["verdict"] == "accepted"
    assert found.report()["consent_reason"] == "second look"
    assert [e["seq"] for e in found.feed()] == [1]


# -- a living fabric feeds the tap without rewiring -------------------------------


def test_a_living_fabrics_events_feed_the_tap():
    fabric = Fabric(now=Clock())
    fabric.attach("alfheim")
    fabric.attach("brage")
    fabric.send("alfheim", "brage", {"note": "hello"})
    fabric.detach("brage")
    fabric.send("alfheim", "brage", {"note": "still there?"})
    fabric_events = fabric.events
    assert fabric_events  # attach/detach/queued events exist to be tapped
    found = tap()
    records = found.observe_all(fabric_events)
    assert all(r["verdict"] == "accepted" for r in records)
    feed = found.feed()
    assert feed == fabric_events
    types = [e["type"] for e in feed]
    assert "fabric.node_attached" in types
    assert "fabric.message_queued" in types
    report = found.report()
    assert report["events_seen"] == len(fabric_events)
    assert report["events_retained"] == len(fabric_events)
    # The fabric itself was never touched by being read.
    assert fabric.events == fabric_events


# -- counted state -------------------------------------------------------------------


def test_report_records_and_events_validate_with_honest_counts():
    found = tap(on=False)
    found.observe(event(1))                       # tap_off
    found.turn_on(OPERATOR, REASON)
    found.observe(event(1))
    found.observe(event(2, type="fabric.outbox_flushed",
                        payload={"node": "alfheim", "delivered": 1,
                                 "remaining": 0}))
    found.turn_off(OPERATOR)
    report = found.report()
    validate_report(report)
    assert report["simulated"] is True
    assert report["events_seen"] == 2
    assert report["events_retained"] == 2
    assert report["events_by_type"] == {
        "fabric.node_attached": 1,
        "fabric.outbox_flushed": 1,
    }
    assert report["refusals_by_reason"] == {"tap_off": 1}
    for record in report["records"]:
        validate_record(record)
    for own in found.events:
        validate_event(own)


def test_the_founding_readme_is_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
