"""The task channel — Y-057.

Until this slice the fabric (Y-050) could carry any
message, the registry (Y-053) could say who can do what,
and the sandbox (Y-029) could run a task on one node —
but a task itself had no road between nodes. This slice
is that road: task offers, claims, and result streams
ride the fabric with the Y-007 envelopes, and
backpressure is explicit.

- **Offers ride the topic.** ``offer(offerer, task)``
  validates the task as a Y-007 ``task_envelope`` v1,
  records it open, and publishes it on the fabric topic
  ``tasks.offers`` — every joined node sees the offer in
  its fabric inbox as a validated ``fabric_message``
  whose payload is the envelope itself. An offer for a
  task id already known (open or closed) is refused
  ``duplicate_task``; an offer past the policy's
  ``max_open_tasks`` is refused ``channel_full``. Both
  refusals are counted and evented, never silent.
- **First claim wins, by name.** ``claim(task_id,
  claimant)`` is a direct fabric message to the offerer.
  The first claim on an offered task takes it; a claim
  on a claimed or closed task is refused
  ``already_claimed`` / ``task_closed``, a claim on an
  unknown task ``unknown_task``, and a claim by the
  offerer of its own task ``self_claim`` — a node does
  not need the channel to hand itself work. Claims are
  the channel's word only: whether the claimant *should*
  take the task is Verðandi routing (Part IV), and
  capability fitness is Y-038's placement law, neither
  re-decided here.
- **Streams ride direct, in order, under a window.**
  The claimant sends Y-007 ``stream_event`` v1 records
  with ``send_stream(task_id, claimant, event)``:
  each is a direct fabric message to the offerer and is
  appended to the channel's per-task stream in the
  order sent. Sequences must be consecutive from 0 —
  a gap or replay is refused ``out_of_order``; an event
  from anyone but the claimant is refused
  ``not_claimant``; an event for an unclaimed task is
  refused ``not_claimed``. **Backpressure is the
  policy's ``stream_window``:** a task may have at most
  that many delivered-but-unacknowledged events. The
  offerer frees the window with ``acknowledge(task_id,
  offerer, up_to_seq)`` (acks are cumulative and
  monotonic; acking past the last delivered seq is
  refused ``ack_ahead``). An event offered while the
  window is full is refused ``backpressure`` — counted
  apart in the report, evented, and NOT delivered and
  NOT silently dropped into a hidden buffer: the
  claimant is told, in the refusal, exactly how many
  events stand unacknowledged, and may resend the same
  seq once the offerer acks. A task's total stream is
  also bounded (``max_stream_events_per_task``),
  refused ``stream_too_long``.
- **One terminal result closes the task.**
  ``send_result(task_id, claimant, result)`` validates
  a Y-007 ``result_envelope`` v1 whose ``task_id`` must
  name this task and whose ``provenance.node_id`` must
  be the claimant — a result dressed in another node's
  name is refused ``provenance_mismatch``. The result
  rides a direct fabric message to the offerer, the
  task closes, and any later claim, stream event, ack,
  or second result is refused by name. Duplicate
  execution reconciliation across a partition is
  Y-058's law, not this slice's.

Every refusal is a :class:`TaskChannelError` naming its
reason (the reason string is also the error's
``reason`` attribute), counted in the report's
``refused_reasons`` and recorded as a
``task_channel.<act>_refused`` Y-013 event from the
``fabric`` component — names, ids, and counts only,
never payload content. ``report()`` answers a validated
``task_channel_report`` v1; ``stream_for(task_id)`` and
``result_for(task_id)`` hand the offerer exactly what
the channel carried.

This channel is a formalisation standing on Y-050's
fabric, not a rewiring of it: state here is the
channel's own bookkeeping over fabric deliveries.
Store-and-forward for a sleeping node is Y-061,
partition behaviour and duplicate-execution
reconciliation Y-058, message signing enforcement
Y-059, quotas Y-060, real-LAN carriage Y-062. No
node-agent seam (the Y-053/Y-055/Y-056 precedent).
Honesty: fully SIMULATED — the fabric is in-process,
no socket is opened, no LAN is crossed; what is proven
is the channel's offer/claim/stream/result discipline
and its explicit backpressure on the contract shapes.
"""

