"""Encrypted payloads for sensitive task kinds — Y-070.

Y-051 seals the *channel*: every fabric connection is
mutual TLS, so a stranger on the wire reads nothing.
But a channel ends at the node that terminates it —
a task offered on the fabric, relayed, claimed, or
held store-and-forward (Y-061) can be read by every
hand it passes through. This slice seals the
*content* itself, kept by :class:`PayloadSealer`:
where a task's policy demands it — the kinds named
sensitive in the ``sealed_payload_policy`` v1 — the
payload travels sealed to the executing node alone.

- **The law is schema constants.** The policy fixes
  ``plaintext_for_sensitive_allowed: false`` and one
  named algorithm, ``x25519-hkdf-sha256-hmac-v1``.
  ``judge_plain`` is the door plain carriage must
  pass: a non-sensitive kind is accepted, a sensitive
  kind is refused ``sealing_required`` — counted,
  never quietly carried in the clear.
- **Sealing is an ephemeral exchange to the
  recipient's key.** Each node enrols a *sealing*
  keypair — X25519, distinct from its Y-011 Ed25519
  signing identity — publishing only the public key;
  its private key never enters this door (opening is
  a standalone act, :func:`open_sealed`, performed by
  the executing node with its own key). Each seal
  generates a fresh ephemeral keypair, agrees a
  shared secret with the recipient's public key,
  separates an encryption key and a MAC key from it
  with HKDF-SHA256 (salted by the seal's nonce, the
  header bound into the info), encrypts the payload's
  canonical JSON with an HMAC-SHA256 counter
  keystream, and authenticates header + ciphertext
  encrypt-then-MAC. Two seals of one payload share
  nothing but the payload.
- **Only the executing node can open.** Opening
  re-derives the shared secret from the recipient's
  private key and the envelope's ephemeral public
  key, verifies the MAC with a constant-time compare
  *before* a single byte is decrypted, and only then
  recovers the payload whole and exact. A wrong key,
  a flipped ciphertext byte, or a tampered header
  field (task, kind, either end) all fail the same
  named way — ``mac_invalid`` — and nothing is
  returned. A small-order ephemeral key that would
  agree an all-zero secret is refused
  ``invalid_key``.
- **Records carry no content.** Enrolments, seals,
  and plain judgements are judged by named record —
  ``unknown_sender``, ``unknown_recipient``,
  ``self_sealing``, ``duplicate_task``,
  ``kind_not_sensitive``, ``payload_too_large``,
  ``malformed_task``, ``malformed_key``, and the
  rest, counted apart. A node's key stands in the
  report only as its SHA-256 fingerprint; no record,
  report, or event ever carries plaintext,
  ciphertext, or key material.
- **Real tasks feed the door without rewiring.** A
  Y-007 task envelope is validated whole before it
  is judged; the sealed envelope it produces is a
  plain JSON dict, so Y-050's fabric carries it as
  an ordinary direct payload — the suite proves the
  full road: seal → fabric → inbox → open, with the
  fabric's own report never holding the plaintext.

Honesty: everything here is SIMULATED —
``simulated: true`` is a schema constant on the
envelope, the record, and the report. The sealing
construction itself is real and proven in-process
against the RFC 7748 vectors — it is not simulated —
but it is pure Python in this module's own code
(zero native dependencies, Y-011's precedent), NOT
constant-time, and must never be presented as
side-channel resistant or externally audited; if a
measured need for hardened sealing appears, a
compiled path replaces this module behind its
interface, as ADR-001 governs for signing. No socket
was opened and no LAN was crossed in this slice
(real carriage was Y-062's proof); an envelope's
header — task id, kind, both node ids, sizes, time —
travels in the clear as metadata, so traffic
analysis (threat T-06's other half) is not answered
here and is not claimed.
"""

import copy
import hashlib
import hmac
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.observability import make_event

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

