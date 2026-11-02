"""Message provenance tests — Y-059's acceptance.

The slice must prove: (a) the law is data — signing
required, unsigned / wrongly-signed / replayed
traffic never admitted, Ed25519 over the SHA-256
fabric digest, and a weaker policy fails validation;
(b) enrolment is certificate-gated — a node joins
only with a Y-011 certificate that verifies against
the mesh root and names it, paired with its own key
if the gate is to sign for it; foreign-root, expired,
mismatched, and wrong-key enrolments are refused by
name; (c) traffic through a gate-wired fabric is
signed at birth with the sender's real enrolled key
(the signature verifies by hand against Y-011) and
delivered; a fabric whose signer forges or omits
signatures sees its deliveries fail at the fabric by
the name signature_invalid; (d) the door drops and
counts, by named reason: unsigned traffic, a
signature from an unenrolled sender, a forged
signature, a message tampered with after signing
(payload, sender, and sequence each), a signature
made by another enrolled node's key, malformed
traffic, and a replayed verified message — while a
failed forgery carrying a high sequence does not
advance the watermark and lock the true sender out;
(e) reports, records, and events validate, counts
are honest, and the founding README is untouched.

The cryptography here is real (Y-011's Ed25519 over
fixed test seeds); the gate is an in-process door —
no socket is opened and no LAN is crossed, and
nothing claims one was (real carriage is Y-062's).
"""

import base64
import copy
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from yggdrasil import ed25519
from yggdrasil.fabric import Fabric, message_digest
from yggdrasil.identity import (
    NodeKeypair,
    create_mesh_root,
    fingerprint,
    issue_node_certificate,
)
from yggdrasil.message_provenance import (
    ProvenanceError,
    ProvenanceGate,
    default_policy,
    validate_policy,
    validate_record,
    validate_report,
)
from yggdrasil.observability import validate_event

ROOT = Path(__file__).resolve().parents[1]
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
EPOCH = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)

SEEDS = {
    "alfheim": b"\x11" * 32,
    "midgard": b"\x22" * 32,
    "vanaheim": b"\x33" * 32,
}


class Clock:
    def __init__(self):
        self.moment = EPOCH

    def __call__(self):
        return self.moment

    def advance(self, seconds):
        self.moment += timedelta(seconds=seconds)


def make_world(now=EPOCH):
    """A mesh root, three node keypairs, and their
    certificates — the Y-011 world the gate enrols."""
    root_keypair, root_cert = create_mesh_root(
        NodeKeypair(seed=b"\x99" * 32), now=now
    )
    keypairs = {name: NodeKeypair(seed=seed) for name, seed in SEEDS.items()}
    certs = {
        name: issue_node_certificate(
            root_keypair,
            root_cert,
            node_id=name,
            node_keypair=keypairs[name],
            device_class="other",
            agent_version="0.1.0",
            now=now,
        )
        for name in SEEDS
    }
    return root_cert, keypairs, certs


def make_gate(clock=None, *, signers=("alfheim", "midgard", "vanaheim")):
    root_cert, keypairs, certs = make_world()
    gate = ProvenanceGate(root_cert, now=clock or Clock())
    for name in ("alfheim", "midgard", "vanaheim"):
        gate.enrol(
            name, certs[name], keypairs[name] if name in signers else None
        )
    return gate


def signed_message(gate, from_node="alfheim", *, seq=1, payload=None, kind="direct"):
    """One raw fabric-shaped message, signed by hand
    with the gate's own signer — traffic as it would
    arrive at the door from the wire."""
    message = {
        "version": 1,
        "message_id": str(uuid.uuid4()),
        "kind": kind,
        "from_node": from_node,
        "to_node": "midgard" if kind == "direct" else None,
        "topic": None if kind == "direct" else "registry.updates",
        "payload": {"note": "hello"} if payload is None else payload,
        "seq": seq,
        "sent_at": "2026-11-01T12:00:00Z",
        "transport": "minimal-gossip",
        "simulated": True,
        "signature": None,
    }
    message["signature"] = gate.sign_digest(from_node, message_digest(message))
    return message