import copy
import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.fabric import Fabric
from yggdrasil.observability import make_event

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

OFFERS_TOPIC = "tasks.offers"

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


_POLICY_VALIDATOR = _validator("task_channel_policy.schema.json")
_RECORD_VALIDATOR = _validator("task_channel_record.schema.json")
_REPORT_VALIDATOR = _validator("task_channel_report.schema.json")
_TASK_VALIDATOR = _validator("task_envelope.schema.json")
_STREAM_VALIDATOR = _validator("stream_event.schema.json")
_RESULT_VALIDATOR = _validator("result_envelope.schema.json")


class TaskChannelError(Exception):
    """The task channel's own failure: a record that
    fails its schema, or a refused act. A refusal carries
    its counted reason in ``reason``."""

    def __init__(self, message: str, *, reason: str | None = None):
        super().__init__(message)
        self.reason = reason


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record, kind: str):
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise TaskChannelError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_policy(record: dict) -> dict:
    return _validate(_POLICY_VALIDATOR, record, "task channel policy")


def validate_record(record: dict) -> dict:
    return _validate(_RECORD_VALIDATOR, record, "task channel record")


def validate_report(record: dict) -> dict:
    return _validate(_REPORT_VALIDATOR, record, "task channel report")


def validate_task(record: dict) -> dict:
    return _validate(_TASK_VALIDATOR, record, "task envelope")


def validate_stream_event(record: dict) -> dict:
    return _validate(_STREAM_VALIDATOR, record, "stream event")


def validate_result(record: dict) -> dict:
    return _validate(_RESULT_VALIDATOR, record, "result envelope")


def default_policy() -> dict:
    """The channel's bounds as data: 64 tasks open at
    once, a backpressure window of 8 unacknowledged
    stream events per task, and at most 1,024 stream
    events per task in total. A policy, labelled as
    one — an operator's numbers, not facts about a LAN."""
    return validate_policy(
        {
            "version": 1,
            "max_open_tasks": 64,
            "stream_window": 8,
            "max_stream_events_per_task": 1024,
        }
    )


class _TaskState:
    def __init__(self, task: dict, offerer: str):
        self.task = copy.deepcopy(task)
        self.offerer = offerer
        self.claimant: str | None = None
        self.status = "offered"
        self.stream: list[dict] = []
        self.acked_seq = -1
        self.result: dict | None = None

    @property
    def last_seq(self) -> int:
        return len(self.stream) - 1

    def record(self) -> dict:
        return validate_record(
            {
                "task_id": self.task["id"],
                "offerer": self.offerer,
                "claimant": self.claimant,
                "status": self.status,
                "stream_events": len(self.stream),
                "last_seq": self.last_seq,
                "acked_seq": self.acked_seq,
                "result_status": (
                    self.result["status"] if self.result is not None else None
                ),
            }
        )