ALGORITHM = "x25519-hkdf-sha256-hmac-v1"
_BASE_POINT = (9).to_bytes(32, "little")


def _load_schema(name: str) -> dict:
    return json.loads((_SCHEMAS / name).read_text())


def _validator(name: str):
    schema = _load_schema(name)
    resources = []
    for path in sorted(_SCHEMAS.glob("*.json")):
        other = json.loads(path.read_text())
        resource = Resource.from_contents(other)
        resources.append((path.name, resource))
        resources.append((other["$id"], resource))
        resources.append((other["$id"].rsplit("/", 1)[0] + "/" + path.name, resource))
    registry = Registry().with_resources(resources)
    return validators.validator_for(schema)(schema, registry=registry)


_POLICY_VALIDATOR = _validator("sealed_payload_policy.schema.json")
_ENVELOPE_VALIDATOR = _validator("sealed_payload_envelope.schema.json")
_RECORD_VALIDATOR = _validator("sealed_payload_record.schema.json")
_REPORT_VALIDATOR = _validator("sealed_payload_report.schema.json")
_TASK_VALIDATOR = _validator("task_envelope.schema.json")


class SealedPayloadError(Exception):
    """The sealing door's own failure: a policy,
    envelope, record, or report that fails its schema,
    or an opening that cannot be made — a tampered or
    wrongly-keyed envelope, carried in ``reason``.
    (Judged acts at the door are refused by named
    record instead; see ``seal`` and ``judge_plain``.)"""

    def __init__(self, message: str, *, reason: str | None = None):
        super().__init__(message)
        self.reason = reason


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record, kind: str):
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise SealedPayloadError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_policy(record: dict) -> dict:
    return _validate(_POLICY_VALIDATOR, record, "sealed payload policy")


def validate_envelope(record: dict) -> dict:
    return _validate(_ENVELOPE_VALIDATOR, record, "sealed payload envelope")


def validate_record(record: dict) -> dict:
    return _validate(_RECORD_VALIDATOR, record, "sealed payload record")


def validate_report(record: dict) -> dict:
    return _validate(_REPORT_VALIDATOR, record, "sealed payload report")


def validate_task(record: dict) -> dict:
    return _validate(_TASK_VALIDATOR, record, "task envelope")


def default_policy() -> dict:
    """Sealing as data: the kinds an operator names
    sensitive (personal and health-bearing task
    content), one payload up to the fabric's own
    256 KiB bound, at most 1,024 sealed envelopes
    held by one door, and at most 32 nodes enrolled.
    A policy, labelled as one — an operator's list
    and numbers, not facts about any task."""
    return validate_policy(
        {
            "version": 1,
            "algorithm": ALGORITHM,
            "plaintext_for_sensitive_allowed": False,
            "sensitive_kinds": ["chat.private", "health.summary", "personal.memory"],
            "max_payload_bytes": 262_144,
            "max_sealed": 1024,
            "max_nodes": 32,
        }
    )


# -- X25519 (RFC 7748), pure Python --------------------------------
#
# The Montgomery ladder over Curve25519, in the same
# spirit as yggdrasil.ed25519: zero native
# dependencies so every node class can seal, and the
# same honest limit — this is NOT constant-time
# (the ladder's swaps branch on key bits) and must
# never be presented as side-channel resistant.

_P = 2**255 - 19
_A24 = 121665


def _x25519(private: bytes, public: bytes) -> bytes:
    if len(private) != 32 or len(public) != 32:
        raise ValueError("x25519 inputs must be 32 bytes")
    clamped = bytearray(private)
    clamped[0] &= 248
    clamped[31] &= 127
    clamped[31] |= 64
    scalar = int.from_bytes(clamped, "little")
    x1 = int.from_bytes(public, "little") & ((1 << 255) - 1)
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in range(254, -1, -1):
        bit = (scalar >> t) & 1
        swap ^= bit
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = bit
        a = (x2 + z2) % _P
        aa = a * a % _P
        b = (x2 - z2) % _P
        bb = b * b % _P
        e = (aa - bb) % _P
        c = (x3 + z3) % _P
        d = (x3 - z3) % _P
        da = d * a % _P
        cb = c * b % _P
        x3 = (da + cb) ** 2 % _P
        z3 = x1 * ((da - cb) ** 2 % _P) % _P
        x2 = aa * bb % _P
        z2 = e * ((aa + _A24 * e) % _P) % _P
    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


