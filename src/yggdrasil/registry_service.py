"""Registry service — Y-053.

Nodes register census + models; the registry is itself
replicated — no single registrar to kill. That second
clause is schema law, not architecture taste: the
``registry_policy`` v1 carries ``replicated: true`` and
``single_registrar_allowed: false`` as constants, so a
policy that made one node the registry fails validation.

Every :class:`RegistryService` is a **full replica**: it
holds every entry it has accepted or learned, answers
every query from its own copy, and accepts registrations
for any node. A node registers its measured Y-022 census
and its Y-008 model entries as a validated
``registry_registration`` v1 — a per-node rising
``seq``, the census riding whole (its ``node_id`` must
agree with the registration's, or the registration is
refused ``census_node_mismatch`` by name) and the model
entries riding whole beside it. A registration is a
node's word about *itself*; the census stays labelled
with its own measured moment and the entries with their
own provenance grades — the registry repeats facts, it
never upgrades them.

Replication is replica-to-replica sync:
``sync_with(other)`` snapshots both replicas' entries
and withdrawal tombstones and merges each into the
other, so both end the call holding the union. The
merge rules are the same on every replica and depend
only on the records, never on which replica is asked
first — replicas converge to the same entries whichever
order syncs happen in:

- an unknown node is adopted (counted in
  ``entries_learned_via_replication``);
- a newer ``seq`` replaces the older entry whole;
- the same ``seq`` with the same census + models is a
  no-op; the same ``seq`` with different facts is a
  **conflict** — counted, evented, and settled
  deterministically (the canonically smaller record is
  kept on every replica, so the settlement itself
  converges);
- an older ``seq`` is ignored — it is history, not news.

Withdrawal is a tombstone, not a deletion whispered to
one replica: ``withdraw(node_id, seq)`` with a sequence
newer than the entry's removes the entry and records
the tombstone, syncs carry tombstones with the entries,
and a registration at or below a tombstone's sequence
is refused ``stale_sequence`` wherever it arrives —
a withdrawn node cannot be resurrected by an old
registration still travelling the mesh, and a stale
withdrawal cannot kill a fresher entry.

The registrar's death drill is the slice's own
acceptance shape: register at one replica, sync, kill
that replica (drop it) — every other replica still
serves the entry whole, and a brand-new replica syncing
with any survivor learns it. There is no registrar to
kill because there is no registrar: only replicas.

Honesty: replication here is an in-process model —
records and reports carry ``simulated: true`` as a
schema constant, no socket is opened and no LAN is
crossed. What is proven is the registry *rules*:
registration, the census-agreement door, sequence
freshness, bounded size, withdrawal tombstones,
deterministic conflict settlement, and convergence
under sync. Epidemic gossip of this same state is
Y-055, its CRDT formalisation is Y-056, real carriage
over the fabric + TLS is Y-062, and hardware proof is
Part VIII's. Heartbeat liveness (present → suspect →
gone) is Y-054: a registry entry is a node's registered
facts, not a claim that the node is alive right now.
No node-agent seam — as with discovery (Y-052), the
agent gains registry traffic when the heartbeat slice
gives it words of its own (Y-054).
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
    """A validator for one schema, with every contract schema
    registered so relative $refs resolve — the registry
    pattern the earlier slices build."""
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


_POLICY_VALIDATOR = _validator("registry_policy.schema.json")
_REGISTRATION_VALIDATOR = _validator("registry_registration.schema.json")
_ENTRY_VALIDATOR = _validator("registry_entry.schema.json")
_REPORT_VALIDATOR = _validator("registry_report.schema.json")


class RegistryError(Exception):
    """The registry service's own failure: a policy,
    registration, entry, or report that fails its schema,
    or a use of the API that would manufacture truth —
    building a registration whose census names another
    node, or syncing a replica with itself."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise RegistryError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_policy(record: dict) -> dict:
    return _validate(_POLICY_VALIDATOR, record, "registry policy")


def validate_registration(record: dict) -> dict:
    return _validate(_REGISTRATION_VALIDATOR, record, "registry registration")


def validate_entry(record: dict) -> dict:
    return _validate(_ENTRY_VALIDATOR, record, "registry entry")


def validate_report(record: dict) -> dict:
    return _validate(_REPORT_VALIDATOR, record, "registry report")


