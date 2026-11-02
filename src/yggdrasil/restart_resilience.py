"""Restart resilience — Y-066.

Y-056 proved CRDT replicas converge when they merge.
This slice answers the uglier question: a node
*stops* — its process dies, its in-memory replica
with it — and later comes back. What does it hold,
what has the mesh done without it, and how does it
rejoin **without a full reload**? The answer is a
delta discipline, kept by :class:`RejoinCoordinator`:

- **A crash is modelled honestly.** ``crash`` throws
  the node's live replica away. The only survivor is
  the node's **checkpoint** — a validated
  ``crdt_state`` v1 the node saved while it was up
  (``checkpoint``), standing in for durable local
  storage. A write that was neither checkpointed nor
  gossiped to a peer before the crash is *gone*, and
  the suite proves it is gone rather than pretending
  durability the design does not have.
- **The mesh does not wait.** While a node is down
  the survivors keep writing, removing, and syncing
  among themselves; any act addressed at the down
  node is refused by name (``node_down``), never
  silently queued for a node that may never return.
- **Re-sync is a digest and two deltas, never a
  state dump.** ``restart`` raises a fresh replica
  and restores the checkpoint locally — local
  durable storage, not network traffic. ``rejoin``
  then exchanges **digests** (a validated
  ``restart_digest`` v1: per-write stamps plus a
  SHA-256 hash of each census, tombstone stamps,
  model add/remove tags, the epoch and the Lamport
  clock — stamps and hashes only, no census
  content) and computes, each way, a
  ``restart_delta`` v1: a partial ``crdt_state``
  holding exactly the elements the other side's
  digest shows it lacks or holds stale. Both deltas
  are applied by the Y-056 merge, and the rejoin
  record states plainly whether the two replicas'
  mergeable state **converged** (identical writes,
  tombstones, adds, and removes).
- **The law is schema constants.** The
  ``restart_policy`` v1 fixes ``sync_mode: "delta"``
  and ``full_reload_allowed: false`` — a policy
  naming a full reload fails validation, and the
  coordinator exposes no full-state transfer at
  all: its report carries a ``full_reloads`` counter
  that stands at zero because there is no road that
  could raise it. A rejoin whose delta would exceed
  the policy's ``max_delta_elements`` is refused
  ``delta_too_large`` — counted, applied nowhere —
  rather than quietly becoming the bulk transfer
  the policy forbids.
- **Every rejoin is judged, none crashes the
  coordinator.** ``rejoin`` and ``apply_delta``
  answer a validated ``restart_record`` v1:
  ``rejoined`` / ``applied``, or refused by name —
  ``unknown_peer``, ``self_rejoin``, ``peer_down``,
  ``node_down``, ``not_restarted`` (a rejoin
  attempted on a node that never went down),
  ``delta_too_large``, ``malformed_digest``,
  ``malformed_delta``, ``delta_not_for_me`` —
  counted apart, with element counts moved in each
  direction and the checkpoint's part named. A
  refused delta application is recorded too, but
  is not counted among refused *rejoins*.

One honesty bound deserves its own words: a node
that kept **no** checkpoint rejoins from nothing,
so its incoming delta is every element it lacks —
which is all of them. That is still a delta —
judged, counted element by element, applied by
merge — not a reload; the record and the report
say exactly how many elements moved, so the cost
of skipping checkpoints is a published number,
never a hidden one.

Honesty: everything here is SIMULATED —
``simulated: true`` is a schema constant on the
digest, the delta, the record, and the report. A
"crash" is the in-process loss of one replica
object and a "checkpoint" an in-process state
record standing in for durable storage; no
process was killed, no disk was written, no
socket opened, no LAN crossed (real carriage was
Y-062's own proof; the registrar's death drill is
Y-067's in-process exercise, and killing real
processes on real citizens is Part VIII's
hardware road).
"""

