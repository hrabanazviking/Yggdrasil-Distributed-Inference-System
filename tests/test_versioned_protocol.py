"""Versioned protocol tests — Y-065's acceptance.

The slice must prove: (a) the version word is
data — a policy naming crash-on-unknown or
no-downgrade, an unknown current or minimum, or
a minimum past the current fails by name;
(b) enrolment refuses by name — a version the
policy does not know, a preference the node
does not speak, no versions, a duplicate;
(c) negotiation settles the highest common word,
names the nodes it leaves degraded, and a pair
with no common word is a counted refusal
returned as None, never a crash; (d) a message
at a version the receiver speaks is delivered
whole and exact; (e) a newer message to an
older-only receiver is delivered degraded —
a copy at the older version, the newer fields
stripped and named, the payload untouched;
(f) traffic faults are refused by name and
counted apart, delivered nowhere, and never
raise — unknown version, retired version, a
version the sender never enrolled in, no
common word, unknown ends, and malformed
(including a message carrying a field ahead of
the version it claims); (g) a real Y-050 fabric
message feeds the gate at its own version;
(h) the report validates with sessions and
totals counted apart, events validate, no
payload content rides any record, and the
founding README is untouched.

Everything here is SIMULATED — the gate judges
in-process messages; no socket is opened and no
LAN is crossed (real carriage was Y-062's own
proof).
"""

import hashlib
import itertools
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.fabric import Fabric
from yggdrasil.observability import validate_event
from yggdrasil.versioned_protocol import (
    ProtocolError,
    ProtocolGate,
    default_policy,
    validate_message,
    validate_policy,
    validate_record,
    validate_report,
)

ROOT = Path(__file__).resolve().parents[1]
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
EPOCH = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)
IDS = itertools.count(1)


