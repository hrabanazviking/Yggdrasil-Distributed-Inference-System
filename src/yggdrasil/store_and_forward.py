"""Store-and-forward — Y-061.

The task channel (Y-057) carries a task to a node
that is there to receive it. A node that is asleep
— powered down, travelling, or simply off the mesh
for a while — is not there, and a task aimed at it
would have nowhere to land. This slice is the
fabric's waiting room for such tasks: a task for a
sleeping node waits in the fabric with a TTL;
expiry is an event, not a silence.

- **A task waits, whole, under a TTL.**
  :meth:`StoreAndForward.queue` validates the task
  as a Y-007 ``task_envelope`` v1 and holds it,
  whole, for its named recipient. The wait is
  bounded by a time-to-live: the queueing may name
  ``ttl_seconds``, otherwise the policy's
  ``default_ttl_seconds`` applies, and no task ever
  waits past the policy's ``max_ttl_seconds`` — a
  TTL outside ``1..max_ttl_seconds`` is refused
  ``ttl_out_of_bounds``. A task id the mailbox
  already knows — held, delivered, expired, or
  cancelled — is refused ``duplicate_task``: an id
  is spent once, so a held task can never be
  quietly replaced by a second task wearing its
  name.
- **The waiting room is bounded, per side.** The
  policy caps the mailbox as a whole
  (``max_held_tasks``, refused ``mailbox_full``),
  what waits for one recipient
  (``max_held_per_recipient``, refused
  ``recipient_full`` — one sleeping node's pile
  cannot crowd out another's), and what one sender
  may leave waiting (``max_held_per_sender``,
  refused ``sender_full`` — one sender cannot fill
  the room alone). Every refusal is raised as a
  :class:`StoreForwardError` carrying its reason,
  counted in the report, and evented; a refused
  task is held nowhere.
- **Waking delivers, exactly once, in order.**
  :meth:`StoreAndForward.deliver` is the sleeping
  node's waking call: it hands over every task
  still held for that recipient, in the order they
  were queued, and marks each delivered. A second
  call hands over nothing — delivery is once, by
  the schema-constant ``deliver_once`` law, never
  a replay. Tasks for other recipients are
  untouched, and a task whose TTL ran out in the
  night is not among them: it expired, below.
- **Expiry is an event, not a silence.** A held
  task whose TTL runs out is expired — swept on
  any mailbox act and on an explicit
  :meth:`StoreAndForward.expire` call, never
  delivered afterwards, and never quietly dropped:
  the expiry is evented
  (``store_forward.task_expired``), counted apart,
  recorded in the report, and written as a
  validated ``store_and_forward_notice`` v1 the
  task's sender can read back with
  :meth:`StoreAndForward.expiry_notices_for`. The
  sender learns its task died of age, by name,
  instead of waiting on a silence.
- **A sender may take its task back.**
  :meth:`StoreAndForward.cancel` closes a held
  task at its sender's word: a cancel by anyone
  else is refused ``not_sender``, a cancel of a
  task no longer held is refused ``not_held``, and
  a cancelled task is never delivered — its end is
  evented and counted like every other.
- **Every standing is a record.** Each queued task
  is a validated ``store_and_forward_record`` v1
  (identifiers and times only, never payload
  content), and :meth:`StoreAndForward.report`
  answers a validated ``store_and_forward_report``
  v1: the policy, every record in queue order,
  every expiry notice, how many tasks stand held,
  and the counted totals with refusals apart by
  name. Events are Y-013 envelopes from the
  ``fabric`` component.

This mailbox is a formalisation standing beside
the task channel (Y-057), partitions (Y-058),
provenance (Y-059), and rate discipline (Y-060) —
not a rewiring of any of them: it holds Y-007
envelopes for later delivery and decides nothing
about routing, claiming, or execution. No
node-agent seam (the standing Part III precedent).
Honesty: the mailbox is an in-process holding —
``simulated: true`` is a schema constant on its
report, no socket opened, no LAN crossed, and no
sleeping hardware was woken; real carriage is
Y-062, fabric observability Y-064, hardware proof
Part VIII's.
"""

import copy
import json
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.observability import make_event

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

_NODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


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