# -- the law as data --------------------------------------


def test_policy_is_law_and_validates():
    policy = default_policy()
    assert policy["signing_required"] is True
    assert policy["unsigned_allowed"] is False
    assert policy["wrongly_signed_allowed"] is False
    assert policy["replay_allowed"] is False
    assert policy["algorithm"] == "ed25519"
    assert policy["digest"] == "sha256"
    assert validate_policy(policy) == policy
    with pytest.raises(ProvenanceError):
        validate_policy({**policy, "unsigned_allowed": True})
    with pytest.raises(ProvenanceError):
        validate_policy({**policy, "signing_required": False})
    with pytest.raises(ProvenanceError):
        validate_policy({**policy, "algorithm": "hmac"})


# -- enrolment: certificate-gated ----------------------------


def test_enrolment_binds_node_to_its_certified_key():
    root_cert, keypairs, certs = make_world()
    gate = ProvenanceGate(root_cert, now=Clock())
    fp = gate.enrol("alfheim", certs["alfheim"], keypairs["alfheim"])
    assert fp == fingerprint(keypairs["alfheim"].public_key)
    assert gate.signer_fingerprint("alfheim") == fp
    # verify-only enrolment: judged, never signed for
    gate.enrol("vanaheim", certs["vanaheim"])
    report = gate.report()
    by_node = {n["node_id"]: n for n in report["enrolled_nodes"]}
    assert by_node["alfheim"]["can_sign"] is True
    assert by_node["vanaheim"]["can_sign"] is False
    with pytest.raises(ProvenanceError) as exc:
        gate.sign_digest("vanaheim", "ab" * 32)
    assert exc.value.reason == "cannot_sign"
    with pytest.raises(ProvenanceError) as exc:
        gate.enrol("alfheim", certs["alfheim"], keypairs["alfheim"])
    assert exc.value.reason == "already_enrolled"


def test_enrolment_refusals_are_named():
    root_cert, keypairs, certs = make_world()
    gate = ProvenanceGate(root_cert, now=Clock())
    # a certificate naming another node
    with pytest.raises(ProvenanceError) as exc:
        gate.enrol("midgard", certs["alfheim"], keypairs["alfheim"])
    assert exc.value.reason == "node_mismatch"
    # the right certificate paired with the wrong key
    with pytest.raises(ProvenanceError) as exc:
        gate.enrol("alfheim", certs["alfheim"], keypairs["midgard"])
    assert exc.value.reason == "key_mismatch"
    # a certificate from a foreign mesh root
    other_root_keypair, other_root_cert = create_mesh_root(
        NodeKeypair(seed=b"\x77" * 32), now=EPOCH
    )
    foreign = issue_node_certificate(
        other_root_keypair,
        other_root_cert,
        node_id="vanaheim",
        node_keypair=keypairs["vanaheim"],
        device_class="other",
        agent_version="0.1.0",
        now=EPOCH,
    )
    with pytest.raises(ProvenanceError) as exc:
        gate.enrol("vanaheim", foreign, keypairs["vanaheim"])
    assert exc.value.reason == "certificate_invalid"
    # an expired certificate (issued long ago, short life)
    old_root_keypair, old_root_cert = create_mesh_root(
        NodeKeypair(seed=b"\x99" * 32), now=datetime(2025, 1, 1, tzinfo=UTC)
    )
    assert old_root_cert["public_key"] == root_cert["public_key"]
    expired = issue_node_certificate(
        old_root_keypair,
        old_root_cert,
        node_id="vanaheim",
        node_keypair=keypairs["vanaheim"],
        device_class="other",
        agent_version="0.1.0",
        lifetime_days=30,
        now=datetime(2025, 1, 1, tzinfo=UTC),
    )
    with pytest.raises(ProvenanceError) as exc:
        gate.enrol("vanaheim", expired, keypairs["vanaheim"])
    assert exc.value.reason == "certificate_invalid"


# -- signed at birth: the gate-wired fabric --------------------


