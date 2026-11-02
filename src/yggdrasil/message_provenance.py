"""Message provenance — Y-059.

The fabric (Y-050) was built signed from its first
slice: a ``signer`` seam, a ``verifier`` seam, and a
digest for signatures to cover. What it did not hold
is the *law* the threat model (Y-010, T-01/T-03/T-09)
names: every message signed by its node, and
unsigned or wrongly-signed traffic dropped and
counted. This slice is that law, as a door:
:class:`ProvenanceGate`.

- **Enrolment is certificate-gated.** A node joins
  the gate with its Y-011 node certificate. The gate
  verifies the certificate against the mesh root
  (chain, fingerprint, signature, validity window —
  Y-011's own checks) and binds the node id to the
  certificate's public key and fingerprint. A
  certificate naming another node, issued by a
  foreign root, expired, or paired with a keypair
  that is not the certificate's key is refused by
  name. Enrolment with the node's keypair lets the
  gate sign for it; enrolment without one is
  verify-only — the gate can judge that node's
  traffic but can never speak in its name.
- **Signing is real.** The gate's ``signer`` /
  ``verifier`` are Y-050's seam, filled with Y-011's
  Ed25519: a signature is the enrolled key's
  signature over the UTF-8 bytes of the fabric
  message digest (SHA-256 over the message's own
  canonical fields, Y-050's :func:`message_digest`).
  :meth:`ProvenanceGate.fabric` returns a fabric
  already wired with them, so ordinary traffic is
  signed at birth and a delivery whose signature is
  missing or wrong fails at the fabric by its own
  name (``signature_invalid``) — held for direct
  retry there, as Y-050 holds failed deliveries.
- **The door drops and counts.** Raw traffic —
  anything that arrives as a message dict, however
  it got here — is judged by :meth:`admit`, in this
  order: a message that fails the ``fabric_message``
  v1 schema is dropped ``malformed``; a message
  with no signature is dropped ``unsigned``; a
  message whose named sender is not enrolled is
  dropped ``unknown_signer``; a signature that is
  not the enrolled key's signature over this exact
  message's digest — forged, tampered with in any
  signed field, or signed by another node's key —
  is dropped ``signature_invalid``; a verified
  message whose per-sender sequence is not beyond
  the last sequence this door has verified from
  that sender is dropped ``replayed`` (a recording
  cannot speak twice, and a failed forgery never
  advances the watermark, so it cannot lock the
  true sender out). What passes is ``verified``:
  signed by the key the mesh's own root bound to
  the named sender, over exactly these fields, in
  sequence. Every judgement is a validated
  ``message_provenance_record`` v1, counted in the
  report with drops counted apart by reason, and
  evented as Y-013 envelopes from the ``fabric``
  component — names, ids, and counts only, never
  payload content. :meth:`require_verified` is the
  same door for callers that want a refusal raised.
- **The law is schema.** ``message_provenance_
  policy`` v1 carries the constants:
  ``signing_required`` always true, ``unsigned_
  allowed`` / ``wrongly_signed_allowed`` /
  ``replay_allowed`` always false, algorithm
  ``ed25519``, digest ``sha256`` — a policy naming
  any weaker law fails validation.

This gate is a formalisation standing on Y-050's
fabric and Y-011's identity — not a rewiring of
either; components that hold raw messages judge
them here, and a fabric built by :meth:`fabric`
enforces the same keys at delivery. No node-agent
seam (the standing Part III precedent). Honesty:
the cryptography is the real thing — Y-011's
pure-Python Ed25519, NOT constant-time, as Y-011
states — but the gate is an in-process door: no
socket opened, no LAN crossed; real carriage is
Y-062, hardware proof Part VIII's. Rate and size
quotas (a flooding node throttled) are Y-060.
"""

