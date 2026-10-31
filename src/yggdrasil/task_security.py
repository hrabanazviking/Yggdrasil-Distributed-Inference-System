"""Task security — Y-043.

The threat model's T-04 control, enforced at the agent's
door: **tasks are data, never code, unless a signed task
kind explicitly says so.** A task envelope (Y-007) is a
request in data — a prompt, some texts, references by name.
Nothing in a data task is ever executed by this tree: the
sandbox (Y-029) hands the envelope to a runner as data, and
this slice is the guard that keeps it that way, by refusing
at the door any task that tries to smuggle code in as data
or to run code without its papers.

- **Policy as data** (``task_security_policy`` v1): the
  kind names that are declared code-bearing (default:
  none — no kind may carry code), the payload field names
  that count as executable material wherever they appear in
  a payload, at any depth (default vocabulary: ``code``,
  ``script``, ``bytecode``, ``shell_command``, ``exec``,
  and kin), and the trusted signers for code tasks — node
  ids with their Y-011 Ed25519 public keys, handed in as
  deployment wiring from the nodes' certificates (default:
  none trusted).
- **The scan.** Every task is judged before a lease is
  taken or a queue slot is spent. Its payload is walked
  recursively; any executable field found is a *finding*,
  named by its dotted path (field names only — payload
  content never enters a verdict, an event, or a report).
  A task is *code-bearing in fact* when its kind is a
  declared code kind **or** its payload carries executable
  fields.
- **The rule.** A data task with no findings is allowed as
  data. A code-bearing task is allowed only when its kind
  is a declared code kind **and** it travels with a valid
  ``task_signature`` v1 — an Ed25519 signature, by a trusted
  signer, over the SHA-256 digest of the task's own id,
  kind, and payload (the signature travels beside the
  envelope, the way the pin travels with a model fetch,
  Y-037; the Y-007 envelope itself carries no signature
  field). Code fields under a data kind are refused even
  with a signature — the *kind* must say so, a signature
  alone never promotes data into code.
- **Refusals have names.** Every judgement is a validated
  ``task_security_verdict`` v1: ``code_in_data_task``
  (``scope_denied``), ``code_kind_unsigned``
  (``scope_denied``), ``signer_untrusted``
  (``scope_denied``), ``signature_mismatch`` — the block
  names another task, kind, or payload digest
  (``verification_failed``), ``signature_invalid`` — the
  signature does not verify against the signer's key
  (``verification_failed``). Refusals are emitted as Y-013
  events from the ``node_agent`` component, and signed code
  tasks allowed are emitted too — code running is loud,
  data running is ordinary. The ``task_security_report``
  v1 keeps every verdict.

The node agent (wired at this slice) grows the seam:
``task_guard=`` at construction; ``execute_task`` and
``queue_task`` consult the guard before any lease or queue
slot is spent (a ``signature=`` travels with the call), a
refusal raises before the task exists as far as the node
is concerned — no lease, no queue entry, no journal line
(a refusal at the door is not a run, Y-039's rule);
``task_security_report()`` and ``status()`` carry the
ledger. Without a guard wired, the agent judges nothing,
exactly as before. All four citizen factories wire a guard
under the default policy (no code kinds, no trusted
signers — every citizen refuses code outright until an
operator's deployment names a code kind and its signers).

Honesty: this guard is a door check on shapes and
signatures, not an operating-system confinement — it does
not sandbox a runner that disobeys the protocol (the
sandbox's own bounds are Y-029, OS isolation is claimed by
no slice), and a verified signature proves *who signed
these exact bytes*, never that the code is good. The keys
are Y-011's Ed25519 (pure Python, not constant-time, as
Y-011 states). The signature check is proven here against
real generated keypairs; no code task has run on named
hardware, and none is claimed.
"""

import base64
import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil import ed25519
from yggdrasil.observability import make_event

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

ALLOW = "allow"
REFUSE = "refuse"

