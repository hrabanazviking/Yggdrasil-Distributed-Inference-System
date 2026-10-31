"""Fabric core tests — Y-050's acceptance.

The fabric must prove: (a) one API serves both shapes —
publish/subscribe delivers a validated message to every
subscriber (a subscribed publisher hears itself), and a
direct send reaches exactly its named node, through a
handler or an inbox; (b) the chosen transport's
store-and-forward honesty — mail for a detached or
never-attached node waits in the sender's outbox and is
flushed in order on attach, a full outbox refuses the new
message by name, and nothing is silently dropped;
(c) partition honesty in the open — a cut mesh delivers
within each side, holds cross-seam traffic, and heals
with zero loss, the Y-049 criterion-1 rules at API
scale; (d) failures are counted, never hidden — a
raising handler fails that one delivery (the rest of a
topic still served), a failed direct delivery returns to
the outbox and a later flush lands it; (e) the signing
seam — a signer signs the message digest, a verifier's
rejection is a named failed delivery; (f) discipline —
payloads are JSON-copied (later mutation cannot rewrite
a carried message), oversize and non-JSON payloads and
every misuse (unattached sender, duplicate attach, bad
topic, unknown detach) refused by name, per-sender seq
rises in order; (g) the report validates with exact
counts and the events are validated Y-013 fabric events
carrying names and counts only; (h) the founding README
is untouched.

Everything here is in-process and SIMULATED: no socket
was opened and no LAN crossed.
"""

import copy
import hashlib
import itertools
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.fabric import (
    Fabric,
    FabricError,
    default_policy,
    message_digest,
    validate_message,
    validate_policy,
    validate_report,
)
from yggdrasil.observability import validate_event

ROOT = Path(__file__).resolve().parents[1]
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
NOW = datetime(2026, 10, 31, 14, 0, tzinfo=UTC)


def make_fabric(**kwargs):
    counter = itertools.count(1)
    kwargs.setdefault("now", lambda: NOW)
    kwargs.setdefault("id_factory", lambda: str(uuid.UUID(int=next(counter))))
    return Fabric(**kwargs)


def collector(box):
    return lambda message: box.append(message)


# -- the one API: publish/subscribe -------------------------------


def test_publish_reaches_every_subscriber_as_validated_message():
    fabric = make_fabric()
    a, b, c = [], [], []
    fabric.attach("node-a", collector(a))
    fabric.attach("node-b", collector(b))
    fabric.attach("node-c", collector(c))
    fabric.subscribe("node-a", "registry.updates")
    fabric.subscribe("node-b", "registry.updates")
    message = fabric.publish("node-c", "registry.updates", {"node": "node-c"})
    assert validate_message(message) is message
    assert message["kind"] == "publish"
    assert message["topic"] == "registry.updates"
    assert message["to_node"] is None
    assert message["transport"] == "minimal-gossip"
    assert message["simulated"] is True
    assert [m["message_id"] for m in a] == [message["message_id"]]
    assert [m["message_id"] for m in b] == [message["message_id"]]
    assert c == []  # the publisher was not subscribed


def test_subscribed_publisher_hears_its_own_publication():
    fabric = make_fabric()
    heard = []
    fabric.attach("node-a", collector(heard))
    fabric.subscribe("node-a", "health.beats")
    fabric.publish("node-a", "health.beats", {"beat": 1})
    assert len(heard) == 1


def test_unsubscribe_stops_delivery_and_unattached_hear_nothing():
    fabric = make_fabric()
    heard = []
    fabric.attach("node-a", collector(heard))
    fabric.attach("node-b")
    fabric.subscribe("node-a", "tasks.offers")
    fabric.unsubscribe("node-a", "tasks.offers")
    fabric.publish("node-b", "tasks.offers", {"task": 1})
    assert heard == []
    with pytest.raises(FabricError, match="not subscribed"):
        fabric.unsubscribe("node-a", "tasks.offers")


