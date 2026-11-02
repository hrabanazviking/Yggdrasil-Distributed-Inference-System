"""CRDT stores land — Y-056 (ADR-003 executed).

ADR-003 decided which stores are CRDTs and how they
merge; Y-053 built the registry as a replicated service
whose pairwise sync already obeyed those rules in
behaviour. This slice is the formalisation ADR-003
promised: the registry + membership store stated AS a
CRDT, with its merge proven — not merely exercised —
idempotent and order-insensitive, property-tested over
permuted and shuffled merge orders.

Two CRDTs, exactly as the ADR's table names them:

- **Registry + membership: a last-writer-wins element
  set, one element per node record.** An element is a
  node's whole record (its measured Y-022 census). A
  write carries a :func:`make_stamp` stamp —
  ``(node_id, lamport, wall_time)``, the triple ADR-003
  fixes — and stamps carry a total order: lamport
  first, then wall time, then node_id, so a tie breaks
  by node_id and every replica settles the same contest
  the same way without talking. The newest write wins
  whole; an older write is stale and changes nothing;
  the same stamp carrying different facts is a
  *conflict* — counted, evented, and settled
  deterministically (the canonically smaller census is
  kept on every replica). Deletes are tombstones under
  the same order: a tombstone newer than the write
  removes the node, an older write arriving afterwards
  cannot resurrect it, and a write newer than the
  tombstone is a genuine rejoin. Tombstones are
  retained for the policy's registry epochs (ADR-003:
  one epoch); :meth:`CrdtStore.advance_epoch` prunes a
  tombstone only together with the write it superseded,
  so pruning never resurrects anyone locally — a node
  gone past its epoch is pruned whole, and proper
  retirement stays Y-180's act.

- **Models: an observed-remove set per node.** A model
  offer is an add tagged with its own stamp; a remove
  tombstones exactly the add-tags the remover had
  *observed*. An add the remover never saw — a
  concurrent offer travelling another road — survives
  the remove and is present when it arrives (add-wins
  under concurrency, the observed-remove promise),
  while an observed offer removed is gone on every
  replica the remove reaches.

Each replica keeps a Lamport clock: a local act ticks
it, observing any stamp raises it to at least that
stamp's lamport, so a replica's next local stamp
outranks everything it has seen. Merge is state-based:
:meth:`CrdtStore.state` exports the whole mergeable
state as a validated ``crdt_state`` v1 (newest write
per node, tombstones with their epochs, model adds
and the removes that name them), and
:meth:`CrdtStore.merge_state` takes the union under
the stamp order. Union under a total order is why the
property tests hold: merging is commutative,
associative, and idempotent *by construction*, and the
suite proves it on divergent replicas merged in every
permutation, plus seeded shuffles of piecemeal state
fragments.

This store is the CRDT formalisation standing beside
Y-053's service, not a rewiring of it: the registry
service keeps serving registrations and gossip keeps
riding its sync (Y-055); the stores converge here
under their own stamps, and real carriage of CRDT
state over the fabric + TLS is Y-062, as with every
Part III store. No node-agent seam (the Y-053
precedent). Honesty: in-process state only — records
carry ``simulated: true`` as a schema constant, no
socket is opened, no LAN is crossed; what is proven is
the merge mathematics on the contract shapes.
"""

import copy
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

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


_STAMP_VALIDATOR = _validator("crdt_stamp.schema.json")
_POLICY_VALIDATOR = _validator("crdt_policy.schema.json")
_RECORD_VALIDATOR = _validator("crdt_node_record.schema.json")
_STATE_VALIDATOR = _validator("crdt_state.schema.json")
_REPORT_VALIDATOR = _validator("crdt_report.schema.json")
_CENSUS_VALIDATOR = _validator("capability_census.schema.json")


