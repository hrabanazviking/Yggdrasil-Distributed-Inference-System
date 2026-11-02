"""Partition behaviour — Y-058.

The fabric (Y-050) already models a cut in the open:
``cut()`` splits a mesh, each side keeps delivering
within itself, and traffic across the seam waits in
sender outboxes until ``heal()``. The CRDT stores
(Y-056) already know how divergent registry and
membership replicas merge. What neither decides is
what a *task* means across a split. This slice is
that law: a split mesh keeps working in halves, and
on healing the CRDTs merge and duplicate task
executions are reconciled by task id — never silently
double-answered.

- **Halves keep working.** A :class:`PartitionMesh`
  joins nodes onto one fabric, each with its own
  Y-056 CRDT replica. ``offer`` / ``claim`` /
  ``send_result`` follow Y-057's discipline per side:
  an offer is seen by the nodes on the offerer's side
  (mesh-wide while whole — the fabric itself holds the
  cross-seam copies until healing, exactly as it holds
  any seam traffic); a claim is judged against the
  claims on the claimant's own side, because the other
  side's claims cannot be seen from here; a result is
  judged against the claimant of the sender's own
  side. A node on one side therefore cannot claim a
  task offered on the other side during the split —
  from its side, that task is ``unknown_task``.
- **Both sides may execute the same task.** A task
  offered before the cut was seen by everyone, so each
  side may claim and execute it independently. Each
  side's terminal result is held in the mesh's
  reconciliation ledger. The result on the offerer's
  own side reaches the offerer at once — provisionally,
  while the mesh stands split. A result from across
  the seam is NOT sent to the offerer while split:
  which execution is *the* answer is healing's
  decision, and an answer is delivered exactly once.
- **Healing reconciles by task id.** ``heal()``
  reunites the fabric (held seam traffic flushes),
  merges every node's CRDT replica with every other's
  until all hold identical mergeable state, and then
  settles each task that produced executions on both
  sides under the policy's one deterministic winner
  rule — ``offerer_side_first_then_earliest_finish_
  then_claimant``: the offerer's own side's result
  wins when one stands (so a provisional answer the
  offerer already holds is never retracted), else the
  earliest finish, ties broken by the canonically
  smaller claimant name. The winner is confirmed as
  the task's single answer and delivered to the
  offerer if it has not been; every other execution
  is recorded in the task's record as a named
  duplicate — counted in ``duplicates_reconciled``,
  evented as ``partition.duplicate_reconciled``, and
  retained in the ledger — never delivered, never
  silently dropped. A duplicate result that arrives
  *after* healing from a node that legitimately
  claimed the task on its side during the split is
  reconciled the same way on arrival; a result from
  anyone else is refused by name, as Y-057 refuses.
- **The law is schema.** ``partition_policy`` v1
  carries the constants: ``duplicate_answers_allowed``
  is always false, ``merge_on_heal`` always true, and
  the winner rule is a named constant — a policy
  naming any other law fails validation.

Refusals are :class:`PartitionError` carrying a
counted ``reason``, recorded as
``partition.<act>_refused`` Y-013 events from the
``fabric`` component — names, ids, and counts only,
never payload content. ``report()`` answers a
validated ``partition_report`` v1, including whether
every CRDT replica currently holds identical state.

This mesh is a formalisation standing on Y-050's
fabric, Y-056's stores, and Y-057's envelopes — not a
rewiring of any of them; the task channel keeps its
single-mesh bookkeeping, and this slice keeps the
partition ledger beside it. Message signing is
Y-059, quotas Y-060, store-and-forward Y-061,
real-LAN carriage Y-062. No node-agent seam (the
standing Part III precedent). Honesty: fully
SIMULATED — in-process fabric and stores, no socket
opened, no LAN crossed; what is proven is the
partition discipline and the reconciliation law on
the contract shapes.
"""

import copy
import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.crdt_stores import CrdtStore
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


_POLICY_VALIDATOR = _validator("partition_policy.schema.json")
_RECORD_VALIDATOR = _validator("partition_record.schema.json")
_REPORT_VALIDATOR = _validator("partition_report.schema.json")
_TASK_VALIDATOR = _validator("task_envelope.schema.json")
_RESULT_VALIDATOR = _validator("result_envelope.schema.json")


