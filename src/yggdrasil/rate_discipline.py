"""Rate and size discipline — Y-060.

The threat model (Y-010, T-03 gossip spam, T-07
resource exhaustion) names the flooding node as an
enemy, and the roadmap gives Níðhöggr a first small
tooth, local scale: per-node quotas on gossip and
tasks, and a flooding node throttled by its peers.
This slice is that tooth, as a door each peer keeps
for itself: :class:`RateGate`.

- **Judgement is peer-side and per sender.** A gate
  is one peer's door. :meth:`RateGate.admit` judges
  one message by its sender, its traffic class
  (``gossip``, ``task``, or ``other``), and its size
  in bytes, against the gate's policy — quotas are
  counted per sender, so one flooding node spends
  only its own allowance and can never throttle a
  quiet neighbour at this door.
- **The quotas are a sliding window.** The policy's
  ``window_seconds`` window holds a sender's
  *admitted* messages only: at most
  ``max_messages_per_window`` in total, at most
  ``max_gossip_per_window`` of them gossip, at most
  ``max_task_per_window`` of them tasks, and at most
  ``max_bytes_per_window`` admitted bytes in total.
  Throttled messages enter no window and consume no
  quota — a flood cannot extend its own punishment
  by being counted, and a sender that goes quiet
  earns its allowance back as the window slides.
- **Size discipline is absolute.** A single message
  larger than ``max_message_bytes`` is throttled
  ``oversized`` however empty the window stands;
  oversized traffic is dropped and counted apart,
  delivered nowhere. An oversized message alone
  does not earn a throttle cooldown — size is a
  property of the message, flooding is a property
  of the sender.
- **A quota breach throttles the sender.** The
  first message that would exceed a quota is
  throttled by that quota's name (``quota_gossip``,
  ``quota_task``, ``quota_total``, or
  ``quota_bytes``) and the sender enters a throttle
  cooldown of ``throttle_cooldown_seconds`` at this
  door: while it lasts, *all* of that sender's
  traffic is throttled ``throttled``, whatever its
  class or size. Throttled traffic does not extend
  the cooldown. When the cooldown has passed the
  door lifts the throttle (``rate.throttle_lifted``)
  and judges the next message against the window
  again — if the window is still full, that message
  breaches again and earns a fresh cooldown.
- **Fabric messages judge themselves.**
  :meth:`RateGate.admit_message` takes a raw fabric
  message dict: its size is the canonical-JSON byte
  count of the message as it stands
  (:func:`message_size_bytes`), and its class is
  derived by a stated rule — a publication to a
  ``tasks.*`` topic and every direct message (the
  task channel's claims, streams, and results,
  Y-057) are ``task`` traffic; every other
  publication (registry, health, and state news) is
  ``gossip`` traffic. A message with no usable
  sender is throttled ``malformed``. Callers that
  know their class judge directly through
  :meth:`admit` instead. :meth:`require_admitted`
  is the same door for callers that want a refusal
  raised as :class:`RateError` carrying the reason.
- **Every judgement is counted.** Each one is a
  validated ``rate_discipline_record`` v1, counted
  in :meth:`RateGate.report` with throttled
  messages counted apart by reason and a per-sender
  standing (admitted / throttled totals, admitted
  bytes, what the current window still holds, and
  the throttle's end where one runs), and evented
  as Y-013 envelopes from the ``fabric`` component
  — names, classes, sizes, and counts only, never
  payload content.
- **The law is schema.** ``rate_discipline_policy``
  v1 carries the discipline as data:
  ``oversized_allowed`` always false and
  ``throttle_on_quota`` always true, with every
  quota and bound a positive number — a policy
  naming weaker discipline fails validation.

This gate is a formalisation standing beside the
fabric (Y-050), provenance (Y-059), gossip (Y-055),
and the task channel (Y-057) — not a rewiring of
any of them; each peer judges the traffic at its
own door. No node-agent seam (the standing Part III
precedent). Honesty: the gate is an in-process
door — ``simulated: true`` is a schema constant on
its report, no socket opened, no LAN crossed; real
carriage is Y-062, fabric-wide rate observability
is Y-064, hardware proof Part VIII's.
"""