class TaskChannel:
    """Task offers, claims, and result streams over one
    :class:`~yggdrasil.fabric.Fabric`. ``fabric`` may be
    supplied (its nodes are then the caller's to manage
    only through this channel's ``join``/``leave`` for
    the channel's own bookkeeping to stay true); by
    default the channel makes its own. ``now`` is
    injectable so runs are replayable."""

    def __init__(self, policy: dict | None = None, *, fabric: Fabric | None = None, now=None):
        self._policy = validate_policy(
            copy.deepcopy(policy) if policy is not None else default_policy()
        )
        self._fabric = fabric if fabric is not None else Fabric(now=now)
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._nodes: set[str] = set()
        self._tasks: dict[str, _TaskState] = {}
        self._events: list[dict] = []
        self._event_seq = 0
        self._offers = 0
        self._offers_refused = 0
        self._claims = 0
        self._claims_refused = 0
        self._stream_sent = 0
        self._stream_refused = 0
        self._backpressure_refusals = 0
        self._results = 0
        self._results_refused = 0
        self._refused_reasons: dict[str, int] = {}

    # -- accessors -------------------------------------------

    @property
    def policy(self) -> dict:
        return copy.deepcopy(self._policy)

    @property
    def fabric(self) -> Fabric:
        return self._fabric

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

    # -- membership ------------------------------------------

    @staticmethod
    def _check_node_id(node_id: str) -> str:
        if not isinstance(node_id, str) or not _NODE_RE.match(node_id):
            raise TaskChannelError(f"node_id must be a node name, got {node_id!r}")
        return node_id

    def join(self, node_id: str) -> None:
        """Bring a node onto the channel: attached to the
        fabric and subscribed to the offers topic."""
        node_id = self._check_node_id(node_id)
        with self._lock:
            if node_id in self._nodes:
                raise TaskChannelError(f"node {node_id} is already on the channel")
            self._fabric.attach(node_id)
            self._fabric.subscribe(node_id, OFFERS_TOPIC)
            self._nodes.add(node_id)
            self._emit("task_channel.node_joined", node_id, {"node": node_id})

    def leave(self, node_id: str) -> None:
        node_id = self._check_node_id(node_id)
        with self._lock:
            if node_id not in self._nodes:
                raise TaskChannelError(f"node {node_id} is not on the channel")
            self._fabric.detach(node_id)
            self._nodes.discard(node_id)
            self._emit("task_channel.node_left", node_id, {"node": node_id})

    def _require_joined(self, node_id: str, act: str) -> None:
        if node_id not in self._nodes:
            raise TaskChannelError(
                f"node {node_id} is not on the channel; {act} refused",
                reason="not_joined",
            )

    # -- refusals ----------------------------------------------

    def _refuse(self, act: str, node_id: str, task_id, reason: str, message: str):
        counter = {
            "offer": "_offers_refused",
            "claim": "_claims_refused",
            "stream": "_stream_refused",
            "result": "_results_refused",
        }.get(act)
        if counter is not None:
            setattr(self, counter, getattr(self, counter) + 1)
        if act == "stream" and reason == "backpressure":
            self._backpressure_refusals += 1
        self._refused_reasons[reason] = self._refused_reasons.get(reason, 0) + 1
        self._emit(
            f"task_channel.{act}_refused",
            node_id,
            {"task": task_id or "none", "reason": reason},
        )
        raise TaskChannelError(message, reason=reason)

    def _state_for(self, act: str, node_id: str, task_id) -> _TaskState:
        state = self._tasks.get(task_id) if isinstance(task_id, str) else None
        if state is None:
            self._refuse(
                act, node_id, task_id, "unknown_task",
                f"task {task_id!r} is not known to the channel",
            )
        return state  # type: ignore[return-value]

    # -- offers ------------------------------------------------

    def offer(self, offerer: str, task: dict) -> dict:
        """Offer one task to the mesh. Returns the task's
        channel record."""
        offerer = self._check_node_id(offerer)
        with self._lock:
            self._require_joined(offerer, "offer")
            try:
                validate_task(copy.deepcopy(task))
            except TaskChannelError as exc:
                self._refuse("offer", offerer, None, "malformed_task", str(exc))
            task_id = task["id"]
            if task_id in self._tasks:
                self._refuse(
                    "offer", offerer, task_id, "duplicate_task",
                    f"task {task_id} is already known to the channel",
                )
            open_count = sum(
                1 for s in self._tasks.values() if s.status != "closed"
            )
            if open_count >= self._policy["max_open_tasks"]:
                self._refuse(
                    "offer", offerer, task_id, "channel_full",
                    f"the channel already holds {open_count} open tasks, "
                    f"the policy bound {self._policy['max_open_tasks']}",
                )
            state = _TaskState(task, offerer)
            self._tasks[task_id] = state
            self._fabric.publish(
                offerer,
                OFFERS_TOPIC,
                {"channel": "task_offer", "task": copy.deepcopy(task)},
            )
            self._offers += 1
            self._emit(
                "task_channel.task_offered", offerer, {"task": task_id}
            )
            return state.record()

    # -- claims --------------------------------------------------

    def claim(self, task_id: str, claimant: str) -> dict:
        """Claim one offered task. First claim wins.
        Returns the task's channel record."""
        claimant = self._check_node_id(claimant)
        with self._lock:
            self._require_joined(claimant, "claim")
            state = self._state_for("claim", claimant, task_id)
            if claimant == state.offerer:
                self._refuse(
                    "claim", claimant, task_id, "self_claim",
                    f"node {claimant} offered task {task_id} and cannot "
                    "claim its own offer over the channel",
                )
            if state.status == "closed":
                self._refuse(
                    "claim", claimant, task_id, "task_closed",
                    f"task {task_id} is already closed",
                )
            if state.status == "claimed":
                self._refuse(
                    "claim", claimant, task_id, "already_claimed",
                    f"task {task_id} is already claimed by {state.claimant}",
                )
            state.claimant = claimant
            state.status = "claimed"
            self._fabric.send(
                claimant,
                state.offerer,
                {"channel": "task_claim", "task_id": task_id, "claimant": claimant},
            )
            self._claims += 1
            self._emit(
                "task_channel.task_claimed", claimant, {"task": task_id}
            )
            return state.record()

    # -- streams -------------------------------------------------

    def send_stream(self, task_id: str, sender: str, event: dict) -> dict:
        """Carry one stream event from the claimant to
        the offerer, under the backpressure window.
        Returns the task's channel record."""
        sender = self._check_node_id(sender)
        with self._lock:
            self._require_joined(sender, "stream")
            state = self._state_for("stream", sender, task_id)
            if state.status == "closed":
                self._refuse(
                    "stream", sender, task_id, "task_closed",
                    f"task {task_id} is already closed",
                )
            if state.status != "claimed":
                self._refuse(
                    "stream", sender, task_id, "not_claimed",
                    f"task {task_id} has not been claimed",
                )
            if sender != state.claimant:
                self._refuse(
                    "stream", sender, task_id, "not_claimant",
                    f"task {task_id} is claimed by {state.claimant}, "
                    f"not by {sender}",
                )
            try:
                validate_stream_event(copy.deepcopy(event))
            except TaskChannelError as exc:
                self._refuse("stream", sender, task_id, "malformed_event", str(exc))
            if event["task_id"] != task_id:
                self._refuse(
                    "stream", sender, task_id, "task_mismatch",
                    f"stream event names task {event['task_id']}, "
                    f"not {task_id}",
                )
            expected = len(state.stream)
            if event["seq"] != expected:
                self._refuse(
                    "stream", sender, task_id, "out_of_order",
                    f"stream event seq {event['seq']} is not the next "
                    f"expected seq {expected} for task {task_id}",
                )
            if len(state.stream) >= self._policy["max_stream_events_per_task"]:
                self._refuse(
                    "stream", sender, task_id, "stream_too_long",
                    f"task {task_id} has already carried "
                    f"{len(state.stream)} stream events, the policy bound "
                    f"{self._policy['max_stream_events_per_task']}",
                )
            unacked = len(state.stream) - (state.acked_seq + 1)
            if unacked >= self._policy["stream_window"]:
                self._refuse(
                    "stream", sender, task_id, "backpressure",
                    f"backpressure: task {task_id} has {unacked} "
                    f"unacknowledged stream events, the policy window "
                    f"{self._policy['stream_window']}; the event was "
                    "not delivered — resend it after the offerer "
                    "acknowledges",
                )
            self._fabric.send(
                sender,
                state.offerer,
                {
                    "channel": "task_stream",
                    "task_id": task_id,
                    "event": copy.deepcopy(event),
                },
            )
            state.stream.append(copy.deepcopy(event))
            self._stream_sent += 1
            return state.record()

    def acknowledge(self, task_id: str, offerer: str, up_to_seq: int) -> dict:
        """The offerer acknowledges stream events up to
        ``up_to_seq`` (cumulative), freeing the
        backpressure window. Returns the record."""
        offerer = self._check_node_id(offerer)
        with self._lock:
            self._require_joined(offerer, "acknowledge")
            state = self._state_for("stream", offerer, task_id)
            if offerer != state.offerer:
                self._refuse(
                    "stream", offerer, task_id, "not_offerer",
                    f"task {task_id} was offered by {state.offerer}, "
                    f"not by {offerer}",
                )
            if state.status == "closed":
                self._refuse(
                    "stream", offerer, task_id, "task_closed",
                    f"task {task_id} is already closed",
                )
            if not isinstance(up_to_seq, int) or up_to_seq <= state.acked_seq:
                self._refuse(
                    "stream", offerer, task_id, "ack_not_forward",
                    f"ack {up_to_seq!r} does not move task {task_id} "
                    f"past its acknowledged seq {state.acked_seq}",
                )
            if up_to_seq > state.last_seq:
                self._refuse(
                    "stream", offerer, task_id, "ack_ahead",
                    f"ack {up_to_seq} runs ahead of task {task_id}'s "
                    f"last delivered seq {state.last_seq}",
                )
            state.acked_seq = up_to_seq
            self._emit(
                "task_channel.stream_acknowledged",
                offerer,
                {"task": task_id, "acked_seq": up_to_seq},
            )
            return state.record()

    def stream_for(self, task_id: str) -> list[dict]:
        """The stream events the channel has carried for
        one task, in delivery order (copies)."""
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                raise TaskChannelError(
                    f"task {task_id!r} is not known to the channel",
                    reason="unknown_task",
                )
            return copy.deepcopy(state.stream)

    # -- results -------------------------------------------------

    def send_result(self, task_id: str, sender: str, result: dict) -> dict:
        """Carry the one terminal result for a claimed
        task and close it. Returns the record."""
        sender = self._check_node_id(sender)
        with self._lock:
            self._require_joined(sender, "result")
            state = self._state_for("result", sender, task_id)
            if state.status == "closed":
                self._refuse(
                    "result", sender, task_id, "task_closed",
                    f"task {task_id} is already closed",
                )
            if state.status != "claimed":
                self._refuse(
                    "result", sender, task_id, "not_claimed",
                    f"task {task_id} has not been claimed",
                )
            if sender != state.claimant:
                self._refuse(
                    "result", sender, task_id, "not_claimant",
                    f"task {task_id} is claimed by {state.claimant}, "
                    f"not by {sender}",
                )
            try:
                validate_result(copy.deepcopy(result))
            except TaskChannelError as exc:
                self._refuse("result", sender, task_id, "malformed_result", str(exc))
            if result["task_id"] != task_id:
                self._refuse(
                    "result", sender, task_id, "task_mismatch",
                    f"result names task {result['task_id']}, not {task_id}",
                )
            if result["provenance"]["node_id"] != sender:
                self._refuse(
                    "result", sender, task_id, "provenance_mismatch",
                    f"result provenance names "
                    f"{result['provenance']['node_id']}, not the "
                    f"claimant {sender}",
                )
            self._fabric.send(
                sender,
                state.offerer,
                {
                    "channel": "task_result",
                    "task_id": task_id,
                    "result": copy.deepcopy(result),
                },
            )
            state.result = copy.deepcopy(result)
            state.status = "closed"
            self._results += 1
            self._emit(
                "task_channel.task_closed", sender, {"task": task_id}
            )
            return state.record()

    def result_for(self, task_id: str) -> dict | None:
        """The terminal result the channel carried for
        one task, or None while it stands open."""
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                raise TaskChannelError(
                    f"task {task_id!r} is not known to the channel",
                    reason="unknown_task",
                )
            return copy.deepcopy(state.result)

    def record_for(self, task_id: str) -> dict:
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                raise TaskChannelError(
                    f"task {task_id!r} is not known to the channel",
                    reason="unknown_task",
                )
            return state.record()

    # -- report ----------------------------------------------------

    def report(self) -> dict:
        with self._lock:
            tasks = [
                self._tasks[task_id].record()
                for task_id in sorted(self._tasks)
            ]
            return validate_report(
                {
                    "version": 1,
                    "at": _fmt(self._now()),
                    "simulated": True,
                    "policy": copy.deepcopy(self._policy),
                    "node_ids": sorted(self._nodes),
                    "tasks": tasks,
                    "open_tasks": sum(
                        1 for t in tasks if t["status"] != "closed"
                    ),
                    "offers": self._offers,
                    "offers_refused": self._offers_refused,
                    "claims": self._claims,
                    "claims_refused": self._claims_refused,
                    "stream_sent": self._stream_sent,
                    "stream_refused": self._stream_refused,
                    "backpressure_refusals": self._backpressure_refusals,
                    "results": self._results,
                    "results_refused": self._results_refused,
                    "refused_reasons": dict(sorted(self._refused_reasons.items())),
                }
            )