def generate_sealing_keypair() -> tuple[str, str]:
    """A fresh X25519 sealing keypair as (private,
    public) hex. The private half is the executing
    node's own custody: it is returned to the caller
    and never enters the sealing door."""
    private = os.urandom(32)
    return private.hex(), _x25519(private, _BASE_POINT).hex()


def _hkdf(shared: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, shared, hashlib.sha256).digest()
    okm = b""
    block = b""
    counter = 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        okm += block
        counter += 1
    return okm[:length]


def _canonical(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _header(envelope: dict) -> dict:
    return {
        key: envelope[key]
        for key in (
            "version", "task_id", "kind", "sender_id", "recipient_id",
            "algorithm", "ephemeral_public_key", "nonce", "payload_bytes",
        )
    }


def _derive_keys(shared: bytes, envelope: dict) -> tuple[bytes, bytes]:
    header = _canonical(_header(envelope))
    salt = bytes.fromhex(envelope["nonce"])
    enc_key = _hkdf(shared, salt, b"yggdrasil/sealed-payload/v1/enc/" + header, 32)
    mac_key = _hkdf(shared, salt, b"yggdrasil/sealed-payload/v1/mac/" + header, 32)
    return enc_key, mac_key


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < length:
        out += hmac.new(
            key, nonce + counter.to_bytes(8, "big"), hashlib.sha256
        ).digest()
        counter += 1
    return out[:length]


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream, strict=True))


def _seal_bytes(
    sender_id: str, recipient_id: str, task: dict, recipient_public: bytes, *, at: str
) -> dict:
    plaintext = _canonical(task["payload"])
    ephemeral_private = os.urandom(32)
    ephemeral_public = _x25519(ephemeral_private, _BASE_POINT)
    shared = _x25519(ephemeral_private, recipient_public)
    if shared == b"\x00" * 32:
        raise SealedPayloadError(
            "recipient sealing key agrees an all-zero secret",
            reason="malformed_key",
        )
    envelope = {
        "version": 1,
        "task_id": task["id"],
        "kind": task["kind"],
        "sender_id": sender_id,
        "recipient_id": recipient_id,
        "algorithm": ALGORITHM,
        "ephemeral_public_key": ephemeral_public.hex(),
        "nonce": os.urandom(16).hex(),
        "ciphertext": "",
        "mac": "",
        "payload_bytes": len(plaintext),
        "sealed_at": at,
        "simulated": True,
    }
    enc_key, mac_key = _derive_keys(shared, envelope)
    nonce = bytes.fromhex(envelope["nonce"])
    ciphertext = _xor(plaintext, _keystream(enc_key, nonce, len(plaintext)))
    envelope["ciphertext"] = ciphertext.hex()
    envelope["mac"] = hmac.new(
        mac_key, _canonical(_header(envelope)) + b"." + ciphertext, hashlib.sha256
    ).hexdigest()
    return validate_envelope(envelope)