REASON_DATA_TASK = "data_task"
REASON_SIGNED_CODE_TASK = "signed_code_task"
REASON_CODE_IN_DATA_TASK = "code_in_data_task"
REASON_CODE_KIND_UNSIGNED = "code_kind_unsigned"
REASON_SIGNER_UNTRUSTED = "signer_untrusted"
REASON_SIGNATURE_MISMATCH = "signature_mismatch"
REASON_SIGNATURE_INVALID = "signature_invalid"

ERROR_SCOPE_DENIED = "scope_denied"
ERROR_VERIFICATION_FAILED = "verification_failed"

#: The default executable-field vocabulary: payload field
#: names that carry code wherever they appear. An operator's
#: policy may widen or narrow the list; the defaults are a
#: policy, not a law of nature.
DEFAULT_EXECUTABLE_FIELDS = [
    "bytecode",
    "code",
    "command",
    "commands",
    "eval",
    "exec",
    "executable",
    "javascript",
    "python",
    "script",
    "shell",
    "shell_command",
    "source_code",
    "wasm",
]


def _load_schema(name: str) -> dict:
    return json.loads((_SCHEMAS / name).read_text())


def _validator(name: str):
    """A validator for one schema, with every contract schema
    registered so relative $refs resolve — the registry pattern
    the earlier slices build."""
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


_POLICY_VALIDATOR = _validator("task_security_policy.schema.json")
_SIGNATURE_VALIDATOR = _validator("task_signature.schema.json")
_VERDICT_VALIDATOR = _validator("task_security_verdict.schema.json")
_REPORT_VALIDATOR = _validator("task_security_report.schema.json")
_TASK_VALIDATOR = _validator("task_envelope.schema.json")


class TaskSecurityError(Exception):
    """Any task-security failure: an unshaped policy, task,
    or signature block, or a record that fails its schema."""


class TaskSecurityRefusal(TaskSecurityError):
    """A task the guard refused. Carries the validated
    verdict, so the door that raises it can name the reason
    and the Y-007 error code exactly."""

    def __init__(self, verdict: dict):
        self.verdict = verdict
        self.error_code = verdict["error_code"]
        super().__init__(
            "task refused by the task guard: "
            + ", ".join(verdict["reasons"])
            + f" ({verdict['error_code']})"
        )


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise TaskSecurityError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_policy(policy: dict) -> dict:
    """A task-security policy against its schema."""
    return _validate(_POLICY_VALIDATOR, policy, "task security policy")


def validate_signature(record: dict) -> dict:
    """A task signature block against its schema."""
    return _validate(_SIGNATURE_VALIDATOR, record, "task signature")


def validate_verdict(record: dict) -> dict:
    """A task-security verdict against its schema."""
    return _validate(_VERDICT_VALIDATOR, record, "task security verdict")


def validate_report(record: dict) -> dict:
    """A task-security report against its schema."""
    return _validate(_REPORT_VALIDATOR, record, "task security report")


def default_policy() -> dict:
    """The default task-security policy — a policy, not a
    fact: no kind may carry code, the default executable
    vocabulary is scanned for, and no signer is trusted.
    Under it, every citizen refuses code outright."""
    return validate_policy(
        {
            "version": 1,
            "code_kinds": [],
            "executable_fields": list(DEFAULT_EXECUTABLE_FIELDS),
            "trusted_signers": [],
        }
    )