_POLICY_VALIDATOR = _validator("store_and_forward_policy.schema.json")
_RECORD_VALIDATOR = _validator("store_and_forward_record.schema.json")
_NOTICE_VALIDATOR = _validator("store_and_forward_notice.schema.json")
_REPORT_VALIDATOR = _validator("store_and_forward_report.schema.json")
_TASK_VALIDATOR = _validator("task_envelope.schema.json")


class StoreForwardError(Exception):
    """The mailbox's own failure: a record that fails
    its schema, or a refused act. A refusal carries
    its counted reason in ``reason``."""

    def __init__(self, message: str, *, reason: str | None = None):
        super().__init__(message)
        self.reason = reason


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record, kind: str):
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise StoreForwardError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_policy(record: dict) -> dict:
    _validate(_POLICY_VALIDATOR, record, "store-and-forward policy")
    if record["default_ttl_seconds"] > record["max_ttl_seconds"]:
        raise StoreForwardError(
            "store-and-forward policy fails its law: default_ttl_seconds "
            f"{record['default_ttl_seconds']} exceeds max_ttl_seconds "
            f"{record['max_ttl_seconds']}"
        )
    return record


def validate_record(record: dict) -> dict:
    return _validate(_RECORD_VALIDATOR, record, "store-and-forward record")


def validate_notice(record: dict) -> dict:
    return _validate(_NOTICE_VALIDATOR, record, "store-and-forward notice")


def validate_report(record: dict) -> dict:
    return _validate(_REPORT_VALIDATOR, record, "store-and-forward report")


def validate_task(record: dict) -> dict:
    return _validate(_TASK_VALIDATOR, record, "task envelope")


def default_policy() -> dict:
    """The waiting room as data: a queued task waits
    one hour by default and never more than a day;
    the room holds at most 256 tasks, at most 64 for
    one recipient and at most 128 from one sender.
    A policy, not a fact about a LAN."""
    return validate_policy(
        {
            "version": 1,
            "default_ttl_seconds": 3600,
            "max_ttl_seconds": 86400,
            "max_held_tasks": 256,
            "max_held_per_recipient": 64,
            "max_held_per_sender": 128,
            "expiry_evented": True,
            "deliver_once": True,
        }
    )


class _Held:
    def __init__(self, task: dict, sender: str, recipient: str, queued_at: datetime, ttl_seconds: int):
        self.task = copy.deepcopy(task)
        self.sender = sender
        self.recipient = recipient
        self.queued_at = queued_at
        self.ttl_seconds = ttl_seconds
        self.status = "held"
        self.delivered_at: datetime | None = None

    @property
    def expires_at(self) -> datetime:
        return self.queued_at + timedelta(seconds=self.ttl_seconds)

    def record(self) -> dict:
        return validate_record(
            {
                "version": 1,
                "task_id": self.task["id"],
                "sender": self.sender,
                "recipient": self.recipient,
                "status": self.status,
                "queued_at": _fmt(self.queued_at),
                "ttl_seconds": self.ttl_seconds,
                "expires_at": _fmt(self.expires_at),
                "delivered_at": (
                    _fmt(self.delivered_at) if self.delivered_at is not None else None
                ),
            }
        )