import copy
import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.crdt_stores import (
    CrdtStore,
    stamp_key,
)
from yggdrasil.crdt_stores import (
    validate_state as validate_crdt_state,
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


_POLICY_VALIDATOR = _validator("restart_policy.schema.json")
_DIGEST_VALIDATOR = _validator("restart_digest.schema.json")
_DELTA_VALIDATOR = _validator("restart_delta.schema.json")
_RECORD_VALIDATOR = _validator("restart_record.schema.json")
_REPORT_VALIDATOR = _validator("restart_report.schema.json")


class RestartError(Exception):
    """The coordinator's own failure: a policy or
    record that fails its contract, or a use of the
    API that would manufacture a standing — crashing
    a node that is already down, restarting one that
    never crashed, or addressing a node the mesh has
    never held. (Judged rejoin traffic is refused by
    named record instead; see ``rejoin``.)"""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record, kind: str):
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise RestartError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_policy(record: dict) -> dict:
    return _validate(_POLICY_VALIDATOR, record, "restart policy")


def validate_digest(record: dict) -> dict:
    return _validate(_DIGEST_VALIDATOR, record, "restart digest")


def validate_delta(record: dict) -> dict:
    checked = _validate(_DELTA_VALIDATOR, record, "restart delta")
    if checked["elements"] != elements_of(checked["state"]):
        raise RestartError(
            "elements_mismatch: a delta's stated elements must equal "
            "the element count of the state it carries"
        )
    return checked


def validate_record(record: dict) -> dict:
    return _validate(_RECORD_VALIDATOR, record, "restart record")


def validate_report(record: dict) -> dict:
    return _validate(_REPORT_VALIDATOR, record, "restart report")


def default_policy() -> dict:
    """The rejoin discipline's bounds as data: deltas
    only, full reloads never, at most 1024 elements in
    one delta — a policy, not a measurement."""
    return validate_policy(
        {
            "version": 1,
            "sync_mode": "delta",
            "full_reload_allowed": False,
            "max_delta_elements": 1024,
        }
    )


def elements_of(state: dict) -> int:
    """The element count of one mergeable state:
    writes + tombstones + model adds + model
    removes. The unit every digest, delta, and
    record in this slice counts in."""
    return (
        len(state["writes"])
        + len(state["tombstones"])
        + len(state["model_adds"])
        + len(state["model_removes"])
    )


def _census_hash(census: dict) -> str:
    return hashlib.sha256(
        json.dumps(census, sort_keys=True).encode()
    ).hexdigest()


def _stamp_tag(stamp: dict) -> tuple:
    return (stamp["node_id"], stamp["lamport"], stamp["wall_time"])


def build_digest(state: dict, *, node_id: str, at: datetime) -> dict:
    """One replica's digest from its exported state:
    stamps and census hashes only — the census
    itself never rides a digest."""
    checked = validate_crdt_state(copy.deepcopy(state))
    digest = {
        "version": 1,
        "node_id": node_id,
        "at": _fmt(at),
        "epoch": checked["epoch"],
        "lamport_clock": max(
            [_stamp_tag(w["stamp"])[1] for w in checked["writes"]]
            + [_stamp_tag(t["stamp"])[1] for t in checked["tombstones"]]
            + [_stamp_tag(a["stamp"])[1] for a in checked["model_adds"]]
            + [_stamp_tag(r["stamp"])[1] for r in checked["model_removes"]]
            + [0]
        ),
        "elements": elements_of(checked),
        "writes": [
            {
                "node_id": w["node_id"],
                "stamp": copy.deepcopy(w["stamp"]),
                "census_hash": _census_hash(w["census"]),
            }
            for w in checked["writes"]
        ],
        "tombstones": [
            {"node_id": t["node_id"], "stamp": copy.deepcopy(t["stamp"])}
            for t in checked["tombstones"]
        ],
        "model_adds": [copy.deepcopy(a) for a in checked["model_adds"]],
        "model_removes": [copy.deepcopy(r) for r in checked["model_removes"]],
        "simulated": True,
    }
    return validate_digest(digest)