def task_payload_digest(task: dict) -> str:
    """The SHA-256 (hex) of the task's own identity as data:
    its id, kind, and payload in canonical JSON. This is the
    digest a code task's signature signs — change any byte of
    the payload and the digest, and so the signature, dies."""
    if not isinstance(task, dict):
        raise TaskSecurityError("a task digest needs a task envelope")
    for name in ("id", "kind", "payload"):
        if name not in task:
            raise TaskSecurityError(f"a task digest needs the envelope's {name}")
    canonical = json.dumps(
        {"id": task["id"], "kind": task["kind"], "payload": task["payload"]},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sign_task(keypair, task: dict, *, signer_node_id: str) -> dict:
    """Sign one code-bearing task with a Y-011 keypair and
    return its validated ``task_signature`` v1 block, to
    travel beside the envelope. ``keypair`` is anything with
    the NodeKeypair shape (a ``sign(bytes)`` method)."""
    digest = task_payload_digest(task)
    signature = base64.b64encode(keypair.sign(bytes.fromhex(digest))).decode("ascii")
    return validate_signature(
        {
            "version": 1,
            "task_id": task["id"],
            "kind": task["kind"],
            "signer_node_id": signer_node_id,
            "payload_sha256": digest,
            "signature": signature,
        }
    )


def _scan(value, path: str, fields: frozenset, findings: list[str]) -> None:
    """Walk a payload recording the dotted path of every
    executable field name, at any depth. Field names only —
    the payload's content is never copied anywhere."""
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            if key in fields:
                findings.append(child)
            _scan(item, child, fields, findings)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan(item, f"{path}[{index}]", fields, findings)


def _decode_b64(text) -> bytes | None:
    if not isinstance(text, str):
        return None
    try:
        return base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        return None


class TaskGuard:
    """One node's task guard: every task judged at the door
    against the policy, every judgement ledgered.

    ``policy`` is a ``task_security_policy`` v1 (the default
    when none is handed in). ``now`` is an injectable clock.
    """

    def __init__(self, node_id: str, policy: dict | None = None, *, now=None):
        if not isinstance(node_id, str) or not node_id:
            raise TaskSecurityError("a task guard needs a node_id")
        self.node_id = node_id
        self._policy = validate_policy(dict(policy)) if policy is not None else default_policy()
        self._code_kinds = frozenset(self._policy["code_kinds"])
        self._fields = frozenset(self._policy["executable_fields"])
        self._signers = {
            row["node_id"]: row["public_key"] for row in self._policy["trusted_signers"]
        }
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._verdicts: list[dict] = []
        self._events: list[dict] = []
        self._event_seq = 0

    # -- facts ----------------------------------------------------------

    @property
    def policy(self) -> dict:
        return json.loads(json.dumps(self._policy))

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    @property
    def verdicts(self) -> list[dict]:
        return [dict(verdict) for verdict in self._verdicts]

    # -- internals --------------------------------------------------------

    def _emit(self, type: str, verdict: dict) -> dict:
        event = make_event(
            type=type,
            node_id=self.node_id,
            component="node_agent",
            payload={
                "kind": verdict["kind"],
                "decision": verdict["decision"],
                "reasons": verdict["reasons"],
                "error_code": verdict["error_code"],
                "code_bearing": verdict["code_bearing"],
                "signer_node_id": verdict["signer_node_id"],
                "findings": verdict["findings"],
            },
            seq=self._event_seq,
            task_id=verdict["task_id"],
            at=self._now(),
        )
        self._event_seq += 1
        self._events.append(event)
        return event

    def _judge_signature(self, task: dict, signature: dict | None):
        """Judge a declared code kind's papers. Returns
        (allowed, reason, error_code, signer_node_id)."""
        if signature is None:
            return False, REASON_CODE_KIND_UNSIGNED, ERROR_SCOPE_DENIED, None
        signer = signature["signer_node_id"]
        public_key = self._signers.get(signer)
        if public_key is None:
            return False, REASON_SIGNER_UNTRUSTED, ERROR_SCOPE_DENIED, signer
        digest = task_payload_digest(task)
        if (
            signature["task_id"] != task["id"]
            or signature["kind"] != task["kind"]
            or signature["payload_sha256"] != digest
        ):
            return False, REASON_SIGNATURE_MISMATCH, ERROR_VERIFICATION_FAILED, signer
        key_bytes = _decode_b64(public_key)
        sig_bytes = _decode_b64(signature["signature"])
        if (
            key_bytes is None
            or len(key_bytes) != 32
            or sig_bytes is None
            or len(sig_bytes) != 64
            or not ed25519.verify(key_bytes, bytes.fromhex(digest), sig_bytes)
        ):
            return False, REASON_SIGNATURE_INVALID, ERROR_VERIFICATION_FAILED, signer
        return True, REASON_SIGNED_CODE_TASK, None, signer

    # -- the door ---------------------------------------------------------

    def check(self, task: dict, signature: dict | None = None) -> dict:
        """Judge one task envelope (with its signature block
        when it carries one) and return the validated
        ``task_security_verdict`` v1. A refusal is a verdict,
        not an exception — the caller decides how loudly to
        refuse. An unshaped task or signature block raises
        :class:`TaskSecurityError`: a malformed envelope is
        Y-007's ``malformed_envelope`` country, not a verdict."""
        _validate(_TASK_VALIDATOR, task, "task envelope")
        if signature is not None:
            validate_signature(signature)
        findings: list[str] = []
        _scan(task["payload"], "", self._fields, findings)
        findings = sorted(set(findings))
        kind = task["kind"]
        declared_code = kind in self._code_kinds
        code_bearing = declared_code or bool(findings)

        signer_node_id = signature["signer_node_id"] if signature is not None else None
        signature_verified = False
        if not code_bearing:
            decision, reasons, error_code = ALLOW, [REASON_DATA_TASK], None
        elif not declared_code:
            # Code in fact under a kind that never said so —
            # refused even with a signature: the kind must
            # explicitly say so, papers alone promote nothing.
            decision, reasons, error_code = (
                REFUSE,
                [REASON_CODE_IN_DATA_TASK],
                ERROR_SCOPE_DENIED,
            )
        else:
            allowed, reason, error_code, signer_node_id = self._judge_signature(
                task, signature
            )
            signature_verified = allowed
            decision = ALLOW if allowed else REFUSE
            reasons = [reason]

        verdict = {
            "version": 1,
            "node_id": self.node_id,
            "task_id": task["id"],
            "kind": kind,
            "at": _fmt(self._now()),
            "decision": decision,
            "code_bearing": code_bearing,
            "signature_verified": signature_verified,
            "signer_node_id": signer_node_id,
            "error_code": error_code,
            "reasons": reasons,
            "findings": findings,
        }
        validate_verdict(verdict)
        with self._lock:
            self._verdicts.append(verdict)
            if decision == REFUSE:
                self._emit("task_security.task_refused", verdict)
            elif code_bearing:
                self._emit("task_security.code_task_allowed", verdict)
            return verdict

    def require_allowed(self, task: dict, signature: dict | None = None) -> dict:
        """Judge one task and raise :class:`TaskSecurityRefusal`
        (carrying the verdict) when the judgement is a refusal.
        Returns the allowing verdict otherwise."""
        verdict = self.check(task, signature)
        if verdict["decision"] == REFUSE:
            raise TaskSecurityRefusal(verdict)
        return verdict

    def report(self) -> dict:
        """The validated task_security_report v1: every task
        this guard judged, counted, verdicts kept."""
        with self._lock:
            verdicts = [dict(verdict) for verdict in self._verdicts]
            return validate_report(
                {
                    "version": 1,
                    "node_id": self.node_id,
                    "at": _fmt(self._now()),
                    "checked": len(verdicts),
                    "allowed": sum(1 for v in verdicts if v["decision"] == ALLOW),
                    "refused": sum(1 for v in verdicts if v["decision"] == REFUSE),
                    "signed_code_allowed": sum(
                        1
                        for v in verdicts
                        if v["decision"] == ALLOW and v["signature_verified"]
                    ),
                    "verdicts": verdicts,
                }
            )


__all__ = [
    "ALLOW",
    "DEFAULT_EXECUTABLE_FIELDS",
    "REFUSE",
    "TaskGuard",
    "TaskSecurityError",
    "TaskSecurityRefusal",
    "default_policy",
    "sign_task",
    "task_payload_digest",
    "validate_policy",
    "validate_report",
    "validate_signature",
    "validate_verdict",
]