def fmt(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def message(from_node="alfheim", to_node="brage", *, version=2, **overrides):
    record = {
        "version": 1,
        "message_id": f"00000000-0000-4000-8000-{next(IDS):012d}",
        "from_node": from_node,
        "to_node": to_node,
        "protocol_version": version,
        "sent_at": fmt(EPOCH),
        "priority": "high" if version >= 2 else None,
        "hop_limit": 4 if version >= 2 else None,
        "payload": {"text": "the ravens are out"},
        "simulated": True,
    }
    record.update(overrides)
    return record


def gate_with(**enrolments):
    gate = ProtocolGate(now=lambda: EPOCH)
    for node_id, versions in enrolments.items():
        gate.enrol(node_id=node_id, versions=versions)
    return gate


# -- the word is data ------------------------------------------


def test_policy_is_data_and_the_contract_law_is_constant():
    policy = default_policy()
    assert validate_policy(policy) == policy
    assert policy["known_versions"] == [1, 2]
    assert policy["downgrade_allowed"] is True
    assert policy["unknown_version_action"] == "refuse_and_count"
    with pytest.raises(ProtocolError):
        validate_policy({**policy, "downgrade_allowed": False})
    with pytest.raises(ProtocolError):
        validate_policy({**policy, "unknown_version_action": "crash"})
    with pytest.raises(ProtocolError, match="current_version_not_known"):
        validate_policy({**policy, "current_version": 3})
    with pytest.raises(ProtocolError, match="minimum_version_not_known"):
        validate_policy({**policy, "minimum_version": 3})
    with pytest.raises(ProtocolError, match="minimum_past_current"):
        validate_policy(
            {**policy, "known_versions": [1, 2, 3], "current_version": 2, "minimum_version": 3}
        )
    with pytest.raises(ProtocolError):
        validate_message(message(simulated=False))


# -- enrolment ---------------------------------------------------


def test_enrolment_refusals_are_named():
    gate = ProtocolGate(now=lambda: EPOCH)
    with pytest.raises(ProtocolError, match="version_not_known"):
        gate.enrol("alfheim", [1, 7])
    with pytest.raises(ProtocolError, match="versions_empty"):
        gate.enrol("alfheim", [])
    with pytest.raises(ProtocolError, match="preferred_not_spoken"):
        gate.enrol("alfheim", [1], preferred=2)
    record = gate.enrol("alfheim", [2, 1])
    assert record == {"node_id": "alfheim", "versions": [1, 2], "preferred": 2}
    with pytest.raises(ProtocolError, match="already_enrolled"):
        gate.enrol("alfheim", [1])


# -- negotiation ---------------------------------------------------


def test_negotiation_picks_the_highest_common_word():
    gate = gate_with(alfheim=[1, 2], brage=[1, 2], cowrie=[1])
    assert gate.negotiate("alfheim", "brage") == 2
    assert gate.negotiate("alfheim", "cowrie") == 1
    session = gate.session("cowrie", "alfheim")
    assert session["version"] == 1
    # Cowrie prefers 1 and loses nothing; Alfheim
    # preferred 2 and works at the older word.
    assert session["degraded_nodes"] == ["alfheim"]
    report = gate.report()
    assert validate_report(report) == report
    assert report["negotiations"] == 2
    assert report["negotiations_refused"] == 0
    assert len(report["sessions"]) == 2


def test_negotiation_without_a_common_word_is_a_counted_refusal():
    gate = ProtocolGate(
        {
            "version": 1,
            "known_versions": [1, 2],
            "current_version": 2,
            "minimum_version": 2,
            "downgrade_allowed": True,
            "unknown_version_action": "refuse_and_count",
        },
        now=lambda: EPOCH,
    )
    gate.enrol("alfheim", [2])
    gate.enrol("cowrie", [1])
    # Version 1 is retired by the floor, so the pair
    # shares no admitted word — a refusal, not a crash.
    assert gate.negotiate("alfheim", "cowrie") is None
    report = gate.report()
    assert report["negotiations_refused"] == 1
    assert report["sessions"][0]["version"] is None
    with pytest.raises(ProtocolError, match="unknown_node"):
        gate.negotiate("alfheim", "ghost")


# -- delivery ---------------------------------------------------


def test_native_delivery_is_whole_and_exact():
    gate = gate_with(alfheim=[1, 2], brage=[1, 2])
    sent = message()
    record = gate.carry(sent)
    assert validate_record(record) == record
    assert record["verdict"] == "delivered"
    assert record["delivered_version"] == 2
    assert record["reason"] is None
    assert record["dropped_fields"] == []
    assert gate.inbox("brage") == [sent]
    assert gate.inbox("brage") == []


def test_newer_message_degrades_to_an_older_receiver_by_contract():
    gate = gate_with(alfheim=[1, 2], cowrie=[1])
    record = gate.carry(message(to_node="cowrie"))
    assert record["verdict"] == "degraded"
    assert record["delivered_version"] == 1
    assert record["dropped_fields"] == ["hop_limit", "priority"]
    (delivered,) = gate.inbox("cowrie")
    assert delivered["protocol_version"] == 1
    assert delivered["priority"] is None
    assert delivered["hop_limit"] is None
    # The payload is never the thing downgraded.
    assert delivered["payload"] == {"text": "the ravens are out"}
    assert validate_message(delivered) == delivered
    report = gate.report()
    assert report["degraded"] == 1
    assert report["dropped_fields"] == {"hop_limit": 1, "priority": 1}


def test_degradation_names_only_fields_actually_carried():
    gate = gate_with(alfheim=[1, 2], cowrie=[1])
    record = gate.carry(message(to_node="cowrie", hop_limit=None))
    assert record["verdict"] == "degraded"
    assert record["dropped_fields"] == ["priority"]


# -- refusals ---------------------------------------------------


def test_unknown_and_retired_versions_are_refused_and_counted():
    gate = gate_with(alfheim=[1, 2], brage=[1, 2])
    record = gate.carry(message(version=99, priority=None, hop_limit=None))
    assert record["verdict"] == "refused"
    assert record["reason"] == "unknown_version"
    assert gate.inbox("brage") == []

    retired = ProtocolGate(
        {
            "version": 1,
            "known_versions": [1, 2],
            "current_version": 2,
            "minimum_version": 2,
            "downgrade_allowed": True,
            "unknown_version_action": "refuse_and_count",
        },
        now=lambda: EPOCH,
    )
    retired.enrol("alfheim", [1, 2])
    retired.enrol("brage", [2])
    record = retired.carry(message(version=1))
    assert record["reason"] == "version_retired"
    report = retired.report()
    assert report["refused"] == 1
    assert report["refusals_by_reason"] == {"version_retired": 1}


def test_unspoken_version_and_no_common_word_are_refused():
    gate = gate_with(alfheim=[1], brage=[2])
    # Alfheim never enrolled in v2, however the
    # message is dressed.
    record = gate.carry(message())
    assert record["reason"] == "version_not_spoken"
    gate.enrol("cowrie", [2])
    # Alfheim speaks only v1 and Brage only v2:
    # no older shared word exists to degrade to.
    record = gate.carry(message(from_node="cowrie", to_node="alfheim", version=2))
    assert record["reason"] == "no_common_version"
    report = gate.report()
    assert report["refusals_by_reason"] == {
        "no_common_version": 1,
        "version_not_spoken": 1,
    }
    assert gate.inbox("alfheim") == []
    assert gate.inbox("brage") == []


def test_unknown_ends_and_malformed_traffic_are_refused_not_raised():
    gate = gate_with(alfheim=[1, 2], brage=[1, 2])
    assert gate.carry(message(from_node="ghost"))["reason"] == "unknown_sender"
    assert gate.carry(message(to_node="ghost"))["reason"] == "unknown_receiver"
    assert gate.carry({"nonsense": True})["reason"] == "malformed"
    assert gate.carry("not a message")["reason"] == "malformed"
    # A "v1" message carrying v2's priority field is
    # not a v1 message at all.
    lying = message(version=1)
    lying["priority"] = "high"
    assert gate.carry(lying)["reason"] == "malformed"
    report = gate.report()
    assert report["carried"] == 5
    assert report["refused"] == 5
    assert report["delivered"] == 0


# -- the real fabric feeds the gate -------------------------------


def test_a_real_fabric_message_is_carried_at_its_own_version():
    fabric = Fabric(now=lambda: EPOCH)
    fabric.attach("alfheim")
    fabric.attach("brage")
    sent = fabric.send("alfheim", "brage", {"text": "from the fabric"})
    fabric.inbox("brage")

    gate = gate_with(alfheim=[1, 2], brage=[1])
    record = gate.carry_fabric_message(sent)
    assert record["verdict"] == "delivered"
    assert record["message_version"] == 1
    assert record["delivered_version"] == 1
    (delivered,) = gate.inbox("brage")
    assert delivered["payload"] == {"text": "from the fabric"}

    published = fabric.publish("alfheim", "news", {"text": "to everyone"})
    # A publication has no single receiver: refused
    # malformed at this door, counted, not crashed on.
    assert gate.carry_fabric_message(published)["reason"] == "malformed"
    assert gate.carry_fabric_message({"junk": 1})["reason"] == "malformed"


# -- the door ---------------------------------------------------


def test_report_events_and_the_readme():
    gate = gate_with(alfheim=[1, 2], cowrie=[1])
    gate.negotiate("alfheim", "cowrie")
    gate.carry(message(to_node="cowrie"))
    gate.carry(message(from_node="cowrie", to_node="alfheim", version=1))
    gate.carry(message(version=99, priority=None, hop_limit=None))
    report = gate.report()
    assert validate_report(report) == report
    assert report["carried"] == 3
    assert report["delivered"] == 1
    assert report["degraded"] == 1
    assert report["refused"] == 1
    assert report["nodes"] == [
        {"node_id": "alfheim", "versions": [1, 2], "preferred": 2},
        {"node_id": "cowrie", "versions": [1], "preferred": 1},
    ]
    for event in gate.events:
        assert validate_event(event) == event
    # No payload content rides any record or report.
    assert "ravens" not in str(report)
    readme = (ROOT / "README.md").read_bytes()
    assert hashlib.md5(readme).hexdigest() == README_MD5