class CrdtError(Exception):
    """The CRDT store's own failure: a stamp, policy,
    record, or state that fails its schema, or a use of
    the API that would manufacture truth — writing a
    census under another node's name, or merging a
    replica with itself."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise CrdtError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_stamp(record: dict) -> dict:
    return _validate(_STAMP_VALIDATOR, record, "crdt stamp")


def validate_policy(record: dict) -> dict:
    return _validate(_POLICY_VALIDATOR, record, "crdt policy")


def validate_record(record: dict) -> dict:
    return _validate(_RECORD_VALIDATOR, record, "crdt node record")


def validate_state(record: dict) -> dict:
    return _validate(_STATE_VALIDATOR, record, "crdt state")


def validate_report(record: dict) -> dict:
    return _validate(_REPORT_VALIDATOR, record, "crdt report")


def make_stamp(node_id: str, *, lamport: int, wall_time: datetime) -> dict:
    """One record's stamp, the ADR-003 triple: who acted,
    their Lamport clock at the act, and their wall time."""
    return validate_stamp(
        {"node_id": node_id, "lamport": lamport, "wall_time": _fmt(wall_time)}
    )


def stamp_key(stamp: dict) -> tuple:
    """The total order on stamps: lamport, then wall
    time, then node_id — ties break by node_id, so the
    order is total and identical on every replica."""
    return (stamp["lamport"], stamp["wall_time"], stamp["node_id"])


def default_policy() -> dict:
    """The stores' bounds as data: at most 512 node
    records per replica, tombstones retained for one
    registry epoch (ADR-003's own number). A policy,
    labelled as one — an operator's numbers, not facts
    about a mesh."""
    return validate_policy(
        {"version": 1, "max_nodes": 512, "tombstone_retention_epochs": 1}
    )


def _canonical(census: dict) -> str:
    return json.dumps(census, sort_keys=True)


def _tag(stamp: dict) -> tuple:
    return (stamp["node_id"], stamp["lamport"], stamp["wall_time"])


class CrdtStore:
    """One replica of the registry + membership CRDT.

    ``local_node_id`` names the holding node (the stamp
    origin for local acts, and the event source). ``now``
    is injectable so runs replay deterministically.
    Events are Y-013 envelopes from the ``registry``
    component, kept on ``events`` and also handed to
    ``event_sink`` when one is given. All state changes
    are lock-guarded.
    """

    def __init__(
        self,
        *,
        local_node_id: str,
        policy: dict | None = None,
        now=None,
        event_sink=None,
    ):
        self._node_id = local_node_id
        self._now = now or (lambda: datetime.now(UTC))
        self._policy = validate_policy(
            dict(policy) if policy is not None else default_policy()
        )
        self._event_sink = event_sink
        self._lock = threading.RLock()
        self._clock = 0
        self._epoch = 1
        self._writes: dict[str, dict] = {}
        self._tombstones: dict[str, dict] = {}
        self._model_adds: dict[tuple, dict] = {}
        self._model_removes: set[tuple] = set()
        self._events: list[dict] = []
        self._event_seq = 0
        self._writes_applied = 0
        self._writes_stale = 0
        self._removes_applied = 0
        self._merges = 0
        self._conflicts = 0
        self._pruned = 0

    # -- identity, clock, events ---------------------------

    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def clock(self) -> int:
        return self._clock

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    def _emit(self, type_: str, payload: dict) -> None:
        self._event_seq += 1
        event = make_event(
            type=type_,
            node_id=self._node_id,
            component="registry",
            seq=self._event_seq,
            at=self._now(),
            payload=payload,
        )
        self._events.append(event)
        if self._event_sink is not None:
            self._event_sink(event)

    def _observe(self, stamp: dict) -> None:
        self._clock = max(self._clock, stamp["lamport"])

    def _tick(self, origin: str | None = None) -> dict:
        self._clock += 1
        return make_stamp(
            origin or self._node_id, lamport=self._clock, wall_time=self._now()
        )

    # -- LWW element set: node records ----------------------

    def _apply_write(self, node_id: str, stamp: dict, census: dict) -> bool:
        """Apply one stamped write under the LWW rules.
        The caller holds the lock and has observed the
        stamp. Returns True when the stored write changed."""
        tombstone = self._tombstones.get(node_id)
        if tombstone is not None and stamp_key(stamp) <= stamp_key(tombstone["stamp"]):
            # Blocked as a *resolution*, never dropped as
            # state: the write is still the newest LWW
            # candidate for its key, so it is retained
            # behind the tombstone — a replica that met
            # the tombstone first and one that met the
            # write first hold identical state.
            existing = self._writes.get(node_id)
            if existing is None or stamp_key(stamp) > stamp_key(existing["stamp"]):
                self._writes[node_id] = {
                    "node_id": node_id,
                    "stamp": copy.deepcopy(stamp),
                    "census": copy.deepcopy(census),
                }
            self._writes_stale += 1
            self._emit(
                "registry.crdt_write_stale",
                {"node_id": node_id, "reason": "tombstoned"},
            )
            return False
        existing = self._writes.get(node_id)
        if existing is not None:
            new_key, old_key = stamp_key(stamp), stamp_key(existing["stamp"])
            if new_key < old_key:
                self._writes_stale += 1
                self._emit(
                    "registry.crdt_write_stale",
                    {"node_id": node_id, "reason": "stale_sequence"},
                )
                return False
            if new_key == old_key:
                if _canonical(census) == _canonical(existing["census"]):
                    return False
                self._conflicts += 1
                self._emit(
                    "registry.crdt_conflict",
                    {"node_id": node_id, "lamport": stamp["lamport"]},
                )
                if _canonical(census) >= _canonical(existing["census"]):
                    return False
                self._writes[node_id] = {
                    "node_id": node_id,
                    "stamp": copy.deepcopy(stamp),
                    "census": copy.deepcopy(census),
                }
                return True
        elif len(self._writes) >= self._policy["max_nodes"]:
            self._writes_stale += 1
            self._emit(
                "registry.crdt_write_stale",
                {"node_id": node_id, "reason": "store_full"},
            )
            return False
        self._writes[node_id] = {
            "node_id": node_id,
            "stamp": copy.deepcopy(stamp),
            "census": copy.deepcopy(census),
        }
        self._writes_applied += 1
        self._emit(
            "registry.crdt_node_written",
            {"node_id": node_id, "lamport": stamp["lamport"]},
        )
        return True

    def put_node(self, census: dict, *, stamp: dict | None = None) -> dict | None:
        """Write one node's record: its census, under a
        stamp (a local tick when none is given). The
        census must name the node its stamp claims to
        write — in fact the record's key IS the census's
        own node_id, and a stamp whose origin is a third
        party is refused outright: a node record is
        written by its node or carried by merge, never
        forged by a bystander. Returns the resolved
        record, or ``None`` when the write was stale."""
        census = copy.deepcopy(census)
        errors = sorted(_CENSUS_VALIDATOR.iter_errors(census), key=str)
        if errors:
            raise CrdtError(f"capability census fails its schema: {errors[0].message}")
        node_id = census["node_id"]
        with self._lock:
            if stamp is None:
                stamp = self._tick(node_id)
            else:
                stamp = validate_stamp(copy.deepcopy(stamp))
                if stamp["node_id"] != node_id:
                    raise CrdtError(
                        f"a write for {node_id} cannot carry a stamp "
                        f"authored by {stamp['node_id']}"
                    )
                self._observe(stamp)
            self._apply_write(node_id, stamp, census)
            return self.node(node_id)

    def _apply_tombstone(self, node_id: str, stamp: dict, epoch: int) -> bool:
        """Apply one stamped delete under the LWW rules.
        The caller holds the lock and has observed the
        stamp. Returns True when the node left (or stayed
        gone under a newer tombstone)."""
        existing = self._tombstones.get(node_id)
        if existing is not None and stamp_key(stamp) <= stamp_key(existing["stamp"]):
            return False
        # The tombstone is retained even when a newer
        # write currently outranks it — it is state, and
        # dropping it would make convergence depend on
        # whether a replica met the write or the delete
        # first. It simply resolves to nothing while the
        # fresher write stands.
        self._tombstones[node_id] = {
            "node_id": node_id,
            "stamp": copy.deepcopy(stamp),
            "epoch": epoch,
        }
        self._removes_applied += 1
        self._emit(
            "registry.crdt_node_removed",
            {"node_id": node_id, "lamport": stamp["lamport"]},
        )
        return self.node(node_id) is None

    def remove_node(self, node_id: str, *, stamp: dict | None = None) -> bool:
        """Delete one node's record under a stamp (a local
        tick when none is given): the record goes, a
        tombstone stays for the policy's epochs, and an
        older write arriving later cannot resurrect the
        node. Returns True when the tombstone stands
        newer than the node's write."""
        with self._lock:
            if stamp is None:
                stamp = self._tick()
            else:
                stamp = validate_stamp(copy.deepcopy(stamp))
                self._observe(stamp)
            return self._apply_tombstone(node_id, stamp, self._epoch)

    # -- Observed-remove set: models -------------------------

    def add_model(
        self, node_id: str, model: str, *, stamp: dict | None = None
    ) -> dict:
        """Offer one model on one node: an add tagged with
        its stamp (a local tick when none is given). The
        add is independent of the node's record — offers
        and records travel separately and meet at read
        time, which is what lets merge order never
        matter. Returns the add's stamp (its tag)."""
        with self._lock:
            if stamp is None:
                stamp = self._tick(node_id)
            else:
                stamp = validate_stamp(copy.deepcopy(stamp))
                if stamp["node_id"] != node_id:
                    raise CrdtError(
                        f"a model offer on {node_id} cannot carry a stamp "
                        f"authored by {stamp['node_id']}"
                    )
                self._observe(stamp)
            key = (node_id, model, _tag(stamp))
            if key not in self._model_adds:
                self._model_adds[key] = {
                    "node_id": node_id,
                    "model": model,
                    "stamp": copy.deepcopy(stamp),
                }
                self._emit(
                    "registry.crdt_model_added",
                    {"node_id": node_id, "model": model},
                )
            return copy.deepcopy(stamp)

    def remove_model(self, node_id: str, model: str) -> bool:
        """Withdraw one model offer, observed-remove
        style: exactly the adds this replica has observed
        are tombstoned. An add never observed here is
        untouched — when it arrives later it is present,
        because this remove never saw it to kill it.
        Returns True when at least one observed add was
        removed."""
        with self._lock:
            observed = [
                key
                for key in self._model_adds
                if key[0] == node_id and key[1] == model and key not in self._model_removes
            ]
            if not observed:
                return False
            for key in observed:
                self._model_removes.add(key)
            self._removes_applied += 1
            self._emit(
                "registry.crdt_model_removed",
                {"node_id": node_id, "model": model},
            )
            return True

    def _present_models(self, node_id: str) -> list[str]:
        return sorted(
            {
                key[1]
                for key in self._model_adds
                if key[0] == node_id and key not in self._model_removes
            }
        )

    # -- resolved views ---------------------------------------

    def node(self, node_id: str) -> dict | None:
        """One node's resolved record, or ``None``: the
        node is present exactly when it holds a write and
        that write outranks its tombstone, if any."""
        with self._lock:
            write = self._writes.get(node_id)
            if write is None:
                return None
            tombstone = self._tombstones.get(node_id)
            if tombstone is not None and stamp_key(write["stamp"]) <= stamp_key(
                tombstone["stamp"]
            ):
                return None
            record = {
                "version": 1,
                "node_id": node_id,
                "stamp": copy.deepcopy(write["stamp"]),
                "census": copy.deepcopy(write["census"]),
                "models": self._present_models(node_id),
                "simulated": True,
            }
            return validate_record(record)

    def nodes(self) -> list[dict]:
        """Every present node's resolved record, sorted by
        node id — this replica's whole resolved truth."""
        with self._lock:
            found = []
            for node_id in sorted(self._writes):
                record = self.node(node_id)
                if record is not None:
                    found.append(record)
            return found

    def nodes_with_model(self, model: str) -> list[str]:
        """The present nodes currently offering this model,
        sorted — the registry question placement asks."""
        with self._lock:
            return sorted(
                record["node_id"]
                for record in self.nodes()
                if model in record["models"]
            )

    # -- state export and merge --------------------------------

    def state(self) -> dict:
        """This replica's full mergeable state as a
        validated ``crdt_state`` v1: newest write per
        node, tombstones with their epochs, model adds
        and the removes naming the adds they observed —
        everything a merge needs, nothing it doesn't."""
        with self._lock:
            record = {
                "version": 1,
                "epoch": self._epoch,
                "simulated": True,
                "writes": [
                    copy.deepcopy(self._writes[k]) for k in sorted(self._writes)
                ],
                "tombstones": [
                    copy.deepcopy(self._tombstones[k])
                    for k in sorted(self._tombstones)
                ],
                "model_adds": [
                    copy.deepcopy(self._model_adds[k])
                    for k in sorted(self._model_adds)
                ],
                "model_removes": [
                    {
                        "node_id": key[0],
                        "model": key[1],
                        "stamp": {
                            "node_id": key[2][0],
                            "lamport": key[2][1],
                            "wall_time": key[2][2],
                        },
                    }
                    for key in sorted(self._model_removes)
                ],
            }
            return validate_state(record)

    def merge_state(self, state: dict) -> dict:
        """Merge another replica's exported state into
        this one: the union under the stamp order. Every
        element is applied by the same rules a local act
        obeys, so merging is idempotent (the same state
        twice changes nothing the second time) and
        order-insensitive (any order of merges over the
        same states resolves identically) — the ADR-003
        properties the suite proves. Returns the report."""
        record = validate_state(copy.deepcopy(state))
        with self._lock:
            self._merges += 1
            for write in record["writes"]:
                self._observe(write["stamp"])
                self._apply_write(write["node_id"], write["stamp"], write["census"])
            for tombstone in record["tombstones"]:
                self._observe(tombstone["stamp"])
                self._apply_tombstone(
                    tombstone["node_id"], tombstone["stamp"], tombstone["epoch"]
                )
            for add in record["model_adds"]:
                self._observe(add["stamp"])
                key = (add["node_id"], add["model"], _tag(add["stamp"]))
                if key not in self._model_adds:
                    self._model_adds[key] = copy.deepcopy(add)
            for remove in record["model_removes"]:
                self._observe(remove["stamp"])
                self._model_removes.add(
                    (remove["node_id"], remove["model"], _tag(remove["stamp"]))
                )
            self._emit(
                "registry.crdt_merged",
                {
                    "writes": len(record["writes"]),
                    "tombstones": len(record["tombstones"]),
                    "model_adds": len(record["model_adds"]),
                },
            )
            return self.report()

    def merge(self, other: "CrdtStore") -> dict:
        """Merge another replica whole: its exported state
        into this replica. One-directional by design —
        convergence is both replicas merging each other,
        in either order, which the suite proves lands
        both at the same state."""
        if other is self:
            raise CrdtError("a replica cannot merge with itself")
        return self.merge_state(other.state())

    def sync(self, other: "CrdtStore") -> None:
        """Merge both ways: each replica ends the call
        holding the union — the pairwise shape gossip
        (Y-055) will carry once real carriage (Y-062)
        gives CRDT state a wire."""
        if other is self:
            raise CrdtError("a replica cannot sync with itself")
        mine = self.state()
        theirs = other.state()
        self.merge_state(theirs)
        other.merge_state(mine)

    # -- epochs --------------------------------------------------

    def advance_epoch(self) -> int:
        """Advance the registry epoch and prune what has
        outlived its retention: a tombstone struck more
        than the policy's retention epochs ago is dropped
        — together with the write it superseded, so the
        prune itself can never resurrect a node on this
        replica. A fresher write for the same node is
        untouched (it already outranked the tombstone).
        Returns the new epoch."""
        with self._lock:
            self._epoch += 1
            horizon = self._epoch - self._policy["tombstone_retention_epochs"]
            for node_id in sorted(self._tombstones):
                tombstone = self._tombstones[node_id]
                if tombstone["epoch"] >= horizon:
                    continue
                write = self._writes.get(node_id)
                if write is not None and stamp_key(write["stamp"]) <= stamp_key(
                    tombstone["stamp"]
                ):
                    del self._writes[node_id]
                del self._tombstones[node_id]
                self._pruned += 1
                self._emit(
                    "registry.crdt_tombstone_pruned", {"node_id": node_id}
                )
            return self._epoch

    # -- the counted state ----------------------------------------

    def report(self) -> dict:
        """This replica's counted state as a validated
        ``crdt_report`` v1: present and tombstoned nodes,
        present model offers, writes applied and refused
        stale, removes, merges, conflicts, epoch prunes,
        the Lamport clock and epoch — with the resolved
        records themselves."""
        with self._lock:
            records = self.nodes()
            record = {
                "version": 1,
                "at": _fmt(self._now()),
                "local_node_id": self._node_id,
                "simulated": True,
                "epoch": self._epoch,
                "lamport_clock": self._clock,
                "nodes_present": len(records),
                "nodes_tombstoned": len(self._tombstones),
                "models_present": sum(len(r["models"]) for r in records),
                "writes_applied": self._writes_applied,
                "writes_stale": self._writes_stale,
                "removes_applied": self._removes_applied,
                "model_adds": len(self._model_adds),
                "model_removes": len(self._model_removes),
                "merges": self._merges,
                "conflicts": self._conflicts,
                "tombstones_pruned": self._pruned,
                "records": records,
                "withdrawn_node_ids": sorted(self._tombstones),
            }
            return validate_report(record)