def compute_delta(source_state: dict, target_digest: dict) -> dict:
    """The partial ``crdt_state`` a source must send a
    target: exactly the elements the target's digest
    shows it lacks or holds stale. A write travels
    when the target holds no write for that node, an
    *older* stamp for it (under the ADR-003 total
    order), or the same stamp over a different
    census (the digest's hash sees what its stamps
    cannot); a write the target already holds
    *fresher* never travels backwards — the merge
    would refuse it stale anyway, so sending it
    would be waste dressed as sync. Tombstones and
    model tags travel when the target's digest does
    not name them. An element the target already
    holds identically never travels — that is the
    whole economy of the rejoin."""
    source = validate_crdt_state(copy.deepcopy(source_state))
    target = validate_digest(copy.deepcopy(target_digest))
    target_writes = {
        w["node_id"]: (w["stamp"], w["census_hash"]) for w in target["writes"]
    }
    target_tombstones = {
        (t["node_id"], _stamp_tag(t["stamp"])) for t in target["tombstones"]
    }
    target_adds = {
        (a["node_id"], a["model"], _stamp_tag(a["stamp"]))
        for a in target["model_adds"]
    }
    target_removes = {
        (r["node_id"], r["model"], _stamp_tag(r["stamp"]))
        for r in target["model_removes"]
    }
    def write_travels(write) -> bool:
        held = target_writes.get(write["node_id"])
        if held is None:
            return True
        held_stamp, held_hash = held
        if stamp_key(write["stamp"]) > stamp_key(held_stamp):
            return True
        return (
            stamp_key(write["stamp"]) == stamp_key(held_stamp)
            and _census_hash(write["census"]) != held_hash
        )

    delta = {
        "version": 1,
        "epoch": source["epoch"],
        "simulated": True,
        "writes": [
            copy.deepcopy(w) for w in source["writes"] if write_travels(w)
        ],
        "tombstones": [
            copy.deepcopy(t)
            for t in source["tombstones"]
            if (t["node_id"], _stamp_tag(t["stamp"])) not in target_tombstones
        ],
        "model_adds": [
            copy.deepcopy(a)
            for a in source["model_adds"]
            if (a["node_id"], a["model"], _stamp_tag(a["stamp"]))
            not in target_adds
        ],
        "model_removes": [
            copy.deepcopy(r)
            for r in source["model_removes"]
            if (r["node_id"], r["model"], _stamp_tag(r["stamp"]))
            not in target_removes
        ],
    }
    return validate_crdt_state(delta)


def _fingerprint(state: dict) -> str:
    """A canonical fingerprint of one state's
    mergeable content (epoch excluded — advancing an
    epoch is a local act, not a divergence). Two
    replicas have converged exactly when their
    fingerprints agree."""
    mergeable = {
        key: state[key]
        for key in ("writes", "tombstones", "model_adds", "model_removes")
    }
    return hashlib.sha256(
        json.dumps(mergeable, sort_keys=True).encode()
    ).hexdigest()


class _Node:
    """One mesh member's standing at the coordinator:
    its live replica (``None`` while down), its last
    checkpoint, and its counted history."""

    def __init__(self, store: CrdtStore):
        self.store: CrdtStore | None = store
        self.status = "up"
        self.checkpoint: dict | None = None
        self.checkpoint_used = False
        self.crashes = 0
        self.rejoins = 0