def open_sealed(private_key_hex, envelope, *, recipient_id: str | None = None) -> dict:
    """Open one sealed envelope with the executing
    node's own private sealing key — the only road
    back to the payload. The MAC is verified before
    any decryption; a wrong key, tampered ciphertext,
    or tampered header fails ``mac_invalid`` and
    nothing is returned. The key is used here and
    never stored. Returns the task's payload whole
    and exact."""
    try:
        checked = validate_envelope(copy.deepcopy(envelope))
    except SealedPayloadError as error:
        raise SealedPayloadError(
            f"sealed envelope fails its schema: {error}", reason="malformed_envelope"
        ) from error
    if recipient_id is not None and recipient_id != checked["recipient_id"]:
        raise SealedPayloadError(
            f"envelope is sealed to {checked['recipient_id']}, not {recipient_id}",
            reason="recipient_mismatch",
        )
    try:
        private = bytes.fromhex(private_key_hex)
        ephemeral_public = bytes.fromhex(checked["ephemeral_public_key"])
        ciphertext = bytes.fromhex(checked["ciphertext"])
        nonce = bytes.fromhex(checked["nonce"])
    except (TypeError, ValueError) as error:
        raise SealedPayloadError(
            "sealed envelope or key is not hex", reason="malformed_envelope"
        ) from error
    if len(private) != 32:
        raise SealedPayloadError(
            "private sealing key must be 32 bytes", reason="malformed_key"
        )
    shared = _x25519(private, ephemeral_public)
    if shared == b"\x00" * 32:
        raise SealedPayloadError(
            "ephemeral key agrees an all-zero secret", reason="invalid_key"
        )
    enc_key, mac_key = _derive_keys(shared, checked)
    expected = hmac.new(
        mac_key, _canonical(_header(checked)) + b"." + ciphertext, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, checked["mac"]):
        raise SealedPayloadError(
            "sealed payload fails its MAC — tampered, or not sealed to this key",
            reason="mac_invalid",
        )
    plaintext = _xor(ciphertext, _keystream(enc_key, nonce, len(ciphertext)))
    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SealedPayloadError(
            "sealed payload does not decode", reason="mac_invalid"
        ) from error
    if not isinstance(payload, dict) or len(plaintext) != checked["payload_bytes"]:
        raise SealedPayloadError(
            "sealed payload does not match its envelope", reason="mac_invalid"
        )
    return payload