import base64
import binascii
import copy
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil import ed25519
from yggdrasil.fabric import Fabric, FabricError, message_digest, validate_message
from yggdrasil.identity import (
    IdentityError,
    NodeKeypair,
    fingerprint,
    verify_certificate,
)
from yggdrasil.observability import make_event

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"


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


_POLICY_VALIDATOR = _validator("message_provenance_policy.schema.json")
_RECORD_VALIDATOR = _validator("message_provenance_record.schema.json")
_REPORT_VALIDATOR = _validator("message_provenance_report.schema.json")


class ProvenanceError(Exception):
    """The provenance gate's own failure: a record
    that fails its schema, a refused enrolment, or
    (via :meth:`ProvenanceGate.require_verified`) a
    dropped message. A refusal carries its counted
    reason in ``reason`` where one names it."""

    def __init__(self, message: str, *, reason: str | None = None):
        super().__init__(message)
        self.reason = reason


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record, kind: str):
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise ProvenanceError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_policy(record: dict) -> dict:
    return _validate(_POLICY_VALIDATOR, record, "message provenance policy")


def validate_record(record: dict) -> dict:
    return _validate(_RECORD_VALIDATOR, record, "message provenance record")


def validate_report(record: dict) -> dict:
    return _validate(_REPORT_VALIDATOR, record, "message provenance report")


def default_policy() -> dict:
    """The provenance law as data: signing is
    required, unsigned and wrongly-signed traffic
    is never admitted, replays are never admitted,
    and the signature is Ed25519 over the SHA-256
    fabric digest. Constants, labelled as law — a
    policy naming anything weaker fails its
    schema."""
    return validate_policy(
        {
            "version": 1,
            "signing_required": True,
            "unsigned_allowed": False,
            "wrongly_signed_allowed": False,
            "replay_allowed": False,
            "algorithm": "ed25519",
            "digest": "sha256",
        }
    )


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text) -> bytes | None:
    if not isinstance(text, str):
        return None
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        return None


class _Enrolled:
    def __init__(self, node_id: str, cert: dict, keypair: NodeKeypair | None):
        self.node_id = node_id
        self.cert = copy.deepcopy(cert)
        self.public_key = base64.b64decode(cert["public_key"])
        self.fingerprint = fingerprint(self.public_key)
        self.keypair = keypair