def default_policy() -> dict:
    """The registry's bounds as data: one replica holds at
    most 512 node entries. A policy, labelled as one — an
    operator's number, not a fact about a mesh. The two
    constants are not numbers at all: the registry is
    always replicated, and a single registrar is never
    allowed to be its only holder."""
    return validate_policy(
        {
            "version": 1,
            "max_entries": 512,
            "replicated": True,
            "single_registrar_allowed": False,
        }
    )


def _canonical(census: dict, models: list) -> str:
    return json.dumps({"census": census, "models": models}, sort_keys=True)


def build_registration(
    node_id: str,
    *,
    seq: int,
    census: dict,
    models: list,
    at: datetime | None = None,
) -> dict:
    """One node's registration, built honestly: the census
    must name the registering node itself — a registration
    carrying another node's census is refused here, at
    construction, before any replica is asked."""
    if census.get("node_id") != node_id:
        raise RegistryError(
            f"census is for {census.get('node_id')}, registration is for {node_id}"
        )
    record = {
        "version": 1,
        "node_id": node_id,
        "seq": seq,
        "census": copy.deepcopy(census),
        "models": copy.deepcopy(models),
        "registered_at": _fmt(at or datetime.now(UTC)),
        "simulated": True,
    }
    return validate_registration(record)