def test_gate_fabric_signs_and_delivers():
    gate = make_gate()
    fabric = gate.fabric()
    fabric.attach("alfheim")
    fabric.attach("midgard")
    fabric.subscribe("midgard", "registry.updates")
    direct = fabric.send("alfheim", "midgard", {"note": "hello"})
    assert direct["signature"] is not None
    # the signature is the enrolled key's own, over the
    # digest — verified by hand, not by the gate's word
    raw = base64.b64decode(direct["signature"])
    assert ed25519.verify(
        NodeKeypair(seed=SEEDS["alfheim"]).public_key,
        message_digest(direct).encode("utf-8"),
        raw,
    )
    published = fabric.publish("alfheim", "registry.updates", {"note": "news"})
    inbox = fabric.inbox("midgard")
    assert [m["message_id"] for m in inbox] == [
        direct["message_id"],
        published["message_id"],
    ]
    assert fabric.report()["failed_deliveries"] == 0
    # and both messages pass the door, in sequence
    for message in inbox:
        assert gate.admit(message)["verdict"] == "verified"


def test_forged_or_unsigned_fabric_traffic_fails_by_name():
    gate = make_gate()
    forged = Fabric(
        signer=lambda _n, _d: base64.b64encode(b"\x00" * 64).decode(),
        verifier=gate.verifier(),
        now=Clock(),
    )
    forged.attach("alfheim")
    forged.attach("midgard")
    forged.send("alfheim", "midgard", {"note": "forged"})
    assert forged.inbox("midgard") == []  # held for retry, not delivered
    assert forged.report()["failed_deliveries"] == 1
    failed = [
        e for e in forged.events if e["type"] == "fabric.delivery_failed"
    ]
    assert failed[0]["payload"]["reason"] == "signature_invalid"

    unsigned = Fabric(verifier=gate.verifier(), now=Clock())
    unsigned.attach("alfheim")
    unsigned.attach("midgard")
    unsigned.send("alfheim", "midgard", {"note": "unsigned"})
    assert unsigned.inbox("midgard") == []
    assert unsigned.report()["failed_deliveries"] == 1


# -- the door: verified -----------------------------------------


def test_admit_verifies_a_signed_message():
    gate = make_gate()
    message = signed_message(gate)
    record = gate.admit(message)
    validate_record(record)
    assert record["verdict"] == "verified"
    assert record["reason"] is None
    assert record["from_node"] == "alfheim"
    assert record["signer_fingerprint"] == fingerprint(
        NodeKeypair(seed=SEEDS["alfheim"]).public_key
    )
    assert gate.require_verified(signed_message(gate, seq=2))["seq"] == 2
    report = gate.report()
    assert report["seen"] == 2 and report["verified"] == 2
    assert report["dropped"] == 0


# -- the door: dropped and counted --------------------------------


def test_unsigned_traffic_is_dropped_and_counted():
    gate = make_gate()
    message = signed_message(gate)
    message["signature"] = None
    record = gate.admit(message)
    assert record["verdict"] == "dropped"
    assert record["reason"] == "unsigned"
    with pytest.raises(ProvenanceError) as exc:
        gate.require_verified(signed_message(gate, seq=2) | {"signature": None})
    assert exc.value.reason == "unsigned"
    report = gate.report()
    assert report["dropped"] == 2
    assert report["dropped_reasons"] == {"unsigned": 2}


def test_unknown_signer_is_dropped_even_with_a_real_signature():
    gate = make_gate()
    # a stranger with a genuine keypair of its own,
    # signing honestly — but never enrolled, so the
    # mesh's root never bound this key to this name
    stranger = NodeKeypair(seed=b"\x44" * 32)
    raw = {
        "version": 1,
        "message_id": str(uuid.uuid4()),
        "kind": "direct",
        "from_node": "niflheim",
        "to_node": "midgard",
        "topic": None,
        "payload": {"note": "trust me"},
        "seq": 1,
        "sent_at": "2026-11-01T12:00:00Z",
        "transport": "minimal-gossip",
        "simulated": True,
        "signature": None,
    }
    raw["signature"] = base64.b64encode(
        stranger.sign(message_digest(raw).encode("utf-8"))
    ).decode()
    record = gate.admit(raw)
    assert record["verdict"] == "dropped"
    assert record["reason"] == "unknown_signer"
    assert record["signer_fingerprint"] is None