class RejoinCoordinator:
    """The restart-resilience door: a mesh of Y-056
    CRDT replicas, their checkpoints, their crashes,
    and their delta re-syncs under one policy.
    ``now`` is injectable so records are testable."""

    def __init__(self, policy: dict | None = None, *, now=None, crdt_policy=None):
        self._policy = validate_policy(
            copy.deepcopy(policy) if policy is not None else default_policy()
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._crdt_policy = copy.deepcopy(crdt_policy) if crdt_policy else None
        self._lock = threading.RLock()
        self._nodes: dict[str, _Node] = {}
        self._records: list[dict] = []
        self._refusals_by_reason: dict[str, int] = {}
        self._rejoin_refusals = 0
        self._crashes = 0
        self._restarts = 0
        self._rejoins = 0
        self._converged_rejoins = 0
        self._digests_exchanged = 0
        self._elements_transferred = 0
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
                component="registry",
                seq=self._event_seq,
                at=self._now(),
                payload=payload,
            )
        )

    def _member(self, node_id: str) -> _Node:
        member = self._nodes.get(node_id)
        if member is None:
            raise RestartError(f"unknown_node: node {node_id} is not in this mesh")
        return member

    def _live_store(self, node_id: str) -> CrdtStore:
        member = self._member(node_id)
        if member.store is None:
            raise RestartError(f"node_down: node {node_id} is down")
        return member.store

    # -- membership --------------------------------------

    def add_node(self, node_id: str) -> CrdtStore:
        """Bring one node into the mesh with a fresh
        replica. Adding the same node twice is refused
        — a returning node goes by ``crash`` /
        ``restart`` / ``rejoin``, never by pretending
        it is new."""
        with self._lock:
            if node_id in self._nodes:
                raise RestartError(
                    f"already_in_mesh: node {node_id} is already in this mesh"
                )
            store = CrdtStore(
                local_node_id=node_id, policy=self._crdt_policy, now=self._now
            )
            self._nodes[node_id] = _Node(store)
            self._emit("restart.node_added", node_id, {"node_id": node_id})
            return store

    def store(self, node_id: str) -> CrdtStore:
        """The node's live replica — the handle tests
        and callers write censuses and offers through.
        A down node has no live replica and asking for
        one raises ``node_down``."""
        with self._lock:
            return self._live_store(node_id)

    def status(self, node_id: str) -> str:
        """One node's standing: ``up``, ``down``, or
        ``restarted`` (live again, not yet re-synced)."""
        with self._lock:
            return self._member(node_id).status

    def sync_pair(self, a: str, b: str) -> None:
        """Ordinary two-way CRDT sync between two up
        nodes — the mesh's everyday gossip road,
        against which a rejoin's delta is measured.
        Either end being down raises ``node_down``:
        the mesh never queues state for the dead."""
        with self._lock:
            self._live_store(a).sync(self._live_store(b))

    # -- checkpoints, crashes, restarts -------------------

    def checkpoint(self, node_id: str) -> int:
        """Save the node's durable checkpoint: its
        whole mergeable state, validated, kept against
        a crash. Returns the checkpoint's element
        count. Only an up (or restarted) node can
        checkpoint; a down node saves nothing."""
        with self._lock:
            member = self._member(node_id)
            state = self._live_store(node_id).state()
            member.checkpoint = state
            elements = elements_of(state)
            self._emit(
                "restart.checkpoint_saved",
                node_id,
                {"node_id": node_id, "elements": elements},
            )
            return elements

    def crash(self, node_id: str) -> None:
        """Kill the node in-process: its live replica
        is thrown away and only its checkpoint (if it
        ever saved one) survives. Crashing a node that
        is already down raises — a death is an event,
        not a standing state to re-enter silently."""
        with self._lock:
            member = self._member(node_id)
            if member.store is None:
                raise RestartError(f"already_down: node {node_id} is already down")
            member.store = None
            member.status = "down"
            member.checkpoint_used = False
            member.crashes += 1
            self._crashes += 1
            kept = (
                elements_of(member.checkpoint)
                if member.checkpoint is not None
                else 0
            )
            self._emit(
                "restart.node_crashed",
                node_id,
                {"node_id": node_id, "checkpoint_elements": kept},
            )

    def restart(self, node_id: str) -> CrdtStore:
        """Raise the node again: a fresh replica, its
        checkpoint (if any) restored locally — local
        durable storage, no network traffic, counted
        as neither a delta nor a reload. The node
        stands ``restarted``: live, holding only what
        its checkpoint held, until ``rejoin`` re-syncs
        it. Restarting a node that is not down raises."""
        with self._lock:
            member = self._member(node_id)
            if member.store is not None:
                raise RestartError(
                    f"not_down: node {node_id} is not down and cannot restart"
                )
            store = CrdtStore(
                local_node_id=node_id, policy=self._crdt_policy, now=self._now
            )
            member.checkpoint_used = member.checkpoint is not None
            if member.checkpoint is not None:
                store.merge_state(copy.deepcopy(member.checkpoint))
            member.store = store
            member.status = "restarted"
            self._restarts += 1
            kept = (
                elements_of(member.checkpoint)
                if member.checkpoint is not None
                else 0
            )
            self._emit(
                "restart.node_restarted",
                node_id,
                {"node_id": node_id, "checkpoint_elements": kept},
            )
            return store

    # -- digests and deltas ---------------------------------

    def digest(self, node_id: str) -> dict:
        """The node's current re-sync digest — stamps
        and hashes only. A down node has no digest."""
        with self._lock:
            return build_digest(
                self._live_store(node_id).state(), node_id=node_id, at=self._now()
            )

    def _record(
        self,
        node_id,
        peer_id,
        verdict,
        reason,
        *,
        received=0,
        sent=0,
        converged=False,
        checkpoint_used=False,
    ) -> dict:
        record = {
            "version": 1,
            "seq": len(self._records) + 1,
            "at": _fmt(self._now()),
            "node_id": node_id,
            "peer_id": peer_id,
            "verdict": verdict,
            "reason": reason,
            "elements_received": received,
            "elements_sent": sent,
            "converged": converged,
            "checkpoint_used": checkpoint_used,
            "simulated": True,
        }
        checked = validate_record(record)
        self._records.append(checked)
        if verdict == "refused":
            self._refusals_by_reason[reason] = (
                self._refusals_by_reason.get(reason, 0) + 1
            )
            self._emit(
                "restart.rejoin_refused",
                node_id or "unknown",
                {
                    "node_id": node_id or "unknown",
                    "peer_id": peer_id or "unknown",
                    "reason": reason,
                },
            )
        return checked

    def _refuse(
        self, node_id, peer_id, reason, *, checkpoint_used=False, rejoin=True
    ):
        if rejoin:
            self._rejoin_refusals += 1
        return self._record(
            node_id,
            peer_id,
            "refused",
            reason,
            checkpoint_used=checkpoint_used,
        )

    # -- the rejoin ------------------------------------------

    def rejoin(self, node_id: str, *, via: str, peer_digest=None) -> dict:
        """Re-sync one restarted node through one peer:
        digests are exchanged, each way's delta is
        computed against the other side's digest, both
        deltas are applied by merge, and the record
        states whether the pair converged. Refusals
        are records, never exceptions, and a refused
        rejoin applies nothing either way — the pair
        is left exactly as it stood. The node itself
        must be a mesh member (an unknown node id is
        the caller's own error and raises); a caller
        may hand in the peer's digest it already
        holds, and a digest that fails its contract
        is refused ``malformed_digest``."""
        with self._lock:
            member = self._member(node_id)
            if via == node_id:
                return self._refuse(node_id, via, "self_rejoin")
            peer = self._nodes.get(via)
            if peer is None:
                return self._refuse(node_id, via, "unknown_peer")
            if member.store is None:
                return self._refuse(
                    node_id, via, "node_down", checkpoint_used=False
                )
            if member.status == "up":
                return self._refuse(
                    node_id,
                    via,
                    "not_restarted",
                    checkpoint_used=member.checkpoint_used,
                )
            if peer.store is None:
                return self._refuse(
                    node_id,
                    via,
                    "peer_down",
                    checkpoint_used=member.checkpoint_used,
                )
            if peer_digest is not None:
                try:
                    peer_digest = validate_digest(copy.deepcopy(peer_digest))
                except Exception:  # noqa: BLE001 — a digest that fails its contract is this door's data: refused malformed and counted, never the coordinator's crash
                    return self._refuse(
                        node_id,
                        via,
                        "malformed_digest",
                        checkpoint_used=member.checkpoint_used,
                    )
                if peer_digest["node_id"] != via:
                    return self._refuse(
                        node_id,
                        via,
                        "malformed_digest",
                        checkpoint_used=member.checkpoint_used,
                    )
            else:
                peer_digest = self.digest(via)
            own_digest = self.digest(node_id)
            self._digests_exchanged += 2
            incoming = compute_delta(peer.store.state(), own_digest)
            outgoing = compute_delta(member.store.state(), peer_digest)
            received, sent = elements_of(incoming), elements_of(outgoing)
            if (
                received > self._policy["max_delta_elements"]
                or sent > self._policy["max_delta_elements"]
            ):
                return self._refuse(
                    node_id,
                    via,
                    "delta_too_large",
                    checkpoint_used=member.checkpoint_used,
                )
            incoming_record = validate_delta(
                {
                    "version": 1,
                    "from_node": via,
                    "to_node": node_id,
                    "at": _fmt(self._now()),
                    "elements": received,
                    "state": incoming,
                    "simulated": True,
                }
            )
            outgoing_record = validate_delta(
                {
                    "version": 1,
                    "from_node": node_id,
                    "to_node": via,
                    "at": _fmt(self._now()),
                    "elements": sent,
                    "state": outgoing,
                    "simulated": True,
                }
            )
            member.store.merge_state(incoming_record["state"])
            peer.store.merge_state(outgoing_record["state"])
            self._elements_transferred += received + sent
            converged = _fingerprint(member.store.state()) == _fingerprint(
                peer.store.state()
            )
            member.status = "up"
            member.rejoins += 1
            self._rejoins += 1
            if converged:
                self._converged_rejoins += 1
            self._emit(
                "restart.node_rejoined",
                node_id,
                {
                    "node_id": node_id,
                    "peer_id": via,
                    "elements_received": received,
                    "elements_sent": sent,
                    "converged": 1 if converged else 0,
                },
            )
            return self._record(
                node_id,
                via,
                "rejoined",
                None,
                received=received,
                sent=sent,
                converged=converged,
                checkpoint_used=member.checkpoint_used,
            )

    def apply_delta(self, node_id: str, delta) -> dict:
        """Judge and apply one raw ``restart_delta``
        v1 handed to a node — the seam real carriage
        (Y-062's road) would feed. A delta that fails
        its contract is refused ``malformed_delta``,
        one addressed to another node
        ``delta_not_for_me``, one landing on a down
        node ``node_down``, and one past the policy's
        bound ``delta_too_large`` — each counted,
        applied nowhere. An applied delta merges by
        the Y-056 rules and the record names the
        elements it carried."""
        with self._lock:
            member = self._nodes.get(node_id)
            if member is None:
                raise RestartError(
                    f"unknown_node: node {node_id} is not in this mesh"
                )
            try:
                checked = validate_delta(copy.deepcopy(delta))
            except Exception:  # noqa: BLE001 — a delta that fails its contract is this door's data: refused malformed and counted, never the coordinator's crash
                return self._refuse(node_id, None, "malformed_delta", rejoin=False)
            if checked["to_node"] != node_id:
                return self._refuse(
                    node_id, checked["from_node"], "delta_not_for_me", rejoin=False
                )
            if member.store is None:
                return self._refuse(node_id, checked["from_node"], "node_down", rejoin=False)
            if checked["elements"] > self._policy["max_delta_elements"]:
                return self._refuse(
                    node_id, checked["from_node"], "delta_too_large", rejoin=False
                )
            member.store.merge_state(checked["state"])
            self._elements_transferred += checked["elements"]
            peer = self._nodes.get(checked["from_node"])
            converged = (
                peer is not None
                and peer.store is not None
                and _fingerprint(member.store.state())
                == _fingerprint(peer.store.state())
            )
            self._emit(
                "restart.delta_applied",
                node_id,
                {
                    "node_id": node_id,
                    "peer_id": checked["from_node"],
                    "elements_received": checked["elements"],
                },
            )
            return self._record(
                node_id,
                checked["from_node"],
                "applied",
                None,
                received=checked["elements"],
                converged=converged,
                checkpoint_used=member.checkpoint_used,
            )

    # -- the door ---------------------------------------------

    def report(self) -> dict:
        with self._lock:
            return validate_report(
                {
                    "version": 1,
                    "at": _fmt(self._now()),
                    "simulated": True,
                    "policy": copy.deepcopy(self._policy),
                    "nodes": [
                        {
                            "node_id": node_id,
                            "status": self._nodes[node_id].status,
                            "crashes": self._nodes[node_id].crashes,
                            "rejoins": self._nodes[node_id].rejoins,
                            "checkpoint_elements": (
                                elements_of(self._nodes[node_id].checkpoint)
                                if self._nodes[node_id].checkpoint is not None
                                else 0
                            ),
                        }
                        for node_id in sorted(self._nodes)
                    ],
                    "crashes": self._crashes,
                    "restarts": self._restarts,
                    "rejoins": self._rejoins,
                    "converged_rejoins": self._converged_rejoins,
                    "rejoins_refused": self._rejoin_refusals,
                    "refusals_by_reason": dict(
                        sorted(self._refusals_by_reason.items())
                    ),
                    "digests_exchanged": self._digests_exchanged,
                    "elements_transferred": self._elements_transferred,
                    "full_reloads": 0,
                    "records": copy.deepcopy(self._records),
                }
            )