class RegistryService:
    """One full replica of the registry, held by one node.

    ``registry_node_id`` names the holding node (the
    event source, and the origin recorded on entries this
    replica accepts directly). ``now`` is injectable so
    runs replay deterministically. Events are Y-013
    envelopes from the ``registry`` component, kept on
    ``events`` and also handed to ``event_sink`` when one
    is given. All state changes are lock-guarded.
    """

    def __init__(
        self,
        *,
        registry_node_id: str,
        policy: dict | None = None,
        now=None,
        event_sink=None,
    ):
        self._node_id = registry_node_id
        self._now = now or (lambda: datetime.now(UTC))
        self._policy = validate_policy(
            dict(policy) if policy is not None else default_policy()
        )
        self._event_sink = event_sink
        self._lock = threading.RLock()
        self._entries: dict[str, dict] = {}
        self._tombstones: dict[str, int] = {}
        self._issued_seq: dict[str, int] = {}
        self._events: list[dict] = []
        self._event_seq = 0
        self._accepted = 0
        self._refused = 0
        self._refused_reasons: dict[str, int] = {}
        self._learned = 0
        self._syncs = 0
        self._withdrawals = 0
        self._conflicts = 0

    # -- identity and events ---------------------------------

    @property
    def node_id(self) -> str:
        return self._node_id

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

    def _refuse(self, reason: str, node_id) -> None:
        self._refused += 1
        self._refused_reasons[reason] = self._refused_reasons.get(reason, 0) + 1
        self._emit(
            "registry.registration_refused",
            {"node_id": node_id, "reason": reason},
        )

    # -- registration ------------------------------------------

    def register(self, registration: dict) -> dict | None:
        """Accept one node's registration, or refuse it by
        name — every refusal named, counted, and evented,
        nothing silently dropped and nothing admitted on a
        claim its own census contradicts. Returns the
        stored entry, or ``None`` when refused."""
        with self._lock:
            try:
                record = validate_registration(copy.deepcopy(registration))
            except RegistryError:
                return self._refuse(
                    "malformed_registration",
                    registration.get("node_id")
                    if isinstance(registration, dict)
                    else None,
                )
            node_id = record["node_id"]
            if record["census"].get("node_id") != node_id:
                return self._refuse("census_node_mismatch", node_id)
            names = [m["name"] for m in record["models"]]
            if len(set(names)) != len(names):
                return self._refuse("duplicate_model", node_id)
            tombstone = self._tombstones.get(node_id)
            if tombstone is not None and record["seq"] <= tombstone:
                return self._refuse("stale_sequence", node_id)
            existing = self._entries.get(node_id)
            if existing is not None:
                if record["seq"] < existing["seq"]:
                    return self._refuse("stale_sequence", node_id)
                if record["seq"] == existing["seq"]:
                    if _canonical(record["census"], record["models"]) != _canonical(
                        existing["census"], existing["models"]
                    ):
                        self._conflicts += 1
                        return self._refuse("conflicting_sequence", node_id)
                    refreshed = copy.deepcopy(existing)
                    refreshed["last_registered_at"] = record["registered_at"]
                    self._entries[node_id] = validate_entry(refreshed)
                    self._accepted += 1
                    return copy.deepcopy(refreshed)
            elif len(self._entries) >= self._policy["max_entries"]:
                return self._refuse("registry_full", node_id)
            entry = {
                "version": 1,
                "node_id": node_id,
                "seq": record["seq"],
                "census": record["census"],
                "models": record["models"],
                "first_registered_at": (
                    existing["first_registered_at"]
                    if existing is not None
                    else record["registered_at"]
                ),
                "last_registered_at": record["registered_at"],
                "origin_registry": (
                    existing["origin_registry"]
                    if existing is not None
                    else self._node_id
                ),
                "simulated": True,
            }
            entry = validate_entry(entry)
            self._entries[node_id] = entry
            self._accepted += 1
            self._emit(
                "registry.registered"
                if existing is None
                else "registry.registration_updated",
                {"node_id": node_id, "seq": record["seq"]},
            )
            return copy.deepcopy(entry)

    def register_local(self, census: dict, models: list) -> dict:
        """This replica's holding node registers itself:
        the sequence is issued for it here — one past the
        newest sequence this replica has issued, holds, or
        has tombstoned for that node, so its own word can
        never arrive stale at home."""
        node_id = census.get("node_id")
        if not isinstance(node_id, str):
            raise RegistryError("a census without a node_id cannot register")
        with self._lock:
            highest = self._issued_seq.get(node_id, 0)
            if node_id in self._entries:
                highest = max(highest, self._entries[node_id]["seq"])
            if node_id in self._tombstones:
                highest = max(highest, self._tombstones[node_id])
            seq = highest + 1
            self._issued_seq[node_id] = seq
        registration = build_registration(
            node_id, seq=seq, census=census, models=models, at=self._now()
        )
        entry = self.register(registration)
        if entry is None:  # pragma: no cover - register_local's seq is fresh by construction
            raise RegistryError(f"local registration refused for {node_id}")
        return entry

    # -- withdrawal ----------------------------------------------

    def withdraw(self, node_id: str, *, seq: int) -> bool:
        """Withdraw one node's entry under a sequence newer
        than the entry's own: the entry goes, a tombstone
        stays, and both travel on every later sync. A
        withdrawal at or below a live entry's sequence is
        stale — refused by name, the entry untouched.
        Withdrawing a node with no entry still plants the
        tombstone, so an old registration for it arriving
        later is refused rather than resurrecting it."""
        with self._lock:
            existing = self._entries.get(node_id)
            if existing is not None and seq <= existing["seq"]:
                self._refused += 1
                self._refused_reasons["withdrawal_stale"] = (
                    self._refused_reasons.get("withdrawal_stale", 0) + 1
                )
                self._emit(
                    "registry.registration_refused",
                    {"node_id": node_id, "reason": "withdrawal_stale"},
                )
                return False
            if seq <= self._tombstones.get(node_id, 0):
                return False
            self._tombstones[node_id] = seq
            if existing is None:
                return False
            del self._entries[node_id]
            self._withdrawals += 1
            self._emit("registry.withdrawn", {"node_id": node_id, "seq": seq})
            return True

    def tombstones(self) -> dict:
        with self._lock:
            return dict(self._tombstones)

    # -- replication -----------------------------------------------

    def _merge_entry(self, incoming: dict) -> None:
        """Merge one replicated entry under the mesh-wide
        rules (see the module docstring). The caller holds
        the lock and has already applied tombstones."""
        node_id = incoming["node_id"]
        if incoming["seq"] <= self._tombstones.get(node_id, 0):
            return
        existing = self._entries.get(node_id)
        if existing is None:
            if len(self._entries) >= self._policy["max_entries"]:
                self._conflicts += 1
                return
            self._entries[node_id] = validate_entry(copy.deepcopy(incoming))
            self._learned += 1
            self._emit(
                "registry.entry_replicated",
                {"node_id": node_id, "seq": incoming["seq"]},
            )
            return
        if incoming["seq"] > existing["seq"]:
            self._entries[node_id] = validate_entry(copy.deepcopy(incoming))
            self._learned += 1
            self._emit(
                "registry.entry_replicated",
                {"node_id": node_id, "seq": incoming["seq"]},
            )
            return
        if incoming["seq"] == existing["seq"] and _canonical(
            incoming["census"], incoming["models"]
        ) != _canonical(existing["census"], existing["models"]):
            self._conflicts += 1
            self._emit(
                "registry.conflict",
                {"node_id": node_id, "seq": incoming["seq"]},
            )
            # deterministic settlement: the canonically
            # smaller record is kept on every replica
            if _canonical(incoming["census"], incoming["models"]) < _canonical(
                existing["census"], existing["models"]
            ):
                self._entries[node_id] = validate_entry(copy.deepcopy(incoming))

    def _apply_sync(self, entries: list, tombstones: dict) -> None:
        with self._lock:
            for node_id, seq in sorted(tombstones.items()):
                if seq > self._tombstones.get(node_id, 0):
                    existing = self._entries.get(node_id)
                    if existing is not None and existing["seq"] < seq:
                        del self._entries[node_id]
                        self._withdrawals += 1
                        self._emit(
                            "registry.withdrawn",
                            {"node_id": node_id, "seq": seq},
                        )
                    self._tombstones[node_id] = seq
            for entry in entries:
                try:
                    record = validate_entry(copy.deepcopy(entry))
                except RegistryError:
                    self._conflicts += 1
                    continue
                self._merge_entry(record)

    def sync_with(self, other: "RegistryService") -> None:
        """Sync this replica with another, both ways: each
        ends the call holding the union of both replicas'
        entries and tombstones, merged under the same
        rules — so the pair converges, and either one may
        die afterwards without the truth noticing."""
        if other is self:
            raise RegistryError("a replica cannot sync with itself")
        mine = self.entries()
        theirs = other.entries()
        my_tombstones = self.tombstones()
        their_tombstones = other.tombstones()
        self._apply_sync(theirs, their_tombstones)
        other._apply_sync(mine, my_tombstones)
        with self._lock:
            self._syncs += 1
        with other._lock:
            other._syncs += 1

    # -- queries ------------------------------------------------------

    def entries(self) -> list[dict]:
        """Every entry this replica holds, sorted by node
        id — the replica's whole truth, from its own copy."""
        with self._lock:
            return [copy.deepcopy(self._entries[k]) for k in sorted(self._entries)]

    def entry(self, node_id: str) -> dict | None:
        """One named node's entry, or ``None`` — named and
        absent is an honest answer, never an error."""
        with self._lock:
            found = self._entries.get(node_id)
            return copy.deepcopy(found) if found is not None else None

    def nodes_with_model(self, model_name: str) -> list[str]:
        """The nodes whose registered model entries name
        this model, sorted — the registry question Part IV
        placement asks first."""
        with self._lock:
            return sorted(
                node_id
                for node_id, entry in self._entries.items()
                if any(m["name"] == model_name for m in entry["models"])
            )

    def nodes_with_capability(self, capability: str) -> list[str]:
        """The nodes whose registered census carries a
        reachable engine claiming this capability, sorted.
        A census claim is repeated exactly as measured —
        the registry never upgrades it."""
        with self._lock:
            found = []
            for node_id, entry in self._entries.items():
                for engine in entry["census"].get("engines", []):
                    if engine.get("reachable") and engine.get(
                        "capabilities", {}
                    ).get(capability):
                        found.append(node_id)
                        break
            return sorted(found)

    # -- the counted state ----------------------------------------------

    def report(self) -> dict:
        """This replica's counted state as a validated
        ``registry_report`` v1: the entries held, the
        distinct models offered, every registration
        accepted, refused and why, what replication taught
        it, syncs, withdrawals, conflicts, and the
        withdrawn nodes — names and counts only."""
        with self._lock:
            held = self.entries()
            record = {
                "version": 1,
                "at": _fmt(self._now()),
                "local_node_id": self._node_id,
                "simulated": True,
                "entries": held,
                "entry_count": len(held),
                "model_count": len(
                    {m["name"] for e in held for m in e["models"]}
                ),
                "registrations_accepted": self._accepted,
                "registrations_refused": self._refused,
                "refused_reasons": dict(sorted(self._refused_reasons.items())),
                "entries_learned_via_replication": self._learned,
                "syncs_performed": self._syncs,
                "withdrawals": self._withdrawals,
                "conflicts": self._conflicts,
                "withdrawn_node_ids": sorted(self._tombstones),
            }
            return validate_report(record)