import copy
import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.observability import make_event

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

TRAFFIC_CLASSES = ("gossip", "task", "other")

_SENDER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


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


_POLICY_VALIDATOR = _validator("rate_discipline_policy.schema.json")
_RECORD_VALIDATOR = _validator("rate_discipline_record.schema.json")
_REPORT_VALIDATOR = _validator("rate_discipline_report.schema.json")


class RateError(Exception):
    """The rate door's own failure: a record that
    fails its schema, or (via
    :meth:`RateGate.require_admitted`) a throttled
    message. A refusal carries its counted reason
    in ``reason`` where one names it."""

    def __init__(self, message: str, *, reason: str | None = None):
        super().__init__(message)
        self.reason = reason


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record, kind: str):
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise RateError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_policy(record: dict) -> dict:
    return _validate(_POLICY_VALIDATOR, record, "rate discipline policy")


def validate_record(record: dict) -> dict:
    return _validate(_RECORD_VALIDATOR, record, "rate discipline record")


def validate_report(record: dict) -> dict:
    return _validate(_REPORT_VALIDATOR, record, "rate discipline report")


def default_policy() -> dict:
    """The discipline as data, at the mesh's
    ordinary scale: a 60-second sliding window in
    which one sender may have admitted at most 240
    messages — 120 of them gossip, 60 of them tasks
    — and at most 1 MiB of admitted bytes; no single
    message over 64 KiB is ever admitted; a quota
    breach throttles the sender at this door for 30
    seconds. A policy, not a fact about a LAN."""
    return validate_policy(
        {
            "version": 1,
            "window_seconds": 60,
            "max_messages_per_window": 240,
            "max_gossip_per_window": 120,
            "max_task_per_window": 60,
            "max_bytes_per_window": 1048576,
            "max_message_bytes": 65536,
            "throttle_cooldown_seconds": 30,
            "throttle_on_quota": True,
            "oversized_allowed": False,
        }
    )


