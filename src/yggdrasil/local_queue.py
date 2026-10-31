"""Local queue — Y-031.

Until this slice a node could *run* work (the sandbox,
Y-029) and *lease* work (the lifecycle, Y-021), but it had
nowhere to *hold* work: a task that arrived while the node
was busy had no honest place to wait, and the health report's
``queue_depth`` stayed 0 because claiming a queue before one
existed would have been a lie. This slice is the holding
pen, per node:

- **Priorities from the task's own word.** A task's priority
  is its Y-007 ``criticality`` — ``critical`` before
  ``standard`` before ``casual`` — in the order the task
  contract has always named them. Within one priority the
  queue is strictly FIFO by its own monotonic ``seq``: no
  task jumps its same-priority elders, and no field a caller
  can set (its deadline, its payload) reorders the line.
  Priority is not a caller-supplied knob bolted onto the
  envelope; it is derived at enqueue time and recorded on the
  validated ``queued_task`` v1 entry, where an operator can
  audit exactly why a task stands where it stands.
- **Bounded, and the bound bites honestly.** A ``queue_policy``
  v1 names the node's ``max_depth``. A task offered to a full
  queue is refused with a ``QueueError`` naming the Y-007
  ``queue_full`` code (the error it wraps carries
  ``error_code = "queue_full"``); a task id already held is
  refused as a duplicate. Both refusals are *counted* in the
  report's ``rejected`` total and recorded as
  ``queue.task_rejected`` events — a busy node that is
  turning work away says so, in numbers, instead of dropping
  tasks silently.
- **A busy node says how busy, in numbers.** ``report()``
  answers the validated ``queue_report`` v1: depth,
  capacity, free slots, utilisation (depth ÷ capacity,
  measured, never estimated), the depth broken down
  ``by_priority``, the identity of the next task in service
  order, and the oldest held task with its *measured* wait
  (the injected clock against its enqueue moment — rounded,
  never guessed). Lifetime totals — enqueued, dequeued,
  cancelled, rejected — ride the same report and only rise.
- **Held tasks are tasks, not copies of their payloads.**
  The queue keeps the validated task envelope so
  ``dequeue()`` can hand the work itself to an executor, but
  the entry, the report, and every event carry only
  identifiers, kinds, priorities, and counts — the task's
  payload content never enters the queue's own records.
- **Cancellation and service.** ``cancel(task_id)`` removes
  a held task (False for one not held — a running or finished
  task cannot be cancelled here; running cancellation is the
  sandbox's, Y-029). ``peek()`` names the next task without
  taking it. ``dequeue()`` takes the head of the service
  order and returns ``(task, entry)`` with the entry's wait
  now closed. All mutations are lock-guarded, so an enqueue
  from a fabric thread (Part III) and a dequeue from an
  executor thread cannot interleave into a torn line.

The node agent (wired at this slice) grows the seams:
``local_queue=`` at construction, ``queue_task`` (joined
only — a draining node takes no new work, exactly as its
leases have always said), ``cancel_queued_task``,
``next_queued_task`` (a peek), and
``execute_next_queued`` — peek, run through the sandbox
behind the agent's lease, and only then take the task off
the queue, so a run the governor refuses stays held rather
than vanishing. ``health()`` and the Y-030 heartbeat now
report the queue's true depth when one is wired, and stay 0
when none is, as before.

Honesty: the queue is in-process memory on this node. It is
not durable — a restarted agent holds nothing, and no task
is claimed held across a restart (a durable journal is
Y-039's node journal, a different record); it is not a mesh
queue — no peer sees or shares this line (fabric carriage is
Part III). Its clock is injectable so waits are provable
without real waiting. Nothing here has run on named
hardware; the citizens are Y-033 – Y-035.
"""

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.observability import make_event

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

# An operator's starting bound, not a fact about any machine:
# sixty-four tasks held per node.
DEFAULT_POLICY = {"version": 1, "max_depth": 64}

# Service order: the task contract's criticality vocabulary
# (Y-007), most urgent first. Rank is recorded on every entry
# so the order a report shows is the order that was applied.
PRIORITY_RANK = {"critical": 0, "standard": 1, "casual": 2}


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


_POLICY_VALIDATOR = _validator("queue_policy.schema.json")
_ENTRY_VALIDATOR = _validator("queued_task.schema.json")
_REPORT_VALIDATOR = _validator("queue_report.schema.json")
_TASK_VALIDATOR = _validator("task_envelope.schema.json")


class QueueError(Exception):
    """Any local-queue failure: a bad policy, a malformed
    task, a duplicate, a full queue (``error_code`` carries
    the Y-007 name where one applies)."""

    def __init__(self, message: str, *, error_code: str | None = None):
        super().__init__(message)
        self.error_code = error_code


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise QueueError(f"{kind} fails its schema: {errors[0].message}")
    return record


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_policy(policy: dict) -> dict:
    """A queue policy against its schema."""
    return _validate(_POLICY_VALIDATOR, policy, "queue policy")


