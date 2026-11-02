"""Encrypted payload tests — Y-070's acceptance.

The slice must prove: (a) the law is data — a policy
allowing plaintext for sensitive kinds, naming any
other algorithm, or listing no sensitive kind fails
validation; (b) the cryptography is real — the pure-
Python X25519 agrees with the RFC 7748 test vector
and the RFC's Alice keypair, and two keypairs agree
one shared secret both ways; (c) the headline — a
sensitive task's payload is sealed to its executing
node and only that node's private key opens it,
whole and exact (nested and unicode content
included), while the envelope, the records, the
report, and the events carry no plaintext; (d) the
plain road is judged — a sensitive kind proposed for
plain carriage is refused sealing_required, a
non-sensitive kind is accepted, and sealing a
non-sensitive kind is refused kind_not_sensitive;
(e) a wrong private key, a flipped ciphertext byte,
and a tampered header each fail mac_invalid and
return nothing; a seal addressed to another node
fails recipient_mismatch at the named door; (f) two
seals of one payload differ in ephemeral key, nonce,
and ciphertext, and both open; (g) judged acts —
duplicate and full enrolments, malformed keys,
unknown doors, self-sealing, malformed tasks,
duplicate tasks, oversized payloads, and a full
sealed store — are named and counted apart; (h) a
sealed envelope rides Y-050's fabric as an ordinary
direct payload, without rewiring, and the fabric's
own report never holds the plaintext; (i) the
report, records, and events validate with honest
counts, and the founding README is untouched.

Everything here is SIMULATED in the slice's own
word — sealing is proven in-process; no socket was
opened and no LAN was crossed. The cryptography
itself is not simulated: it is the module's real
pure-Python construction, proven against RFC 7748.
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.fabric import Fabric
from yggdrasil.observability import validate_event
from yggdrasil.sealed_payloads import (
    PayloadSealer,
    SealedPayloadError,
    _x25519,
    default_policy,
    generate_sealing_keypair,
    open_sealed,
    validate_envelope,
    validate_policy,
    validate_record,
    validate_report,
)

ROOT = Path(__file__).resolve().parents[1]
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
EPOCH = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)
SECRET_TEXT = "the quiet diagnosis nobody else may read"


class Clock:
    def __call__(self):
        return EPOCH


def task(kind="health.summary", payload=None, caller="alfheim"):
    return {
        "id": str(uuid.uuid4()),
        "kind": kind,
        "caller": caller,
        "payload": payload if payload is not None else {"note": SECRET_TEXT},
    }


def door(policy=None, nodes=("alfheim", "brage", "solberg")):
    """A sealer with three enrolled nodes; returns the
    sealer and each node's private key, which the door
    itself never sees."""
    found = PayloadSealer(policy=policy, now=Clock())
    keys = {}
    for node_id in nodes:
        private, public = generate_sealing_keypair()
        assert found.enrol(node_id, public)["verdict"] == "accepted"
        keys[node_id] = private
    return found, keys


# -- the law is data ----------------------------------------------


def test_policy_law_is_schema_constants():
    policy = default_policy()
    assert policy["plaintext_for_sensitive_allowed"] is False
    assert policy["algorithm"] == "x25519-hkdf-sha256-hmac-v1"
    assert "health.summary" in policy["sensitive_kinds"]
    validate_policy(policy)
    with pytest.raises(SealedPayloadError):
        validate_policy({**policy, "plaintext_for_sensitive_allowed": True})
    with pytest.raises(SealedPayloadError):
        validate_policy({**policy, "algorithm": "rot13"})
    with pytest.raises(SealedPayloadError):
        validate_policy({**policy, "sensitive_kinds": []})
    with pytest.raises(SealedPayloadError):
        validate_policy({**policy, "sensitive_kinds": ["Health Summary"]})


# -- the cryptography is real ---------------------------------------


def test_x25519_matches_rfc7748_vectors():
    scalar = bytes.fromhex(
        "a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4"
    )
    point = bytes.fromhex(
        "e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c"
    )
    assert _x25519(scalar, point).hex() == (
        "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552"
    )
    alice_private = bytes.fromhex(
        "77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a"
    )
    base = (9).to_bytes(32, "little")
    assert _x25519(alice_private, base).hex() == (
        "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"
    )


def test_two_keypairs_agree_one_shared_secret():
    alice_private, alice_public = generate_sealing_keypair()
    brage_private, brage_public = generate_sealing_keypair()
    base = (9).to_bytes(32, "little")
    assert _x25519(bytes.fromhex(alice_private), base).hex() == alice_public
    forward = _x25519(bytes.fromhex(alice_private), bytes.fromhex(brage_public))
    back = _x25519(bytes.fromhex(brage_private), bytes.fromhex(alice_public))
    assert forward == back
    assert forward != b"\x00" * 32


# -- the headline: sealed to the executing node alone -----------------


def test_a_sensitive_payload_opens_only_at_its_executing_node():
    found, keys = door()
    payload = {
        "note": SECRET_TEXT,
        "nested": {"readings": [1, 2, 3], "unit": "µmol/L — þætte"},
        "flag": True,
    }
    record = found.seal("alfheim", "brage", task(payload=payload))
    validate_record(record)
    assert record["verdict"] == "accepted"
    assert record["ciphertext_bytes"] > 0
    envelope = found.sealed_for(record["task_id"])
    validate_envelope(envelope)
    assert envelope["sender_id"] == "alfheim"
    assert envelope["recipient_id"] == "brage"
    # The whole road carries no plaintext anywhere.
    blob = json.dumps(
        {"envelope": envelope, "report": found.report(),
         "events": found.events}
    )
    assert SECRET_TEXT not in blob
    assert "quiet diagnosis" not in json.dumps(envelope)
    # Only brage's own key opens it, whole and exact.
    opened = open_sealed(keys["brage"], envelope, recipient_id="brage")
    assert opened == payload
    # A third node's key fails the MAC and gets nothing.
    with pytest.raises(SealedPayloadError) as caught:
        open_sealed(keys["solberg"], envelope)
    assert caught.value.reason == "mac_invalid"
    # The sender's key is not the recipient's key.
    with pytest.raises(SealedPayloadError) as caught:
        open_sealed(keys["alfheim"], envelope)
    assert caught.value.reason == "mac_invalid"


def test_tampering_with_ciphertext_or_header_fails_the_mac():
    found, keys = door()
    record = found.seal("alfheim", "brage", task())
    envelope = found.sealed_for(record["task_id"])
    flipped = dict(envelope)
    raw = bytearray(bytes.fromhex(flipped["ciphertext"]))
    raw[0] ^= 1
    flipped["ciphertext"] = raw.hex()
    with pytest.raises(SealedPayloadError) as caught:
        open_sealed(keys["brage"], flipped)
    assert caught.value.reason == "mac_invalid"
    for field, value in (
        ("recipient_id", "solberg"),
        ("sender_id", "solberg"),
        ("kind", "personal.memory"),
    ):
        retargeted = dict(envelope, **{field: value})
        with pytest.raises(SealedPayloadError) as caught:
            open_sealed(keys["brage"], retargeted)
        assert caught.value.reason == "mac_invalid"
    # A named recipient check fires before any key work.
    with pytest.raises(SealedPayloadError) as caught:
        open_sealed(keys["brage"], envelope, recipient_id="solberg")
    assert caught.value.reason == "recipient_mismatch"
    # A malformed envelope is named, never a crash.
    with pytest.raises(SealedPayloadError) as caught:
        open_sealed(keys["brage"], {"version": 1})
    assert caught.value.reason == "malformed_envelope"


def test_two_seals_of_one_payload_share_nothing_but_the_payload():
    found, keys = door()
    first = found.seal("alfheim", "brage", task())
    second = found.seal("alfheim", "brage", task())
    env_one = found.sealed_for(first["task_id"])
    env_two = found.sealed_for(second["task_id"])
    assert env_one["ephemeral_public_key"] != env_two["ephemeral_public_key"]
    assert env_one["nonce"] != env_two["nonce"]
    assert env_one["ciphertext"] != env_two["ciphertext"]
    assert open_sealed(keys["brage"], env_one) == {"note": SECRET_TEXT}
    assert open_sealed(keys["brage"], env_two) == {"note": SECRET_TEXT}


# -- the plain road is judged ------------------------------------------


def test_plain_carriage_refuses_sensitive_kinds_by_name():
    found, _keys = door()
    refused = found.judge_plain("alfheim", "brage", task())
    assert refused["verdict"] == "refused"
    assert refused["reason"] == "sealing_required"
    plain = found.judge_plain(
        "alfheim", "brage", task(kind="chat.generate", payload={"prompt": "hi"})
    )
    assert plain["verdict"] == "accepted"
    report = found.report()
    assert report["plain_accepted"] == 1
    assert report["refusals_by_reason"] == {"sealing_required": 1}
    not_sensitive = found.seal(
        "alfheim", "brage", task(kind="chat.generate", payload={"prompt": "hi"})
    )
    assert not_sensitive["reason"] == "kind_not_sensitive"
    assert found.sealed_for(not_sensitive["task_id"]) is None


# -- judged acts ----------------------------------------------------------


def test_enrol_refusals_are_named():
    tight = default_policy()
    tight["max_nodes"] = 2
    found = PayloadSealer(policy=tight, now=Clock())
    _private, public = generate_sealing_keypair()
    found.enrol("alfheim", public)
    dup = found.enrol("alfheim", public)
    assert dup["reason"] == "duplicate_node"
    bad = found.enrol("brage", "not-hex")
    assert bad["reason"] == "malformed_key"
    zero = found.enrol("brage", "00" * 32)
    assert zero["reason"] == "malformed_key"
    found.enrol("brage", public)
    full = found.enrol("solberg", public)
    assert full["reason"] == "mesh_full"
    report = found.report()
    assert report["refusals_by_reason"] == {
        "duplicate_node": 1,
        "malformed_key": 2,
        "mesh_full": 1,
    }


def test_seal_refusals_are_named_and_counted_apart():
    policy = default_policy()
    policy["max_payload_bytes"] = 32
    policy["max_sealed"] = 1
    found, _keys = door(policy=policy)
    ghost_sender = found.seal("never-enrolled", "brage", task())
    assert ghost_sender["reason"] == "unknown_sender"
    ghost_recipient = found.seal("alfheim", "never-enrolled", task())
    assert ghost_recipient["reason"] == "unknown_recipient"
    own = found.seal("alfheim", "alfheim", task())
    assert own["reason"] == "self_sealing"
    broken = found.seal("alfheim", "brage", {"id": "x"})
    assert broken["reason"] == "malformed_task"
    big = found.seal(
        "alfheim", "brage", task(payload={"note": "x" * 64})
    )
    assert big["reason"] == "payload_too_large"
    small = task(payload={"n": 1})
    assert found.seal("alfheim", "brage", small)["verdict"] == "accepted"
    again = found.seal("alfheim", "solberg", small)
    assert again["reason"] == "duplicate_task"
    other = found.seal("alfheim", "brage", task(payload={"n": 2}))
    assert other["reason"] == "sealed_store_full"
    report = found.report()
    assert report["payloads_sealed"] == 1
    assert report["refusals_by_reason"] == {
        "duplicate_task": 1,
        "malformed_task": 1,
        "payload_too_large": 1,
        "sealed_store_full": 1,
        "self_sealing": 1,
        "unknown_recipient": 1,
        "unknown_sender": 1,
    }


# -- the fabric carries a sealed envelope without rewiring -----------------


def test_a_sealed_envelope_rides_the_fabric_and_opens_at_the_far_end():
    found, keys = door(nodes=("alfheim", "brage"))
    mine = task(payload={"note": SECRET_TEXT})
    record = found.seal("alfheim", "brage", mine)
    envelope = found.sealed_for(record["task_id"])
    fabric = Fabric(now=Clock())
    fabric.attach("alfheim")
    fabric.attach("brage")
    fabric.send("alfheim", "brage", envelope)
    inbox = fabric.inbox("brage")
    assert len(inbox) == 1
    carried = inbox[0]["payload"]
    validate_envelope(carried)
    assert open_sealed(keys["brage"], carried) == {"note": SECRET_TEXT}
    assert SECRET_TEXT not in json.dumps(fabric.report())
    assert SECRET_TEXT not in json.dumps(inbox[0]["payload"])


# -- counted state ---------------------------------------------------------


def test_report_records_and_events_validate_with_honest_counts():
    found, _keys = door()
    found.seal("alfheim", "brage", task())
    found.seal("alfheim", "brage", task(kind="personal.memory",
                                        payload={"note": "mine"}))
    found.seal("brage", "alfheim", task(payload={"note": "back"}))
    found.judge_plain(
        "alfheim", "brage", task(kind="chat.generate", payload={"prompt": "hi"})
    )
    report = found.report()
    validate_report(report)
    assert report["nodes_enrolled"] == 3
    assert report["payloads_sealed"] == 3
    assert report["plain_accepted"] == 1
    assert report["simulated"] is True
    by_node = {n["node_id"]: n for n in report["nodes"]}
    assert by_node["alfheim"]["sealed_sent"] == 2
    assert by_node["alfheim"]["sealed_received"] == 1
    assert by_node["brage"]["sealed_sent"] == 1
    assert by_node["brage"]["sealed_received"] == 2
    # A node's key stands only as its fingerprint.
    _private, public = generate_sealing_keypair()
    other = PayloadSealer(now=Clock())
    other.enrol("alfheim", public)
    fingerprint = other.report()["nodes"][0]["public_key_fingerprint"]
    assert fingerprint == hashlib.sha256(bytes.fromhex(public)).hexdigest()
    assert public not in json.dumps(other.report())
    for record in report["records"]:
        validate_record(record)
    for event in found.events:
        validate_event(event)
    kinds = [e["type"] for e in found.events]
    assert "sealed.node_enrolled" in kinds
    assert "sealed.payload_sealed" in kinds


def test_the_founding_readme_is_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