class StoreAndForward:
    """The fabric's waiting room: tasks for sleeping
    nodes, held whole under a TTL, delivered exactly
    once in queue order when the node wakes, and
    expired as an event — never a silence — when the
    TTL runs out. ``now`` is injectable so runs are
    replayable."""

    def __init__(self, policy: dict | None = None, *, now=None):
        self._policy = validate_policy(
            copy.deepcopy(policy) if policy is not None else default_policy()
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._held: dict[str, _Held] = {}
        self._order: list[str] = []
        self._notices: list[dict] = []
        self._events: list[dict] = []
        self._event_seq = 0
        self._queued = 0
        self._delivered = 0
        self._expired = 0
        self._cancelled = 0
        self._refused = 0
        self._refused_reasons: dict[str, int] = {}

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

    # -- membership of names ---------------------------------

    @staticmethod
    def _check_node_id(node_id: str) -> str:
        if not isinstance(node_id, str) or not _NODE_RE.match(node_id):
            raise StoreForwardError(f"node_id must be a node name, got {node_id!r}")
        return node_id

    # -- refusals ----------------------------------------------

    def _refuse(self, act: str, node_id: str, task_id, reason: str, message: str):
        self._refused += 1
        self._refused_reasons[reason] = self._refused_reasons.get(reason, 0) + 1
        self._emit(
            f"store_forward.{act}_refused",
            node_id,
            {"task": task_id or "none", "reason": reason},
        )
        raise StoreForwardError(message, reason=reason)

    # -- expiry --------------------------------------------------

    def _sweep_expired(self) -> list[dict]:
        """Expire every held task whose TTL has run
        out at this moment. Each expiry is evented,
        counted, and written as a notice for its
        sender. Returns the records expired by this
        sweep, in queue order."""
        moment = self._now()
        expired = []
        for task_id in self._order:
            held = self._held[task_id]
            if held.status != "held" or moment < held.expires_at:
                continue
            held.status = "expired"
            self._expired += 1
            notice = validate_notice(
                {
                    "version": 1,
                    "task_id": task_id,
                    "sender": held.sender,
                    "recipient": held.recipient,
                    "queued_at": _fmt(held.queued_at),
                    "expired_at": _fmt(moment),
                    "ttl_seconds": held.ttl_seconds,
                    "reason": "ttl_expired",
                }
            )
            self._notices.append(notice)
            self._emit(
                "store_forward.task_expired",
                held.sender,
                {
                    "task": task_id,
                    "recipient": held.recipient,
                    "reason": "ttl_expired",
                },
            )
            expired.append(held.record())
        return expired

    def expire(self) -> list[dict]:
        """Sweep the room at this moment: every held
        task whose TTL has run out expires — evented,
        counted, and noticed to its sender. Returns
        the records this sweep expired."""
        with self._lock:
            return self._sweep_expired()

    # -- queue ---------------------------------------------------

    def queue(self, sender: str, recipient: str, task: dict, ttl_seconds=None) -> dict:
        """Hold one Y-007 task for a sleeping
        recipient. Returns the task's mailbox
        record."""
        sender = self._check_node_id(sender)
        recipient = self._check_node_id(recipient)
        with self._lock:
            self._sweep_expired()
            try:
                validate_task(copy.deepcopy(task))
            except StoreForwardError as exc:
                self._refuse("queue", sender, None, "malformed_task", str(exc))
            task_id = task["id"]
            if task_id in self._held:
                self._refuse(
                    "queue", sender, task_id, "duplicate_task",
                    f"task {task_id} is already known to the mailbox",
                )
            ttl = (
                self._policy["default_ttl_seconds"]
                if ttl_seconds is None
                else ttl_seconds
            )
            if (
                not isinstance(ttl, int)
                or isinstance(ttl, bool)
                or ttl < 1
                or ttl > self._policy["max_ttl_seconds"]
            ):
                self._refuse(
                    "queue", sender, task_id, "ttl_out_of_bounds",
                    f"ttl {ttl_seconds!r} is outside 1.."
                    f"{self._policy['max_ttl_seconds']} seconds",
                )
            held_now = [h for h in self._held.values() if h.status == "held"]
            if len(held_now) >= self._policy["max_held_tasks"]:
                self._refuse(
                    "queue", sender, task_id, "mailbox_full",
                    f"the mailbox already holds {len(held_now)} tasks, "
                    f"the policy bound {self._policy['max_held_tasks']}",
                )
            for_recipient = sum(1 for h in held_now if h.recipient == recipient)
            if for_recipient >= self._policy["max_held_per_recipient"]:
                self._refuse(
                    "queue", sender, task_id, "recipient_full",
                    f"{for_recipient} tasks already wait for {recipient}, "
                    f"the policy bound {self._policy['max_held_per_recipient']}",
                )
            from_sender = sum(1 for h in held_now if h.sender == sender)
            if from_sender >= self._policy["max_held_per_sender"]:
                self._refuse(
                    "queue", sender, task_id, "sender_full",
                    f"{from_sender} tasks from {sender} already wait, "
                    f"the policy bound {self._policy['max_held_per_sender']}",
                )
            held = _Held(task, sender, recipient, self._now(), ttl)
            self._held[task_id] = held
            self._order.append(task_id)
            self._queued += 1
            self._emit(
                "store_forward.task_queued",
                sender,
                {"task": task_id, "recipient": recipient, "ttl_seconds": ttl},
            )
            return held.record()

    # -- delivery --------------------------------------------------

    def deliver(self, recipient: str) -> list[dict]:
        """The waking call: hand over every task still
        held for ``recipient``, in queue order, each
        exactly once. Returns the Y-007 task envelopes
        themselves (copies). Expired tasks are not
        handed over — they expired, with a notice."""
        recipient = self._check_node_id(recipient)
        with self._lock:
            self._sweep_expired()
            handed = []
            for task_id in self._order:
                held = self._held[task_id]
                if held.status != "held" or held.recipient != recipient:
                    continue
                held.status = "delivered"
                held.delivered_at = self._now()
                self._delivered += 1
                self._emit(
                    "store_forward.task_delivered",
                    recipient,
                    {"task": task_id, "sender": held.sender},
                )
                handed.append(copy.deepcopy(held.task))
            return handed

    def held_for(self, recipient: str) -> list[dict]:
        """The records of the tasks still held for one
        recipient, in queue order, at this moment."""
        recipient = self._check_node_id(recipient)
        with self._lock:
            self._sweep_expired()
            return [
                self._held[task_id].record()
                for task_id in self._order
                if self._held[task_id].status == "held"
                and self._held[task_id].recipient == recipient
            ]

    # -- cancel -----------------------------------------------------

    def cancel(self, task_id: str, sender: str) -> dict:
        """Take a held task back at its sender's word.
        Returns the task's record, now cancelled. A
        cancelled task is never delivered."""
        sender = self._check_node_id(sender)
        with self._lock:
            self._sweep_expired()
            held = self._held.get(task_id) if isinstance(task_id, str) else None
            if held is None:
                self._refuse(
                    "cancel", sender, task_id, "unknown_task",
                    f"task {task_id!r} is not known to the mailbox",
                )
            if held.sender != sender:
                self._refuse(
                    "cancel", sender, task_id, "not_sender",
                    f"task {task_id} was queued by {held.sender}, "
                    f"not by {sender}",
                )
            if held.status != "held":
                self._refuse(
                    "cancel", sender, task_id, "not_held",
                    f"task {task_id} is {held.status}, not held",
                )
            held.status = "cancelled"
            self._cancelled += 1
            self._emit(
                "store_forward.task_cancelled",
                sender,
                {"task": task_id, "recipient": held.recipient},
            )
            return held.record()

    # -- lookups ------------------------------------------------------

    def record_for(self, task_id: str) -> dict:
        with self._lock:
            self._sweep_expired()
            held = self._held.get(task_id)
            if held is None:
                raise StoreForwardError(
                    f"task {task_id!r} is not known to the mailbox",
                    reason="unknown_task",
                )
            return held.record()

    def expiry_notices_for(self, sender: str) -> list[dict]:
        """The expiry notices written for one sender,
        in the order its tasks expired. Expiry is an
        event, not a silence — this is where the
        sender reads it."""
        sender = self._check_node_id(sender)
        with self._lock:
            self._sweep_expired()
            return [
                copy.deepcopy(notice)
                for notice in self._notices
                if notice["sender"] == sender
            ]

    # -- report ---------------------------------------------------------

    def report(self) -> dict:
        with self._lock:
            self._sweep_expired()
            records = [self._held[task_id].record() for task_id in self._order]
            return validate_report(
                {
                    "version": 1,
                    "at": _fmt(self._now()),
                    "simulated": True,
                    "policy": copy.deepcopy(self._policy),
                    "records": records,
                    "notices": copy.deepcopy(self._notices),
                    "held": sum(1 for r in records if r["status"] == "held"),
                    "queued": self._queued,
                    "delivered": self._delivered,
                    "expired": self._expired,
                    "cancelled": self._cancelled,
                    "refused": self._refused,
                    "refused_reasons": dict(sorted(self._refused_reasons.items())),
                }
            )