class PartitionError(Exception):
    """The partition mesh's own failure: a record that
    fails its schema, or a refused act. A refusal
    carries its counted reason in ``reason``."""

    def __init__(self, message: str, *, reason: str | None = None):
        super().__init__(message)
        self.reason = reason


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record, kind: str):
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise PartitionError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_policy(record: dict) -> dict:
    return _validate(_POLICY_VALIDATOR, record, "partition policy")


def validate_record(record: dict) -> dict:
    return _validate(_RECORD_VALIDATOR, record, "partition record")


def validate_report(record: dict) -> dict:
    return _validate(_REPORT_VALIDATOR, record, "partition report")


def validate_task(record: dict) -> dict:
    return _validate(_TASK_VALIDATOR, record, "task envelope")


def validate_result(record: dict) -> dict:
    return _validate(_RESULT_VALIDATOR, record, "result envelope")


def default_policy() -> dict:
    """The partition law as data: no task is ever
    answered twice, healing always merges the CRDT
    stores, and duplicate executions settle by the one
    named winner rule. Constants, labelled as law — a
    policy naming anything else fails its schema."""
    return validate_policy(
        {
            "version": 1,
            "duplicate_answers_allowed": False,
            "merge_on_heal": True,
            "winner_rule": (
                "offerer_side_first_then_earliest_finish_then_claimant"
            ),
        }
    )


class _TaskState:
    def __init__(self, task: dict, offerer: str, seen_by: set):
        self.task = copy.deepcopy(task)
        self.offerer = offerer
        self.seen_by = set(seen_by)
        self.claims: list[str] = []  # claimants, in claim order
        self.results: dict[str, dict] = {}  # side -> execution
        self.answer: dict | None = None  # confirmed execution
        self.provisional_side: str | None = None
        self.duplicates: list[dict] = []  # executions, reconciled
        self.delivered = False
        self.reconciled = False

    @property
    def task_id(self) -> str:
        return self.task["id"]