def test_handlerless_node_reads_its_inbox_in_order():
    fabric = make_fabric()
    fabric.attach("node-a")
    fabric.attach("node-b")
    fabric.send("node-b", "node-a", {"n": 1})
    fabric.send("node-b", "node-a", {"n": 2})
    inbox = fabric.inbox("node-a")
    assert [m["payload"] for m in inbox] == [{"n": 1}, {"n": 2}]
    assert fabric.inbox("node-a") == []  # drained


# -- the one API: direct channels ---------------------------------


def test_direct_send_reaches_exactly_the_named_node():
    fabric = make_fabric()
    a, b = [], []
    fabric.attach("node-a", collector(a))
    fabric.attach("node-b", collector(b))
    message = fabric.send("node-a", "node-b", {"hello": "b"})
    assert message["kind"] == "direct"
    assert message["to_node"] == "node-b"
    assert message["topic"] is None
    assert [m["payload"] for m in b] == [{"hello": "b"}]
    assert a == []


def test_per_sender_seq_rises_across_both_shapes():
    fabric = make_fabric()
    heard = []
    fabric.attach("node-a")
    fabric.attach("node-b", collector(heard))
    fabric.subscribe("node-b", "tasks.offers")
    fabric.publish("node-a", "tasks.offers", {})
    fabric.send("node-a", "node-b", {})
    fabric.publish("node-a", "tasks.offers", {})
    assert [m["seq"] for m in heard] == [1, 2, 3]


# -- store-and-forward ---------------------------------------------


def test_mail_for_detached_node_waits_and_flushes_on_attach():
    fabric = make_fabric()
    fabric.attach("node-a")
    fabric.send("node-a", "node-b", {"n": 1})
    fabric.send("node-a", "node-b", {"n": 2})
    assert [m["payload"] for m in fabric.outbox("node-a")] == [{"n": 1}, {"n": 2}]
    heard = []
    fabric.attach("node-b", collector(heard))
    assert [m["payload"] for m in heard] == [{"n": 1}, {"n": 2}]
    assert fabric.outbox("node-a") == []
    report = fabric.report()
    assert report["delivered"] == 2
    assert report["queued"] == 0


def test_detach_holds_new_mail_and_subscriptions_do_not_survive():
    fabric = make_fabric()
    heard = []
    fabric.attach("node-a", collector(heard))
    fabric.attach("node-b")
    fabric.subscribe("node-a", "registry.updates")
    fabric.detach("node-a")
    fabric.send("node-b", "node-a", {"while": "gone"})
    assert len(fabric.outbox("node-b")) == 1
    fabric.attach("node-a", collector(heard))
    assert [m["payload"] for m in heard] == [{"while": "gone"}]
    # The subscription went with the detach: a fresh
    # publication is not delivered until resubscribed.
    fabric.publish("node-b", "registry.updates", {"n": 1})
    assert len(heard) == 1


def test_full_outbox_refuses_new_message_by_name():
    fabric = make_fabric(
        policy={"version": 1, "max_outbox_messages": 2, "max_payload_bytes": 1024}
    )
    fabric.attach("node-a")
    fabric.send("node-a", "node-b", {"n": 1})
    fabric.send("node-a", "node-b", {"n": 2})
    with pytest.raises(FabricError, match="policy bound"):
        fabric.send("node-a", "node-b", {"n": 3})
    assert len(fabric.outbox("node-a")) == 2  # nothing dropped, nothing added


# -- partition honesty -----------------------------------------------


def test_cut_mesh_delivers_within_sides_and_heals_with_zero_loss():
    fabric = make_fabric()
    boxes = {name: [] for name in ("node-a", "node-b", "node-c")}
    for name, box in boxes.items():
        fabric.attach(name, collector(box))
    for name in boxes:
        fabric.subscribe(name, "registry.updates")
    fabric.cut(["node-c"])
    assert fabric.report()["partitioned"] is True
    # Within a side: delivered at once.
    fabric.send("node-a", "node-b", {"side": "main"})
    assert [m["payload"] for m in boxes["node-b"]] == [{"side": "main"}]
    # Across the seam: direct mail held, publication held
    # for the cut-off subscriber only.
    fabric.send("node-a", "node-c", {"across": 1})
    fabric.publish("node-a", "registry.updates", {"across": 2})
    assert boxes["node-c"] == []
    assert [m["payload"] for m in boxes["node-b"]] == [
        {"side": "main"},
        {"across": 2},
    ]
    assert fabric.report()["queued"] == 2
    delivered = fabric.heal()
    assert delivered == 2
    assert [m["payload"] for m in boxes["node-c"]] == [{"across": 1}, {"across": 2}]
    report = fabric.report()
    assert report["partitioned"] is False
    assert report["queued"] == 0
    # Nothing lost, nothing duplicated: a+b+c heard the
    # publication exactly once each (a as subscribed
    # publisher), plus the two direct messages.
    assert report["delivered"] == 5