def test_wrongly_signed_traffic_is_dropped_by_name():
    gate = make_gate()
    # forged signature bytes
    forged = signed_message(gate)
    forged["signature"] = base64.b64encode(b"\x00" * 64).decode()
    assert gate.admit(forged)["reason"] == "signature_invalid"
    # payload tampered with after signing
    tampered = signed_message(gate, seq=2)
    tampered["payload"] = {"note": "rewritten"}
    assert gate.admit(tampered)["reason"] == "signature_invalid"
    # sequence tampered with after signing
    reseq = signed_message(gate, seq=3)
    reseq["seq"] = 99
    assert gate.admit(reseq)["reason"] == "signature_invalid"
    # signed by another enrolled node's key, in alfheim's name
    swapped = signed_message(gate, seq=4)
    swapped["signature"] = gate.sign_digest(
        "midgard", message_digest(swapped)
    )
    assert gate.admit(swapped)["reason"] == "signature_invalid"
    # sender rewritten to another enrolled node after signing
    renamed = signed_message(gate, seq=5)
    renamed["from_node"] = "midgard"
    assert gate.admit(renamed)["reason"] == "signature_invalid"
    # signature that is not even base64
    garbled = signed_message(gate, seq=6)
    garbled["signature"] = "not a signature!!"
    assert gate.admit(garbled)["reason"] == "signature_invalid"
    report = gate.report()
    assert report["verified"] == 0
    assert report["dropped_reasons"] == {"signature_invalid": 6}


def test_malformed_traffic_is_dropped_and_counted():
    gate = make_gate()
    assert gate.admit("not a message")["reason"] == "malformed"
    assert gate.admit({"from_node": "alfheim"})["reason"] == "malformed"
    broken = signed_message(gate)
    broken["seq"] = 0
    record = gate.admit(broken)
    assert record["reason"] == "malformed"
    assert record["from_node"] == "alfheim"  # named as it stood
    report = gate.report()
    assert report["dropped_reasons"] == {"malformed": 3}


def test_replay_is_dropped_and_forgery_cannot_lock_the_sender_out():
    gate = make_gate()
    message = signed_message(gate, seq=1)
    assert gate.admit(message)["verdict"] == "verified"
    # the same recording, word for word, speaks once only
    replay = gate.admit(copy.deepcopy(message))
    assert replay["verdict"] == "dropped"
    assert replay["reason"] == "replayed"
    # an older sequence after a newer one is a replay too
    assert gate.admit(signed_message(gate, seq=3))["verdict"] == "verified"
    assert gate.admit(signed_message(gate, seq=2))["reason"] == "replayed"
    # a forged message carrying a far-future sequence
    # fails its signature and must NOT advance the
    # watermark: the true sender's next message still
    # verifies
    forged = signed_message(gate, seq=500)
    forged["signature"] = base64.b64encode(b"\x00" * 64).decode()
    assert gate.admit(forged)["reason"] == "signature_invalid"
    assert gate.admit(signed_message(gate, seq=4))["verdict"] == "verified"
    report = gate.report()
    assert report["verified"] == 3
    assert report["dropped_reasons"] == {"replayed": 2, "signature_invalid": 1}


# -- shapes, events, honesty --------------------------------------


def test_report_records_and_events_validate():
    gate = make_gate()
    gate.admit(signed_message(gate, seq=1))
    bad = signed_message(gate, seq=2)
    bad["signature"] = None
    gate.admit(bad)
    report = gate.report()
    validate_report(report)
    assert report["simulated"] is True
    assert report["seen"] == 2
    assert [r["verdict"] for r in report["records"]] == ["verified", "dropped"]
    for event in gate.events:
        validate_event(event)
    types = [e["type"] for e in gate.events]
    assert "provenance.node_enrolled" in types
    assert "provenance.message_verified" in types
    assert "provenance.message_dropped" in types


def test_readme_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