class PartitionMesh:
    """A mesh of nodes that keeps working in halves
    under a fabric cut and reconciles on healing.
    ``now`` is injectable so runs are replayable."""

    def __init__(self, policy: dict | None = None, *, now=None):
        self._policy = validate_policy(
            copy.deepcopy(policy) if policy is not None else default_policy()
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._fabric = Fabric(now=now)
        self._lock = threading.RLock()
        self._nodes: set[str] = set()
        self._stores: dict[str, CrdtStore] = {}
        self._split: set[str] | None = None
        self._tasks: dict[str, _TaskState] = {}
        self._events: list[dict] = []
        self._event_seq = 0
        self._cuts = 0
        self._heals = 0
        self._offers = 0
        self._offers_refused = 0
        self._claims = 0
        self._claims_refused = 0
        self._results = 0
        self._results_refused = 0
        self._duplicates_reconciled = 0
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

    @property
    def partitioned(self) -> bool:
        return self._split is not None

    def store(self, node_id: str) -> CrdtStore:
        """One node's own CRDT replica (its registry +
        membership store, Y-056)."""
        with self._lock:
            if node_id not in self._stores:
                raise PartitionError(
                    f"node {node_id} has not joined the mesh",
                    reason="not_joined",
                )
            return self._stores[node_id]

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

    # -- membership and sides ----------------------------------

    @staticmethod
    def _check_node_id(node_id: str) -> str:
        if not isinstance(node_id, str) or not _NODE_RE.match(node_id):
            raise PartitionError(f"node_id must be a node name, got {node_id!r}")
        return node_id

    def join(self, node_id: str) -> None:
        """Bring a node onto the mesh: attached to the
        fabric, subscribed to the offers topic, and
        holding its own CRDT replica. Joining while the
        mesh stands split is refused — a new node has no
        side until the mesh is whole again."""
        node_id = self._check_node_id(node_id)
        with self._lock:
            if node_id in self._nodes:
                raise PartitionError(f"node {node_id} is already on the mesh")
            if self._split is not None:
                raise PartitionError(
                    f"node {node_id} cannot join while the mesh is split",
                    reason="partitioned",
                )
            self._fabric.attach(node_id)
            self._fabric.subscribe(node_id, OFFERS_TOPIC)
            self._nodes.add(node_id)
            self._stores[node_id] = CrdtStore(
                local_node_id=node_id, now=self._now
            )
            self._emit("partition.node_joined", node_id, {"node": node_id})

    def _side_of(self, node_id: str) -> str:
        if self._split is None:
            return "whole"
        return "named" if node_id in self._split else "rest"

    def _require_joined(self, node_id: str, act: str) -> None:
        if node_id not in self._nodes:
            raise PartitionError(
                f"node {node_id} is not on the mesh; {act} refused",
                reason="not_joined",
            )

    # -- the cut and the heal ------------------------------------

    def cut(self, nodes) -> None:
        """Split the named nodes away from the rest of
        the mesh. Each side keeps working within itself;
        the fabric holds cross-seam traffic until
        :meth:`heal`."""
        with self._lock:
            if self._split is not None:
                raise PartitionError(
                    "the mesh is already split; heal before cutting again",
                    reason="already_partitioned",
                )
            split = {self._check_node_id(n) for n in nodes}
            if not split:
                raise PartitionError("a cut must name at least one node")
            unknown = split - self._nodes
            if unknown:
                raise PartitionError(
                    f"a cut cannot name nodes off the mesh: "
                    f"{sorted(unknown)}",
                    reason="not_joined",
                )
            if split == self._nodes:
                raise PartitionError(
                    "a cut naming every node splits nothing",
                    reason="cut_all",
                )
            self._fabric.cut(split)
            self._split = split
            self._cuts += 1
            self._emit(
                "partition.cut",
                min(split),
                {"named": len(split), "rest": len(self._nodes) - len(split)},
            )

    def heal(self) -> dict:
        """Reunite the mesh: the fabric flushes held
        seam traffic, every CRDT replica merges with
        every other (the policy's ``merge_on_heal``),
        and duplicate task executions are reconciled by
        task id under the winner rule. Returns the
        mesh's report after healing."""
        with self._lock:
            if self._split is None:
                raise PartitionError(
                    "the mesh is not split; there is nothing to heal",
                    reason="not_partitioned",
                )
            offerer_sides = {
                task_id: ("named" if task.offerer in self._split else "rest")
                for task_id, task in self._tasks.items()
            }
            self._fabric.heal()
            self._heals += 1
            for task in self._tasks.values():
                task.seen_by |= self._nodes
            self._merge_all_stores()
            for task_id in sorted(self._tasks):
                self._reconcile(
                    self._tasks[task_id], offerer_sides[task_id]
                )
            self._split = None
            self._emit(
                "partition.healed",
                min(self._nodes) if self._nodes else "none",
                {
                    "duplicates_reconciled": self._duplicates_reconciled,
                    "crdt_converged": self._crdt_converged(),
                },
            )
            return self.report()

    def _merge_all_stores(self) -> None:
        ids = sorted(self._stores)
        for a in ids:
            for b in ids:
                if a != b:
                    self._stores[a].sync(self._stores[b])

    def _crdt_converged(self) -> bool:
        states = [
            json.dumps(self._stores[n].state(), sort_keys=True)
            for n in sorted(self._stores)
        ]
        return len(set(states)) <= 1

    # -- registry acts (each node's own replica) -----------------

    def register(self, node_id: str, census: dict) -> dict | None:
        """Register one node's measured census on its
        own CRDT replica. While the mesh is split the
        write lands only on that replica — divergence
        is the honest state of a partition, and healing
        merges it. A census naming another node is
        refused: a node registers itself."""
        node_id = self._check_node_id(node_id)
        with self._lock:
            self._require_joined(node_id, "register")
            if census.get("node_id") != node_id:
                raise PartitionError(
                    f"a census naming {census.get('node_id')!r} cannot "
                    f"be registered by {node_id}",
                    reason="census_mismatch",
                )
            return self._stores[node_id].put_node(census)

    def offer_model(self, node_id: str, model: str) -> dict:
        """Offer one model on one node, on its own CRDT
        replica (an observed-remove add, Y-056)."""
        node_id = self._check_node_id(node_id)
        with self._lock:
            self._require_joined(node_id, "offer_model")
            return self._stores[node_id].add_model(node_id, model)

    def sync_stores(self) -> None:
        """Merge every replica with every other, on
        demand while the mesh is whole. (While split,
        cross-seam state cannot travel — that is what a
        partition means — so this is refused.)"""
        with self._lock:
            if self._split is not None:
                raise PartitionError(
                    "stores cannot sync across a seam while the mesh "
                    "is split; heal first",
                    reason="partitioned",
                )
            self._merge_all_stores()

    # -- refusals ----------------------------------------------

    def _refuse(self, act: str, node_id: str, task_id, reason: str, message: str):
        counter = {
            "offer": "_offers_refused",
            "claim": "_claims_refused",
            "result": "_results_refused",
        }.get(act)
        if counter is not None:
            setattr(self, counter, getattr(self, counter) + 1)
        self._refused_reasons[reason] = self._refused_reasons.get(reason, 0) + 1
        self._emit(
            f"partition.{act}_refused",
            node_id,
            {"task": task_id or "none", "reason": reason},
        )
        raise PartitionError(message, reason=reason)

    def _state_for(self, act: str, node_id: str, task_id) -> _TaskState:
        state = self._tasks.get(task_id) if isinstance(task_id, str) else None
        if state is None or node_id not in state.seen_by:
            self._refuse(
                act, node_id, task_id, "unknown_task",
                f"task {task_id!r} is not known on {node_id}'s side "
                "of the mesh",
            )
        return state  # type: ignore[return-value]

    def _claimant_for_side(self, state: _TaskState, side: str):
        """The first claimant whose own side is ``side``
        (sides are judged at the moment of asking, so a
        claim made while whole belongs to its claimant's
        side after a cut, and to the whole mesh after a
        heal)."""
        for claimant in state.claims:
            if self._side_of(claimant) == side:
                return claimant
        return None

    # -- offers ------------------------------------------------

    def offer(self, offerer: str, task: dict) -> dict:
        """Offer one task. It is seen by every node on
        the offerer's side (mesh-wide while whole) and
        published on the fabric's offers topic — the
        fabric holds the cross-seam copies until
        healing, as it holds any seam traffic. Returns
        the task's partition record."""
        offerer = self._check_node_id(offerer)
        with self._lock:
            self._require_joined(offerer, "offer")
            try:
                validate_task(copy.deepcopy(task))
            except PartitionError as exc:
                self._refuse("offer", offerer, None, "malformed_task", str(exc))
            task_id = task["id"]
            if task_id in self._tasks:
                self._refuse(
                    "offer", offerer, task_id, "duplicate_task",
                    f"task {task_id} is already known to the mesh",
                )
            if self._split is None:
                seen_by = set(self._nodes)
            else:
                side = self._side_of(offerer)
                seen_by = {
                    n for n in self._nodes if self._side_of(n) == side
                }
            state = _TaskState(task, offerer, seen_by)
            self._tasks[task_id] = state
            self._fabric.publish(
                offerer,
                OFFERS_TOPIC,
                {"channel": "task_offer", "task": copy.deepcopy(task)},
            )
            self._offers += 1
            self._emit("partition.task_offered", offerer, {"task": task_id})
            return self._record(state)

    # -- claims --------------------------------------------------

    def claim(self, task_id: str, claimant: str) -> dict:
        """Claim one offered task, judged on the
        claimant's own side: the first claim on a side
        takes the task for that side. While the mesh is
        whole that is the mesh's only claim; while split,
        the far side may claim the same task in
        ignorance — that is the duplicate execution
        healing exists to reconcile. Returns the task's
        partition record."""
        claimant = self._check_node_id(claimant)
        with self._lock:
            self._require_joined(claimant, "claim")
            state = self._state_for("claim", claimant, task_id)
            if claimant == state.offerer:
                self._refuse(
                    "claim", claimant, task_id, "self_claim",
                    f"node {claimant} offered task {task_id} and cannot "
                    "claim its own offer",
                )
            if state.answer is not None:
                self._refuse(
                    "claim", claimant, task_id, "task_closed",
                    f"task {task_id} already stands answered",
                )
            side = self._side_of(claimant)
            if (
                state.provisional_side is not None
                and state.provisional_side == side
            ):
                self._refuse(
                    "claim", claimant, task_id, "task_closed",
                    f"task {task_id} already stands answered on "
                    f"{claimant}'s side of the mesh",
                )
            holder = self._claimant_for_side(state, side)
            if holder is not None:
                self._refuse(
                    "claim", claimant, task_id, "already_claimed",
                    f"task {task_id} is already claimed by {holder} "
                    "on this side of the mesh",
                )
            state.claims.append(claimant)
            self._fabric.send(
                claimant,
                state.offerer,
                {"channel": "task_claim", "task_id": task_id,
                 "claimant": claimant},
            )
            self._claims += 1
            self._emit("partition.task_claimed", claimant, {"task": task_id})
            return self._record(state)

    # -- results and reconciliation ------------------------------

    def send_result(self, task_id: str, sender: str, result: dict) -> dict:
        """Carry one terminal result from a side's
        claimant. While whole, the first result is the
        task's answer, delivered to the offerer at once.
        While split, a result from the offerer's own
        side is delivered provisionally; a result from
        across the seam is held in the reconciliation
        ledger, undelivered, until healing decides.
        A result arriving after an answer stands, from
        a node that legitimately claimed the task on
        its side during the split, is reconciled as a
        duplicate on arrival — recorded and counted,
        never delivered. Returns the partition record."""
        sender = self._check_node_id(sender)
        with self._lock:
            self._require_joined(sender, "result")
            state = self._state_for("result", sender, task_id)
            try:
                validate_result(copy.deepcopy(result))
            except PartitionError as exc:
                self._refuse("result", sender, task_id, "malformed_result",
                              str(exc))
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
            side = self._side_of(sender)
            if sender not in state.claims:
                self._refuse(
                    "result", sender, task_id, "not_claimant",
                    f"task {task_id} was not claimed by {sender}",
                )
            execution = {
                "claimant": sender,
                "result": copy.deepcopy(result),
                "finished_at": self._now(),
                "side": side,
            }
            recorded = list(state.results.values()) + state.duplicates
            if any(e["claimant"] == sender for e in recorded):
                self._refuse(
                    "result", sender, task_id, "duplicate_result",
                    f"task {task_id} already has a terminal result "
                    f"recorded from {sender}",
                )
            if state.answer is not None:
                # The task already stands answered. An
                # execution by a recorded claimant is a
                # duplicate execution across the seam:
                # reconcile it by task id — counted,
                # named, retained, never delivered.
                state.duplicates.append(execution)
                state.reconciled = True
                self._results += 1
                self._duplicates_reconciled += 1
                self._emit(
                    "partition.duplicate_reconciled",
                    sender,
                    {"task": task_id, "duplicate": sender},
                )
                return self._record(state)
            if self._claimant_for_side(state, side) != sender:
                self._refuse(
                    "result", sender, task_id, "not_claimant",
                    f"task {task_id} is claimed on {sender}'s side by "
                    f"{self._claimant_for_side(state, side)}, not by "
                    f"{sender}",
                )
            if side in state.results:
                self._refuse(
                    "result", sender, task_id, "duplicate_result",
                    f"task {task_id} already has a terminal result "
                    f"from {sender}'s side of the mesh",
                )
            state.results[side] = execution
            self._results += 1
            if self._split is None:
                state.answer = execution
                self._deliver(state, execution)
            elif side == self._side_of(state.offerer):
                state.provisional_side = side
                self._deliver(state, execution)
            self._emit(
                "partition.result_recorded", sender, {"task": task_id}
            )
            return self._record(state)

    def _deliver(self, state: _TaskState, execution: dict) -> None:
        """Send one execution's result to the offerer
        over the fabric — the only road an answer ever
        travels, called at most once per task for its
        confirmed answer (a provisional delivery is the
        offerer's own side's execution, which the
        winner rule guarantees is never retracted)."""
        self._fabric.send(
            execution["claimant"],
            state.offerer,
            {
                "channel": "task_result",
                "task_id": state.task_id,
                "result": copy.deepcopy(execution["result"]),
            },
        )
        state.delivered = True

    def _reconcile(self, state: _TaskState, offerer_side: str) -> None:
        """Settle one task at healing. ``offerer_side``
        is the side the offerer stood on under the cut
        being healed (``named`` or ``rest``). Every
        stored side execution is a candidate; the
        winner rule names the answer; the rest become
        named duplicates. A task with a single
        execution simply confirms it (delivering it if
        the offerer never held it)."""
        if state.answer is not None or not state.results:
            return
        candidates = list(state.results.values())
        winner = None
        for execution in candidates:
            if execution["side"] == offerer_side:
                winner = execution
                break
        if winner is None:
            winner = min(
                candidates,
                key=lambda e: (e["finished_at"], e["claimant"]),
            )
        state.answer = winner
        losers = [e for e in candidates if e is not winner]
        for loser in losers:
            for side_key, held in list(state.results.items()):
                if held is loser:
                    del state.results[side_key]
            state.duplicates.append(loser)
            self._duplicates_reconciled += 1
            self._emit(
                "partition.duplicate_reconciled",
                loser["claimant"],
                {"task": state.task_id, "duplicate": loser["claimant"]},
            )
        if losers:
            state.reconciled = True
        state.provisional_side = None
        if not state.delivered:
            self._deliver(state, winner)

    def answer_for(self, task_id: str) -> dict | None:
        """The one answer the offerer holds for a task:
        the confirmed answer once one stands, the
        provisional own-side answer while split, or
        None. There is never a second answer."""
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                raise PartitionError(
                    f"task {task_id!r} is not known to the mesh",
                    reason="unknown_task",
                )
            if state.answer is not None:
                return copy.deepcopy(state.answer["result"])
            if state.provisional_side is not None:
                execution = state.results.get(state.provisional_side)
                if execution is not None:
                    return copy.deepcopy(execution["result"])
            return None

    def duplicates_for(self, task_id: str) -> list[dict]:
        """The duplicate executions reconciled for one
        task — claimant, result, and finish time, in
        the order they were reconciled. Retained and
        named; none of these was ever delivered as an
        answer."""
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                raise PartitionError(
                    f"task {task_id!r} is not known to the mesh",
                    reason="unknown_task",
                )
            return [
                {
                    "claimant": e["claimant"],
                    "result": copy.deepcopy(e["result"]),
                    "finished_at": _fmt(e["finished_at"]),
                }
                for e in state.duplicates
            ]

    # -- records and report ---------------------------------------

    def _record(self, state: _TaskState) -> dict:
        standing = state.answer
        provisional = False
        if standing is None and state.provisional_side is not None:
            standing = state.results.get(state.provisional_side)
            provisional = standing is not None
        if state.reconciled and state.duplicates:
            status = "reconciled"
        elif standing is not None:
            status = "answered"
        elif state.claims:
            status = "claimed"
        else:
            status = "open"
        executions = len(state.results) + len(state.duplicates)
        return validate_record(
            {
                "task_id": state.task_id,
                "offerer": state.offerer,
                "claimants": sorted(set(state.claims)),
                "executions": executions,
                "answer_claimant": (
                    standing["claimant"] if standing is not None else None
                ),
                "answer_status": (
                    standing["result"]["status"]
                    if standing is not None
                    else None
                ),
                "duplicate_claimants": sorted(
                    {e["claimant"] for e in state.duplicates}
                ),
                "status": status,
                "provisional": provisional,
            }
        )

    def record_for(self, task_id: str) -> dict:
        with self._lock:
            state = self._tasks.get(task_id)
            if state is None:
                raise PartitionError(
                    f"task {task_id!r} is not known to the mesh",
                    reason="unknown_task",
                )
            return self._record(state)

    def report(self) -> dict:
        with self._lock:
            tasks = [
                self._record(self._tasks[task_id])
                for task_id in sorted(self._tasks)
            ]
            if self._split is None:
                side_named: list[str] = []
                side_rest: list[str] = []
            else:
                side_named = sorted(self._split)
                side_rest = sorted(self._nodes - self._split)
            return validate_report(
                {
                    "version": 1,
                    "at": _fmt(self._now()),
                    "simulated": True,
                    "policy": copy.deepcopy(self._policy),
                    "node_ids": sorted(self._nodes),
                    "partitioned": self._split is not None,
                    "side_named": side_named,
                    "side_rest": side_rest,
                    "cuts": self._cuts,
                    "heals": self._heals,
                    "tasks": tasks,
                    "offers": self._offers,
                    "offers_refused": self._offers_refused,
                    "claims": self._claims,
                    "claims_refused": self._claims_refused,
                    "results": self._results,
                    "results_refused": self._results_refused,
                    "duplicates_reconciled": self._duplicates_reconciled,
                    "crdt_converged": self._crdt_converged(),
                    "refused_reasons": dict(
                        sorted(self._refused_reasons.items())
                    ),
                }
            )