def message_size_bytes(message) -> int:
    """One message's size: the byte count of its
    canonical JSON (sorted keys, tight separators,
    UTF-8) as it stands, signature included where
    one is carried. Not JSON data at all counts as
    its ``repr`` byte count, so the door can still
    name a size for malformed traffic."""
    try:
        text = json.dumps(
            message, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
    except (TypeError, ValueError):
        text = repr(message)
    return len(text.encode("utf-8"))


def classify_message(message) -> str | None:
    """The stated classification rule for one raw
    fabric message: a publication to a ``tasks.*``
    topic and every direct message are ``task``
    traffic; every other publication is ``gossip``
    traffic. ``None`` where the message carries no
    usable kind/topic shape to classify."""
    if not isinstance(message, dict):
        return None
    kind = message.get("kind")
    topic = message.get("topic")
    if kind == "direct":
        return "task"
    if kind == "publish" and isinstance(topic, str):
        return "task" if topic.startswith("tasks.") else "gossip"
    return None


class _Sender:
    def __init__(self):
        # admitted messages only: (epoch_seconds, traffic_class, size_bytes)
        self.window: list[tuple[float, str, int]] = []
        self.admitted = 0
        self.throttled = 0
        self.bytes_admitted = 0
        self.throttled_until: float | None = None


class RateGate:
    """One peer's rate door: per-sender quotas on
    gossip and tasks, an absolute size bound, and a
    throttle cooldown for senders that breach a
    quota. ``now`` is injectable so runs are
    replayable."""

    def __init__(self, policy: dict | None = None, *, now=None):
        self._policy = validate_policy(
            copy.deepcopy(policy) if policy is not None else default_policy()
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._senders: dict[str, _Sender] = {}
        self._records: list[dict] = []
        self._events: list[dict] = []
        self._event_seq = 0
        self._seen = 0
        self._admitted = 0
        self._throttled = 0
        self._throttled_reasons: dict[str, int] = {}

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

    # -- internals ---------------------------------------------

    def _prune(self, sender: _Sender, moment: float) -> None:
        horizon = moment - self._policy["window_seconds"]
        sender.window = [entry for entry in sender.window if entry[0] > horizon]

    def _record(
        self,
        *,
        sender: str | None,
        traffic_class: str | None,
        size_bytes: int | None,
        verdict: str,
        reason: str | None,
    ) -> dict:
        record = validate_record(
            {
                "version": 1,
                "at": _fmt(self._now()),
                "sender": sender,
                "traffic_class": traffic_class,
                "size_bytes": size_bytes,
                "verdict": verdict,
                "reason": reason,
            }
        )
        self._records.append(record)
        self._seen += 1
        if verdict == "admitted":
            self._admitted += 1
        else:
            self._throttled += 1
            self._throttled_reasons[reason] = (
                self._throttled_reasons.get(reason, 0) + 1
            )
        self._emit(
            "rate.message_admitted"
            if verdict == "admitted"
            else "rate.message_throttled",
            sender or "unknown",
            {
                "sender": sender or "unknown",
                "traffic_class": traffic_class or "none",
                "reason": reason or "admitted",
                "size_bytes": size_bytes or 0,
            },
        )
        return copy.deepcopy(record)

    # -- the door -------------------------------------------------

    def admit(self, sender, traffic_class, size_bytes) -> dict:
        """Judge one message by sender, traffic
        class, and size, and return its validated
        rate record. Admitted messages enter the
        sender's window; throttled messages are
        counted apart by reason and delivered
        nowhere — the record is all that crosses."""
        with self._lock:
            moment = self._now().timestamp()
            if (
                not isinstance(sender, str)
                or not _SENDER_RE.match(sender)
                or traffic_class not in TRAFFIC_CLASSES
                or not isinstance(size_bytes, int)
                or isinstance(size_bytes, bool)
                or size_bytes < 0
            ):
                return self._record(
                    sender=(
                        sender
                        if isinstance(sender, str) and _SENDER_RE.match(sender)
                        else None
                    ),
                    traffic_class=(
                        traffic_class if traffic_class in TRAFFIC_CLASSES else None
                    ),
                    size_bytes=(
                        size_bytes
                        if isinstance(size_bytes, int)
                        and not isinstance(size_bytes, bool)
                        and size_bytes >= 0
                        else None
                    ),
                    verdict="throttled",
                    reason="malformed",
                )
            state = self._senders.setdefault(sender, _Sender())
            if state.throttled_until is not None:
                if moment < state.throttled_until:
                    state.throttled += 1
                    return self._record(
                        sender=sender,
                        traffic_class=traffic_class,
                        size_bytes=size_bytes,
                        verdict="throttled",
                        reason="throttled",
                    )
                state.throttled_until = None
                self._emit(
                    "rate.throttle_lifted",
                    sender,
                    {"sender": sender, "reason": "cooldown_elapsed"},
                )
            if size_bytes > self._policy["max_message_bytes"]:
                state.throttled += 1
                return self._record(
                    sender=sender,
                    traffic_class=traffic_class,
                    size_bytes=size_bytes,
                    verdict="throttled",
                    reason="oversized",
                )
            self._prune(state, moment)
            class_count = sum(
                1 for _t, cls, _s in state.window if cls == traffic_class
            )
            window_bytes = sum(s for _t, _c, s in state.window)
            reason = None
            if traffic_class == "gossip" and (
                class_count >= self._policy["max_gossip_per_window"]
            ):
                reason = "quota_gossip"
            elif traffic_class == "task" and (
                class_count >= self._policy["max_task_per_window"]
            ):
                reason = "quota_task"
            elif len(state.window) >= self._policy["max_messages_per_window"]:
                reason = "quota_total"
            elif window_bytes + size_bytes > self._policy["max_bytes_per_window"]:
                reason = "quota_bytes"
            if reason is not None:
                state.throttled += 1
                state.throttled_until = (
                    moment + self._policy["throttle_cooldown_seconds"]
                )
                self._emit(
                    "rate.node_throttled",
                    sender,
                    {
                        "sender": sender,
                        "reason": reason,
                        "cooldown_seconds": self._policy[
                            "throttle_cooldown_seconds"
                        ],
                    },
                )
                return self._record(
                    sender=sender,
                    traffic_class=traffic_class,
                    size_bytes=size_bytes,
                    verdict="throttled",
                    reason=reason,
                )
            state.window.append((moment, traffic_class, size_bytes))
            state.admitted += 1
            state.bytes_admitted += size_bytes
            return self._record(
                sender=sender,
                traffic_class=traffic_class,
                size_bytes=size_bytes,
                verdict="admitted",
                reason=None,
            )

    def admit_message(self, message) -> dict:
        """Judge one raw fabric message at the door:
        its sender is the message's ``from_node``,
        its class follows :func:`classify_message`,
        and its size is :func:`message_size_bytes`.
        A message with no usable sender or class is
        throttled ``malformed``."""
        if not isinstance(message, dict):
            return self.admit(None, None, message_size_bytes(message))
        sender = message.get("from_node")
        traffic_class = classify_message(message)
        if traffic_class is None:
            return self.admit(sender, None, message_size_bytes(message))
        return self.admit(sender, traffic_class, message_size_bytes(message))

    def require_admitted(self, sender, traffic_class, size_bytes) -> dict:
        """The door for callers that want a refusal
        raised: returns the judgement record when the
        message is admitted, raises :class:`RateError`
        carrying the throttling reason when it is
        not."""
        record = self.admit(sender, traffic_class, size_bytes)
        if record["verdict"] != "admitted":
            raise RateError(
                f"message from {record['sender']!r} throttled at the "
                f"rate door: {record['reason']}",
                reason=record["reason"],
            )
        return record

    def throttled_until(self, sender: str) -> str | None:
        """The moment ``sender``'s throttle cooldown
        at this door ends, or ``None`` when the sender
        is not throttled now."""
        with self._lock:
            state = self._senders.get(sender)
            if state is None or state.throttled_until is None:
                return None
            if self._now().timestamp() >= state.throttled_until:
                return None
            return _fmt(datetime.fromtimestamp(state.throttled_until, tz=UTC))

    # -- report ---------------------------------------------------

    def report(self) -> dict:
        with self._lock:
            moment = self._now().timestamp()
            nodes = []
            for node_id in sorted(self._senders):
                state = self._senders[node_id]
                self._prune(state, moment)
                nodes.append(
                    {
                        "node_id": node_id,
                        "admitted": state.admitted,
                        "throttled": state.throttled,
                        "bytes_admitted": state.bytes_admitted,
                        "window_messages": len(state.window),
                        "window_bytes": sum(s for _t, _c, s in state.window),
                        "throttled_until": (
                            _fmt(
                                datetime.fromtimestamp(
                                    state.throttled_until, tz=UTC
                                )
                            )
                            if state.throttled_until is not None
                            and moment < state.throttled_until
                            else None
                        ),
                    }
                )
            return validate_report(
                {
                    "version": 1,
                    "at": _fmt(self._now()),
                    "simulated": True,
                    "policy": copy.deepcopy(self._policy),
                    "nodes": nodes,
                    "records": copy.deepcopy(self._records),
                    "seen": self._seen,
                    "admitted": self._admitted,
                    "throttled": self._throttled,
                    "throttled_reasons": dict(
                        sorted(self._throttled_reasons.items())
                    ),
                }
            )