def default_policy() -> dict:
    """The starting bound, validated — an operator's policy,
    not a measured fact about any machine."""
    return validate_policy(dict(DEFAULT_POLICY))


def validate_entry(entry: dict) -> dict:
    """A queued-task entry against its schema."""
    return _validate(_ENTRY_VALIDATOR, entry, "queued task")


def validate_report(report: dict) -> dict:
    """A queue report against its schema."""
    return _validate(_REPORT_VALIDATOR, report, "queue report")


class LocalQueue:
    """One node's local queue: tasks held in priority order,
    with the node's busyness reported in numbers.

    ``now`` is an injectable datetime clock — enqueue stamps
    and wait measurements both read it, so a test can pin and
    advance time without waiting.
    """

    def __init__(self, node_id: str, policy: dict | None = None, *, now=None):
        if not isinstance(node_id, str) or not node_id:
            raise QueueError("a local queue needs a node_id")
        self.node_id = node_id
        self._policy = validate_policy(dict(policy) if policy is not None else default_policy())
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        # task_id -> (entry, task, enqueued_moment)
        self._held: dict[str, tuple[dict, dict, datetime]] = {}
        self._seq = 0
        self._events: list[dict] = []
        self._event_seq = 0
        self._totals = {"enqueued": 0, "dequeued": 0, "cancelled": 0, "rejected": 0}

    # -- facts ----------------------------------------------------------

    @property
    def policy(self) -> dict:
        return dict(self._policy)

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    @property
    def totals(self) -> dict:
        return dict(self._totals)

    def __len__(self) -> int:
        with self._lock:
            return len(self._held)

    @property
    def depth(self) -> int:
        return len(self)

    @property
    def is_full(self) -> bool:
        with self._lock:
            return len(self._held) >= self._policy["max_depth"]

    # -- internals --------------------------------------------------------

    def _emit(self, type: str, payload: dict, *, task_id: str) -> dict:
        event = make_event(
            type=type,
            node_id=self.node_id,
            component="node_agent",
            payload=payload,
            seq=self._event_seq,
            task_id=task_id,
            at=self._now(),
        )
        self._event_seq += 1
        self._events.append(event)
        return event

    def _service_order(self) -> list[tuple[dict, dict, datetime]]:
        """Held records in service order: priority rank, then
        the queue's own sequence (FIFO within a priority)."""
        return sorted(
            self._held.values(),
            key=lambda record: (record[0]["priority_rank"], record[0]["seq"]),
        )

    def _reject(self, task_id: str, reason: str, *, error_code: str | None = None) -> None:
        self._totals["rejected"] += 1
        self._emit(
            "queue.task_rejected",
            {"reason": reason, "error_code": error_code, "depth": len(self._held)},
            task_id=task_id,
        )

    # -- holding work -----------------------------------------------------

    def enqueue(self, task: dict) -> dict:
        """Hold one validated task; return its validated entry.

        Priority is derived from the task's criticality (the
        envelope's own default, ``standard``, when it names
        none). A duplicate id and a full queue are refused by
        name, counted, and recorded — never silently dropped.
        """
        _validate(_TASK_VALIDATOR, task, "task envelope")
        task_id = task["id"]
        with self._lock:
            if task_id in self._held:
                self._reject(task_id, "duplicate")
                raise QueueError(f"task {task_id} is already held in this node's queue")
            if len(self._held) >= self._policy["max_depth"]:
                self._reject(task_id, "queue_full", error_code="queue_full")
                raise QueueError(
                    f"queue_full: this node's queue already holds "
                    f"{len(self._held)} task(s) (max_depth {self._policy['max_depth']})",
                    error_code="queue_full",
                )
            criticality = task.get("criticality", "standard")
            moment = self._now()
            requirements = task.get("requirements") or {}
            entry = _validate(
                _ENTRY_VALIDATOR,
                {
                    "version": 1,
                    "task_id": task_id,
                    "node_id": self.node_id,
                    "kind": task["kind"],
                    "caller": task["caller"],
                    "priority": criticality,
                    "priority_rank": PRIORITY_RANK[criticality],
                    "criticality": criticality,
                    "model": requirements.get("model"),
                    "enqueued_at": _fmt(moment),
                    "seq": self._seq,
                    "deadline_ms": task.get("deadline_ms"),
                },
                "queued task",
            )
            self._seq += 1
            self._held[task_id] = (entry, dict(task), moment)
            self._totals["enqueued"] += 1
            self._emit(
                "queue.task_enqueued",
                {
                    "priority": entry["priority"],
                    "kind": entry["kind"],
                    "depth": len(self._held),
                },
                task_id=task_id,
            )
            return dict(entry)

    def entries(self) -> list[dict]:
        """Every held task's entry, in service order."""
        with self._lock:
            return [dict(record[0]) for record in self._service_order()]

    def peek(self) -> dict | None:
        """The next entry in service order, without taking it;
        None when the queue is empty."""
        with self._lock:
            ordered = self._service_order()
            return dict(ordered[0][0]) if ordered else None

    def held_task(self, task_id: str) -> dict | None:
        """The held task envelope itself, for the executor
        that is about to serve it (the node agent's
        execute-next path peeks an entry, runs its task, and
        only then takes it off the queue). A copy — mutating
        it changes nothing held. None when not held."""
        with self._lock:
            record = self._held.get(task_id)
            return dict(record[1]) if record is not None else None

    def wait_ms(self, task_id: str) -> float | None:
        """How long one held task has waited, measured on the
        queue's clock; None for a task not held."""
        with self._lock:
            record = self._held.get(task_id)
            if record is None:
                return None
            return round(max((self._now() - record[2]).total_seconds() * 1000.0, 0.0), 3)

    def _take(self, task_id: str, *, verb: str) -> tuple[dict, dict, datetime]:
        record = self._held.pop(task_id, None)
        if record is None:
            raise QueueError(f"task {task_id} is not held in this node's queue")
        entry, task, moment = record
        waited = round(max((self._now() - moment).total_seconds() * 1000.0, 0.0), 3)
        self._totals["dequeued" if verb == "dequeued" else "cancelled"] += 1
        self._emit(
            f"queue.task_{verb}",
            {"priority": entry["priority"], "wait_ms": waited, "depth": len(self._held)},
            task_id=task_id,
        )
        return entry, task, moment

    def dequeue(self) -> tuple[dict, dict] | None:
        """Take the head of the service order: ``(task, entry)``
        for the executor, or None when the queue is empty."""
        with self._lock:
            ordered = self._service_order()
            if not ordered:
                return None
            entry, task, _moment = self._take(ordered[0][0]["task_id"], verb="dequeued")
            return dict(task), dict(entry)

    def remove(self, task_id: str) -> dict | None:
        """Take one named held task off the queue as served
        (the dequeue bookkeeping, by id — the agent's
        execute-next path, where the head was peeked, run,
        and only then taken). Returns its entry, or None."""
        with self._lock:
            if task_id not in self._held:
                return None
            entry, _task, _moment = self._take(task_id, verb="dequeued")
            return dict(entry)

    def cancel(self, task_id: str) -> bool:
        """Remove one held task without serving it. False when
        it is not held — a running task is the sandbox's to
        cancel (Y-029), a finished one is history."""
        with self._lock:
            if task_id not in self._held:
                return False
            self._take(task_id, verb="cancelled")
            return True

    # -- the numbers --------------------------------------------------------

    def report(self) -> dict:
        """The validated queue_report v1: this node's
        busyness, in numbers, at this moment."""
        with self._lock:
            ordered = self._service_order()
            depth = len(ordered)
            capacity = self._policy["max_depth"]
            by_priority = {"critical": 0, "standard": 0, "casual": 0}
            for entry, _task, _moment in ordered:
                by_priority[entry["priority"]] += 1
            oldest_wait = None
            oldest_id = None
            if ordered:
                # The longest wait, not the head's: priority
                # can keep a casual elder waiting behind newer
                # critical work, and the report says so.
                moment_now = self._now()
                waits = [
                    (
                        round(max((moment_now - record[2]).total_seconds() * 1000.0, 0.0), 3),
                        record[0]["task_id"],
                    )
                    for record in ordered
                ]
                oldest_wait, oldest_id = max(waits)
            return _validate(
                _REPORT_VALIDATOR,
                {
                    "version": 1,
                    "node_id": self.node_id,
                    "at": _fmt(self._now()),
                    "depth": depth,
                    "capacity": capacity,
                    "free_slots": capacity - depth,
                    "utilisation": round(depth / capacity, 3) if capacity else 0.0,
                    "is_full": depth >= capacity,
                    "by_priority": by_priority,
                    "oldest_task_id": oldest_id,
                    "oldest_wait_ms": oldest_wait,
                    "next_task_id": ordered[0][0]["task_id"] if ordered else None,
                    "totals": dict(self._totals),
                },
                "queue report",
            )


__all__ = [
    "DEFAULT_POLICY",
    "PRIORITY_RANK",
    "LocalQueue",
    "QueueError",
    "default_policy",
    "validate_entry",
    "validate_policy",
    "validate_report",
]