class PayloadSealer:
    """The sealing door: enrolments of node sealing
    keys, seals of sensitive tasks to their executing
    node, and the plain-carriage judge. ``now`` is
    injectable so reports are testable; it stamps
    records, reports, and envelopes only."""

    def __init__(self, policy: dict | None = None, *, now=None):
        self._policy = validate_policy(
            copy.deepcopy(policy) if policy is not None else default_policy()
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._nodes: dict[str, dict] = {}
        self._sealed: dict[str, dict] = {}
        self._records: list[dict] = []
        self._refusals_by_reason: dict[str, int] = {}
        self._plain_accepted = 0
        self._events: list[dict] = []
        self._event_seq = 0

    @property
    def policy(self) -> dict:
        return copy.deepcopy(self._policy)

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    def _emit(self, type_: str, node_id: str, payload: dict) -> None:
        self._event_seq += 1
        self._events.append(
            make_event(
                type=type_,
                node_id=node_id,
                component="fabric",
                seq=self._event_seq,
                at=self._now(),
                payload=payload,
            )
        )

    def _record(
        self, action, node_id, peer_id, task_id, kind, verdict, reason,
        ciphertext_bytes=None,
    ) -> dict:
        record = {
            "version": 1,
            "seq": len(self._records) + 1,
            "at": _fmt(self._now()),
            "action": action,
            "node_id": node_id,
            "peer_id": peer_id,
            "task_id": task_id,
            "kind": kind,
            "verdict": verdict,
            "reason": reason,
            "ciphertext_bytes": ciphertext_bytes,
            "simulated": True,
        }
        checked = validate_record(record)
        self._records.append(checked)
        if verdict == "refused":
            self._refusals_by_reason[reason] = (
                self._refusals_by_reason.get(reason, 0) + 1
            )
            self._emit(
                "sealed.act_refused",
                node_id or peer_id or "sealer",
                {"action": action, "reason": reason},
            )
        return checked

    @staticmethod
    def _task_fields(task):
        if not isinstance(task, dict):
            return None, None
        task_id = task.get("id") if isinstance(task.get("id"), str) else None
        kind = task.get("kind") if isinstance(task.get("kind"), str) else None
        return task_id, kind

    def _checked_task(self, task):
        """A Y-007 task envelope, validated whole, or
        None when it fails its own contract."""
        try:
            return validate_task(copy.deepcopy(task))
        except SealedPayloadError:
            return None

    # -- enrolment -------------------------------------------

    def enrol(self, node_id: str, public_key_hex: str) -> dict:
        """Enrol one node's sealing public key. Only
        the public half ever enters the door. A
        duplicate enrolment, one past the policy's
        bound, or a key that is not a usable X25519
        public key is refused by name."""
        with self._lock:
            if not isinstance(node_id, str) or not node_id:
                return self._record(
                    "enrol", None, None, None, None, "refused", "malformed_key"
                )
            if node_id in self._nodes:
                return self._record(
                    "enrol", node_id, None, None, None, "refused", "duplicate_node"
                )
            if len(self._nodes) >= self._policy["max_nodes"]:
                return self._record(
                    "enrol", node_id, None, None, None, "refused", "mesh_full"
                )
            try:
                public = bytes.fromhex(public_key_hex)
            except (TypeError, ValueError):
                public = b""
            if len(public) != 32 or public == b"\x00" * 32:
                return self._record(
                    "enrol", node_id, None, None, None, "refused", "malformed_key"
                )
            self._nodes[node_id] = {
                "public": public,
                "fingerprint": hashlib.sha256(public).hexdigest(),
                "sealed_sent": 0,
                "sealed_received": 0,
            }
            self._emit("sealed.node_enrolled", node_id, {"node_id": node_id})
            return self._record("enrol", node_id, None, None, None, "accepted", None)

    # -- sealing ------------------------------------------------

    def seal(self, sender_id: str, recipient_id: str, task: dict) -> dict:
        """Seal one sensitive task's payload to its
        executing node. The envelope is held by the
        door and read back whole with ``sealed_for``;
        the record carries only its size. Judged by
        name: unknown doors, sealing to oneself, a
        malformed task, a kind the policy does not
        name sensitive, an oversized payload, a task
        already sealed, and a full store are refused
        and counted apart."""
        with self._lock:
            task_id, kind = self._task_fields(task)
            if sender_id not in self._nodes:
                return self._record(
                    "seal", sender_id if isinstance(sender_id, str) else None,
                    recipient_id if isinstance(recipient_id, str) else None,
                    task_id, kind, "refused", "unknown_sender",
                )
            if recipient_id not in self._nodes:
                return self._record(
                    "seal", sender_id, recipient_id, task_id, kind,
                    "refused", "unknown_recipient",
                )
            if sender_id == recipient_id:
                return self._record(
                    "seal", sender_id, recipient_id, task_id, kind,
                    "refused", "self_sealing",
                )
            checked = self._checked_task(task)
            if checked is None:
                return self._record(
                    "seal", sender_id, recipient_id, task_id, kind,
                    "refused", "malformed_task",
                )
            if checked["kind"] not in self._policy["sensitive_kinds"]:
                return self._record(
                    "seal", sender_id, recipient_id, task_id, kind,
                    "refused", "kind_not_sensitive",
                )
            if len(_canonical(checked["payload"])) > self._policy["max_payload_bytes"]:
                return self._record(
                    "seal", sender_id, recipient_id, task_id, kind,
                    "refused", "payload_too_large",
                )
            if checked["id"] in self._sealed:
                return self._record(
                    "seal", sender_id, recipient_id, task_id, kind,
                    "refused", "duplicate_task",
                )
            if len(self._sealed) >= self._policy["max_sealed"]:
                return self._record(
                    "seal", sender_id, recipient_id, task_id, kind,
                    "refused", "sealed_store_full",
                )
            envelope = _seal_bytes(
                sender_id, recipient_id, checked,
                self._nodes[recipient_id]["public"], at=_fmt(self._now()),
            )
            self._sealed[checked["id"]] = envelope
            self._nodes[sender_id]["sealed_sent"] += 1
            self._nodes[recipient_id]["sealed_received"] += 1
            self._emit(
                "sealed.payload_sealed",
                sender_id,
                {
                    "task_id": checked["id"],
                    "kind": checked["kind"],
                    "recipient_id": recipient_id,
                    "ciphertext_bytes": len(bytes.fromhex(envelope["ciphertext"])),
                },
            )
            return self._record(
                "seal", sender_id, recipient_id, task_id, kind, "accepted", None,
                ciphertext_bytes=len(bytes.fromhex(envelope["ciphertext"])),
            )

    def sealed_for(self, task_id: str) -> dict | None:
        """The sealed envelope for one task, whole, or
        None when this door never sealed it. The
        envelope carries ciphertext only — opening it
        is the executing node's own act, elsewhere."""
        with self._lock:
            envelope = self._sealed.get(task_id)
            return copy.deepcopy(envelope) if envelope is not None else None

    # -- the plain-carriage judge ---------------------------------

    def judge_plain(self, sender_id: str, recipient_id: str, task: dict) -> dict:
        """Judge one task proposed for plain (unsealed)
        carriage — the door the policy's law stands
        behind. A non-sensitive kind is accepted; a
        sensitive kind is refused ``sealing_required``:
        its content may travel only sealed. Unknown
        doors and malformed tasks are refused by name."""
        with self._lock:
            task_id, kind = self._task_fields(task)
            if sender_id not in self._nodes:
                return self._record(
                    "judge_plain",
                    sender_id if isinstance(sender_id, str) else None,
                    recipient_id if isinstance(recipient_id, str) else None,
                    task_id, kind, "refused", "unknown_sender",
                )
            if recipient_id not in self._nodes:
                return self._record(
                    "judge_plain", sender_id, recipient_id, task_id, kind,
                    "refused", "unknown_recipient",
                )
            checked = self._checked_task(task)
            if checked is None:
                return self._record(
                    "judge_plain", sender_id, recipient_id, task_id, kind,
                    "refused", "malformed_task",
                )
            if checked["kind"] in self._policy["sensitive_kinds"]:
                return self._record(
                    "judge_plain", sender_id, recipient_id, task_id, kind,
                    "refused", "sealing_required",
                )
            self._plain_accepted += 1
            return self._record(
                "judge_plain", sender_id, recipient_id, task_id, kind,
                "accepted", None,
            )

    # -- the door ---------------------------------------------------

    def report(self) -> dict:
        with self._lock:
            nodes = [
                {
                    "node_id": node_id,
                    "public_key_fingerprint": state["fingerprint"],
                    "sealed_sent": state["sealed_sent"],
                    "sealed_received": state["sealed_received"],
                }
                for node_id, state in sorted(self._nodes.items())
            ]
            return validate_report(
                {
                    "version": 1,
                    "at": _fmt(self._now()),
                    "simulated": True,
                    "policy": copy.deepcopy(self._policy),
                    "nodes": nodes,
                    "nodes_enrolled": len(nodes),
                    "payloads_sealed": len(self._sealed),
                    "plain_accepted": self._plain_accepted,
                    "refusals_by_reason": dict(
                        sorted(self._refusals_by_reason.items())
                    ),
                    "records": copy.deepcopy(self._records),
                }
            )


__all__ = [
    "ALGORITHM",
    "PayloadSealer",
    "SealedPayloadError",
    "default_policy",
    "generate_sealing_keypair",
    "open_sealed",
    "validate_envelope",
    "validate_policy",
    "validate_record",
    "validate_report",
    "validate_task",
]