class ProvenanceGate:
    """The mesh's provenance door: enrolment under
    the mesh root's certificates, signing and
    verification with the enrolled Y-011 keys, and
    admission judgement for raw messages. ``now``
    is injectable so runs are replayable."""

    def __init__(self, root_cert: dict, policy: dict | None = None, *, now=None):
        self._policy = validate_policy(
            copy.deepcopy(policy) if policy is not None else default_policy()
        )
        self._now = now or (lambda: datetime.now(UTC))
        try:
            verify_certificate(root_cert, root_cert)
        except IdentityError as exc:
            raise ProvenanceError(
                f"the gate's root certificate does not verify: {exc}",
                reason="certificate_invalid",
            ) from exc
        self._root_cert = copy.deepcopy(root_cert)
        self._lock = threading.RLock()
        self._nodes: dict[str, _Enrolled] = {}
        self._watermarks: dict[str, int] = {}
        self._records: list[dict] = []
        self._events: list[dict] = []
        self._event_seq = 0
        self._seen = 0
        self._verified = 0
        self._dropped = 0
        self._dropped_reasons: dict[str, int] = {}

    # -- accessors -------------------------------------------

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

    # -- enrolment ---------------------------------------------

    def enrol(
        self, node_id: str, cert: dict, keypair: NodeKeypair | None = None
    ) -> str:
        """Enrol one node under its Y-011 certificate.
        The certificate must verify against the mesh
        root and must name ``node_id``; a handed-in
        keypair must be the certificate's own key.
        Returns the enrolled key's fingerprint. With
        a keypair the gate can sign for the node;
        without one the enrolment is verify-only."""
        with self._lock:
            if not isinstance(cert, dict) or cert.get("node_id") != node_id:
                raise ProvenanceError(
                    f"a certificate naming "
                    f"{cert.get('node_id') if isinstance(cert, dict) else cert!r} "
                    f"cannot enrol node {node_id}",
                    reason="node_mismatch",
                )
            try:
                verify_certificate(cert, self._root_cert)
            except IdentityError as exc:
                raise ProvenanceError(
                    f"node {node_id}'s certificate does not verify "
                    f"against the mesh root: {exc}",
                    reason="certificate_invalid",
                ) from exc
            public_key = base64.b64decode(cert["public_key"])
            if keypair is not None and keypair.public_key != public_key:
                raise ProvenanceError(
                    f"the keypair handed in for node {node_id} is not "
                    f"the key its certificate binds",
                    reason="key_mismatch",
                )
            if node_id in self._nodes:
                raise ProvenanceError(
                    f"node {node_id} is already enrolled",
                    reason="already_enrolled",
                )
            enrolled = _Enrolled(node_id, cert, keypair)
            self._nodes[node_id] = enrolled
            self._emit(
                "provenance.node_enrolled",
                node_id,
                {"node": node_id, "can_sign": keypair is not None},
            )
            return enrolled.fingerprint

    def enrolled(self, node_id: str) -> bool:
        return node_id in self._nodes

    def signer_fingerprint(self, node_id: str) -> str | None:
        enrolled = self._nodes.get(node_id)
        return enrolled.fingerprint if enrolled is not None else None

    # -- the Y-050 seam, filled with Y-011 keys -----------------

    def sign_digest(self, node_id: str, digest: str) -> str:
        """Sign one fabric message digest with one
        enrolled node's key. Returns the base64
        Ed25519 signature over the digest's UTF-8
        bytes."""
        with self._lock:
            enrolled = self._nodes.get(node_id)
            if enrolled is None:
                raise ProvenanceError(
                    f"node {node_id} is not enrolled; the gate cannot "
                    f"sign in its name",
                    reason="unknown_signer",
                )
            if enrolled.keypair is None:
                raise ProvenanceError(
                    f"node {node_id} is enrolled verify-only; the gate "
                    f"holds no keypair for it",
                    reason="cannot_sign",
                )
            return _b64(enrolled.keypair.sign(digest.encode("utf-8")))

    def verify_signature(self, node_id: str, digest: str, signature) -> bool:
        """True iff ``signature`` is the enrolled key's
        signature over ``digest`` for the node named.
        Pure cryptography: no counting, no watermark —
        the fabric calls this per delivery attempt;
        the door's counted judgement is :meth:`admit`.
        """
        enrolled = self._nodes.get(node_id)
        if enrolled is None:
            return False
        raw = _unb64(signature)
        if raw is None:
            return False
        return ed25519.verify(enrolled.public_key, digest.encode("utf-8"), raw)

    def signer(self):
        """The ``signer`` callable for Y-050's fabric:
        ``(node_id, digest) -> signature``."""

        def _sign(node_id: str, digest: str) -> str:
            return self.sign_digest(node_id, digest)

        return _sign

    def verifier(self):
        """The ``verifier`` callable for Y-050's fabric:
        ``(node_id, digest, signature) -> bool``."""

        def _verify(node_id: str, digest: str, signature) -> bool:
            return self.verify_signature(node_id, digest, signature)

        return _verify

    def fabric(self, **fabric_kwargs) -> Fabric:
        """A fabric wired with this gate's signer and
        verifier (and, unless overridden, its clock):
        every message it builds is signed by its
        sender's enrolled key, and every delivery is
        verified against the enrolled key of the node
        it names as sender."""
        fabric_kwargs.setdefault("now", self._now)
        return Fabric(
            signer=self.signer(), verifier=self.verifier(), **fabric_kwargs
        )

    # -- the door -------------------------------------------------

    def _judge(self, message) -> tuple[str, str | None, dict]:
        """(verdict, reason, fields) for one raw
        message. ``fields`` carries the record's
        identifying fields as they stood in the
        message — nulls where a malformed message
        carried nothing usable."""
        fields = {
            "message_id": None,
            "from_node": None,
            "to_node": None,
            "topic": None,
            "kind": None,
            "seq": None,
            "signer_fingerprint": None,
        }
        if not isinstance(message, dict):
            return "dropped", "malformed", fields
        for key in fields:
            value = message.get(key)
            if key in ("message_id", "from_node", "to_node", "topic"):
                fields[key] = value if isinstance(value, str) else None
            elif key == "kind":
                fields[key] = value if value in ("publish", "direct") else None
            elif key == "seq":
                fields[key] = (
                    value
                    if isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 1
                    else None
                )
        sender = fields["from_node"]
        if sender is not None:
            fields["signer_fingerprint"] = self.signer_fingerprint(sender)
        try:
            validate_message(copy.deepcopy(message))
        except FabricError:
            return "dropped", "malformed", fields
        signature = message.get("signature")
        if signature is None:
            return "dropped", "unsigned", fields
        enrolled = self._nodes.get(sender) if sender is not None else None
        if enrolled is None:
            return "dropped", "unknown_signer", fields
        if not self.verify_signature(sender, message_digest(message), signature):
            return "dropped", "signature_invalid", fields
        if message["seq"] <= self._watermarks.get(sender, 0):
            return "dropped", "replayed", fields
        return "verified", None, fields

    def admit(self, message) -> dict:
        """Judge one raw message at the door and
        return its validated provenance record.
        Verified messages advance the sender's
        sequence watermark; dropped messages are
        counted apart by reason and delivered
        nowhere — the record is all that crosses."""
        with self._lock:
            verdict, reason, fields = self._judge(message)
            self._seen += 1
            if verdict == "verified":
                self._verified += 1
                self._watermarks[fields["from_node"]] = fields["seq"]
            else:
                self._dropped += 1
                self._dropped_reasons[reason] = (
                    self._dropped_reasons.get(reason, 0) + 1
                )
            record = validate_record(
                {
                    "version": 1,
                    **fields,
                    "verdict": verdict,
                    "reason": reason,
                }
            )
            self._records.append(record)
            self._emit(
                "provenance.message_verified"
                if verdict == "verified"
                else "provenance.message_dropped",
                fields["from_node"] or "unknown",
                {
                    "sender": fields["from_node"] or "unknown",
                    "reason": reason or "verified",
                    "seq": fields["seq"] or 0,
                    "kind": fields["kind"] or "none",
                },
            )
            return copy.deepcopy(record)

    def require_verified(self, message) -> dict:
        """The door for callers that want a refusal
        raised: returns the message unchanged when it
        verifies, raises :class:`ProvenanceError`
        carrying the drop reason when it does not."""
        record = self.admit(message)
        if record["verdict"] != "verified":
            raise ProvenanceError(
                f"message from {record['from_node']!r} dropped at the "
                f"provenance door: {record['reason']}",
                reason=record["reason"],
            )
        return message

    # -- report ---------------------------------------------------

    def report(self) -> dict:
        with self._lock:
            return validate_report(
                {
                    "version": 1,
                    "at": _fmt(self._now()),
                    "simulated": True,
                    "policy": copy.deepcopy(self._policy),
                    "enrolled_nodes": [
                        {
                            "node_id": node_id,
                            "fingerprint": self._nodes[node_id].fingerprint,
                            "can_sign": self._nodes[node_id].keypair is not None,
                        }
                        for node_id in sorted(self._nodes)
                    ],
                    "records": copy.deepcopy(self._records),
                    "seen": self._seen,
                    "verified": self._verified,
                    "dropped": self._dropped,
                    "dropped_reasons": dict(
                        sorted(self._dropped_reasons.items())
                    ),
                }
            )