# -- failures counted, never hidden -----------------------------------


def test_raising_handler_fails_one_delivery_and_topic_still_served():
    fabric = make_fabric()
    good = []

    def bad(_message):
        raise RuntimeError("boom")

    fabric.attach("node-a", bad)
    fabric.attach("node-b", collector(good))
    fabric.attach("node-c")
    fabric.subscribe("node-a", "tasks.offers")
    fabric.subscribe("node-b", "tasks.offers")
    fabric.publish("node-c", "tasks.offers", {"task": 1})
    assert len(good) == 1
    report = fabric.report()
    assert report["failed_deliveries"] == 1
    assert report["delivered"] == 1
    failed = [e for e in fabric.events if e["type"] == "fabric.delivery_failed"]
    assert len(failed) == 1
    assert failed[0]["payload"]["reason"] == "handler_error"
    assert failed[0]["payload"]["target"] == "node-a"


def test_failed_direct_delivery_returns_to_outbox_and_flush_lands_it():
    fabric = make_fabric()
    state = {"fail": True}
    heard = []

    def flaky(message):
        if state["fail"]:
            raise RuntimeError("not yet")
        heard.append(message)

    fabric.attach("node-a")
    fabric.attach("node-b", flaky)
    fabric.send("node-a", "node-b", {"retry": "me"})
    assert heard == []
    assert len(fabric.outbox("node-a")) == 1
    state["fail"] = False
    assert fabric.flush("node-a") == 1
    assert [m["payload"] for m in heard] == [{"retry": "me"}]
    assert fabric.outbox("node-a") == []


# -- the signing seam ----------------------------------------------------


def _signer(node_id, digest):
    return f"sig:{node_id}:{digest[:12]}"


def _verifier(node_id, digest, signature):
    return signature == _signer(node_id, digest)


def test_signer_signs_the_digest_and_verifier_accepts():
    fabric = make_fabric(signer=_signer, verifier=_verifier)
    heard = []
    fabric.attach("node-a")
    fabric.attach("node-b", collector(heard))
    message = fabric.send("node-a", "node-b", {"signed": True})
    assert message["signature"] == _signer("node-a", message_digest(message))
    assert len(heard) == 1
    # The digest is a pure function of the message's own
    # fields: tampering with the payload breaks it.
    tampered = copy.deepcopy(message)
    tampered["payload"] = {"signed": False}
    assert message_digest(tampered) != message_digest(message)


def test_verifier_rejection_is_a_named_failed_delivery_held_for_retry():
    fabric = make_fabric(signer=lambda _n, _d: "forged", verifier=_verifier)
    heard = []
    fabric.attach("node-a")
    fabric.attach("node-b", collector(heard))
    fabric.send("node-a", "node-b", {"signed": True})
    assert heard == []
    report = fabric.report()
    assert report["failed_deliveries"] == 1
    assert report["queued"] == 1  # held in the outbox, not lost
    failed = [e for e in fabric.events if e["type"] == "fabric.delivery_failed"]
    assert failed[0]["payload"]["reason"] == "signature_invalid"


def test_unsigned_message_fails_a_verifying_fabric():
    fabric = make_fabric(verifier=_verifier)
    fabric.attach("node-a")
    fabric.attach("node-b")
    fabric.send("node-a", "node-b", {"signed": False})
    assert fabric.report()["failed_deliveries"] == 1
    assert fabric.inbox("node-b") == []


# -- discipline -------------------------------------------------------------


def test_payload_is_copied_so_later_mutation_cannot_rewrite_it():
    fabric = make_fabric()
    heard = []
    fabric.attach("node-a")
    fabric.attach("node-b", collector(heard))
    payload = {"nested": {"n": 1}}
    fabric.send("node-a", "node-b", payload)
    payload["nested"]["n"] = 999
    assert heard[0]["payload"] == {"nested": {"n": 1}}


def test_non_json_and_oversize_payloads_refused_by_name():
    fabric = make_fabric(
        policy={"version": 1, "max_outbox_messages": 4, "max_payload_bytes": 16}
    )
    fabric.attach("node-a")
    fabric.attach("node-b")
    with pytest.raises(FabricError, match="not JSON data"):
        fabric.send("node-a", "node-b", {"bad": object()})
    with pytest.raises(FabricError, match="exceeds the policy bound"):
        fabric.send("node-a", "node-b", {"big": "x" * 64})
    assert fabric.report()["sent_direct"] == 0


def test_misuse_refused_by_name():
    fabric = make_fabric()
    with pytest.raises(FabricError, match="not attached"):
        fabric.publish("node-a", "tasks.offers", {})
    with pytest.raises(FabricError, match="not attached"):
        fabric.send("node-a", "node-b", {})
    fabric.attach("node-a")
    with pytest.raises(FabricError, match="already attached"):
        fabric.attach("node-a")
    with pytest.raises(FabricError, match="not attached"):
        fabric.detach("node-b")
    with pytest.raises(FabricError, match="topic"):
        fabric.subscribe("node-a", "Bad Topic!")
    with pytest.raises(FabricError, match="node_id"):
        fabric.attach("  ")
    with pytest.raises(FabricError, match="at least one node"):
        fabric.cut([])


def test_default_policy_is_validated_data():
    policy = default_policy()
    assert validate_policy(policy) is policy
    assert policy["max_outbox_messages"] >= 1
    assert policy["max_payload_bytes"] >= 1


# -- report and events -------------------------------------------------------


def test_report_counts_exactly_and_validates():
    fabric = make_fabric()
    fabric.attach("node-a")
    fabric.attach("node-b")
    fabric.subscribe("node-b", "registry.updates")
    fabric.subscribe("node-a", "registry.updates")
    fabric.publish("node-a", "registry.updates", {"n": 1})
    fabric.send("node-a", "node-b", {"n": 2})
    fabric.send("node-a", "node-c", {"n": 3})
    report = fabric.report()
    assert validate_report(report) is report
    assert report["attached_nodes"] == ["node-a", "node-b"]
    assert report["topics"] == {"registry.updates": 2}
    assert report["published"] == 1
    assert report["sent_direct"] == 2
    assert report["delivered"] == 3  # publication x2 + direct x1
    assert report["queued"] == 1
    assert report["outbox_depths"] == {"node-a": 1}
    assert report["transport"] == "minimal-gossip"
    assert report["simulated"] is True


def test_events_are_validated_fabric_events_with_names_and_counts_only():
    fabric = make_fabric()
    fabric.attach("node-a")
    fabric.send("node-a", "node-b", {"secret-content": "never in an event"})
    fabric.attach("node-b")
    types = [e["type"] for e in fabric.events]
    assert types == [
        "fabric.node_attached",
        "fabric.message_queued",
        "fabric.node_attached",
        "fabric.outbox_flushed",
    ]
    for event in fabric.events:
        assert validate_event(event) is event
        assert event["source"]["component"] == "fabric"
        assert "secret-content" not in str(event["payload"])


def test_replay_is_deterministic_with_injected_clock_and_ids():
    def run():
        fabric = make_fabric()
        fabric.attach("node-a")
        fabric.attach("node-b")
        fabric.subscribe("node-b", "tasks.offers")
        first = fabric.publish("node-a", "tasks.offers", {"n": 1})
        second = fabric.send("node-a", "node-b", {"n": 2})
        return first, second, fabric.report()

    first1, second1, report1 = run()
    first2, second2, report2 = run()
    assert first1 == first2
    assert second1 == second2
    assert report1 == report2


def test_readme_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
