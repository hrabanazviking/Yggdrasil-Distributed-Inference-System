"""The registrar's death drill — Y-067.

Y-053 built the registry replicated — no single
registrar to kill — and named this drill its own
acceptance shape. Y-054 built the peer's verdict on
a silent node: present → suspect → gone, aged on
the observer's own clock. This slice runs the
drill the roadmap orders, as one judged exercise
kept by :class:`DeathDrill`:

- **The mesh is started in order, and the order is
  remembered.** Every node gets one full Y-053
  registry replica and one Y-054 liveness tracker,
  composed, neither rewired. The first node ever
  started is the drill's registrar: the node the
  mesh first registered through and synced from.
- **The kill is aimed by law, not by choice.** The
  ``death_drill_policy`` v1 fixes ``kill_target:
  "first_started"``, ``single_registrar_allowed:
  false``, and ``expected_effect:
  "missing_heartbeat_only"`` as schema constants.
  ``kill`` accepts only the first-started node;
  killing any other node is refused
  ``not_first_node`` (killing nodes mid-task in
  general is Y-073's failure drills), a second
  kill ``already_dead``, a kill with nothing
  started ``no_first_node``.
- **The kill drops the node whole.** Its replica
  and tracker objects are thrown away; it sends no
  more heartbeats and answers no more acts — every
  act aimed at it afterwards is refused
  ``node_down`` by name. Those expected refusals
  are not disruptions: they are the mesh correctly
  naming a dead door.
- **What the survivors notice is measured, not
  asserted.** Each survivor's own Y-054 tracker
  ages the first node on the observer's clock as
  its heartbeats stop: ``missed_beats`` climbs,
  the verdict walks present → suspect → gone. The
  survivors keep beating *each other*, so their
  verdicts on one another stay present — the only
  standing that changes anywhere in the mesh is
  the first node's.
- **Everything else must keep working, and the
  drill only passes when it proves so.** After the
  kill the survivors must accept a registration,
  sync registry truth between themselves, and
  admit a brand-new node that learns the dead
  registrar's own entry whole from a survivor.
  The report counts registrations, syncs, joins,
  and lookups after the kill apart.
- **Loss and disruption are counted, never
  smoothed over.** ``entries_at_kill`` is the
  registry union the mesh held the moment before
  the kill (the victim's own replica included);
  any of it missing from *every* survivor at
  report time is ``entries_lost`` — an entry the
  registrar held that was never replicated *is*
  lost, and the drill says so and fails. A
  registration or sync between survivors refused
  after the kill is a ``disruption``.
  ``effects_beyond_missing_heartbeat`` is losses
  plus disruptions, and the verdict is ``passed``
  only when it stands at zero with the first node
  gone on every survivor tracker that ever heard
  it beat (a node that joined after the kill never
  heard it and honestly holds no verdict) and the
  post-kill proofs done; ``failed`` when anything
  was lost or disrupted; ``incomplete`` until the
  drill has run that far.

Honesty: everything here is SIMULATED —
``simulated: true`` is a schema constant on the
record and the report. A "kill" is the in-process
loss of one node's replica and tracker objects;
no process was killed, no socket opened, no LAN
crossed (real carriage was Y-062's own proof;
killing real processes on real citizens is Part
VIII's hardware road, and Y-066's restart of a
dead node is a different act from this drill's
one-way kill).
"""

import copy
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.health_endpoint import build_node_health
from yggdrasil.liveness import LivenessTracker
from yggdrasil.observability import make_event
from yggdrasil.registry_service import RegistryError, RegistryService

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


_POLICY_VALIDATOR = _validator("death_drill_policy.schema.json")
_RECORD_VALIDATOR = _validator("death_drill_record.schema.json")
_REPORT_VALIDATOR = _validator("death_drill_report.schema.json")


class DeathDrillError(Exception):
    """The drill's own failure: a policy, record, or
    report that fails its contract, or a use of the
    API that would manufacture a standing — asking
    for a dead or unknown node's replica or tracker.
    (Judged drill acts are refused by named record
    instead; see ``kill`` and friends.)"""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record, kind: str):
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise DeathDrillError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_policy(record: dict) -> dict:
    return _validate(_POLICY_VALIDATOR, record, "death drill policy")


def validate_record(record: dict) -> dict:
    return _validate(_RECORD_VALIDATOR, record, "death drill record")


def validate_report(record: dict) -> dict:
    return _validate(_REPORT_VALIDATOR, record, "death drill report")


def default_policy() -> dict:
    """The drill's law as data: the first node ever
    started is the kill target, no single registrar
    may be the registry's only holder, the only
    effect a passed drill may show is a missing
    heartbeat, and one drill mesh holds at most 16
    nodes — a policy, not a measurement."""
    return validate_policy(
        {
            "version": 1,
            "kill_target": "first_started",
            "single_registrar_allowed": False,
            "expected_effect": "missing_heartbeat_only",
            "max_nodes": 16,
        }
    )


def _make_beat(node_id: str, seq: int, at: datetime) -> dict:
    """One node's own heartbeat, built by the Y-030
    door exactly as the liveness suite builds them:
    a node reports itself present, never suspect."""
    health = {
        "node_id": node_id,
        "at": _fmt(at),
        "liveness": "present",
        "queue_depth": 0,
        "models_loaded": [],
    }
    return build_node_health(
        node_id=node_id,
        seq=seq,
        lifecycle_state="joined",
        health=health,
        load={
            "in_flight": 0,
            "queue_depth": 0,
            "active_tasks": 0,
            "executions": 0,
            "thermal_c": None,
            "memory_fraction": None,
            "cpu_share_used": None,
        },
        models={"loaded": [], "present": None, "loader_simulated": None},
        governor_state=None,
        census={"measured": False, "measured_at": None, "fingerprint": None},
        uptime_seconds=1.0,
    )


class _Member:
    """One started node's standing at the drill: its
    registry replica and liveness tracker (both
    ``None`` once killed — the kill drops them
    whole), its start order, and its beat sequence."""

    def __init__(self, node_id: str, order: int, registry, tracker):
        self.node_id = node_id
        self.order = order
        self.registry: RegistryService | None = registry
        self.tracker: LivenessTracker | None = tracker
        self.beat_seq = 0
        self.status = "up"
        self.entries_at_kill = 0


class DeathDrill:
    """The registrar's death drill: a mesh of Y-053
    replicas and Y-054 trackers, started in order,
    whose first node is killed under judgement.
    ``now`` is injectable so ageing is testable."""

    def __init__(self, policy: dict | None = None, *, now=None, liveness_policy=None):
        self._policy = validate_policy(
            copy.deepcopy(policy) if policy is not None else default_policy()
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._liveness_policy = (
            copy.deepcopy(liveness_policy) if liveness_policy else None
        )
        self._lock = threading.RLock()
        self._members: dict[str, _Member] = {}
        self._first_node_id: str | None = None
        self._records: list[dict] = []
        self._refusals_by_reason: dict[str, int] = {}
        self._kills = 0
        self._heartbeats_sent = 0
        self._registrations_accepted = 0
        self._registrations_after_kill = 0
        self._syncs = 0
        self._syncs_after_kill = 0
        self._joins_after_kill = 0
        self._lookups_after_kill = 0
        self._disruptions = 0
        self._entries_at_kill_ids: set[str] = set()
        self._events: list[dict] = []
        self._event_seq = 0

    @property
    def policy(self) -> dict:
        return copy.deepcopy(self._policy)

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    @property
    def first_node_id(self) -> str | None:
        return self._first_node_id

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

    def _record(self, action, node_id, peer_id, verdict, reason) -> dict:
        record = {
            "version": 1,
            "seq": len(self._records) + 1,
            "at": _fmt(self._now()),
            "action": action,
            "node_id": node_id,
            "peer_id": peer_id,
            "verdict": verdict,
            "reason": reason,
            "simulated": True,
        }
        checked = validate_record(record)
        self._records.append(checked)
        if verdict == "refused":
            self._refusals_by_reason[reason] = (
                self._refusals_by_reason.get(reason, 0) + 1
            )
            self._emit(
                "death_drill.act_refused",
                node_id or peer_id or self._first_node_id or "drill",
                {
                    "action": action,
                    "node_id": node_id or "unknown",
                    "reason": reason,
                },
            )
        return checked

    def _member(self, node_id: str) -> _Member:
        member = self._members.get(node_id)
        if member is None:
            raise DeathDrillError(f"unknown_node: node {node_id} was never started")
        return member

    def _live(self, node_id: str) -> _Member:
        member = self._member(node_id)
        if member.status != "up":
            raise DeathDrillError(f"node_down: node {node_id} is down")
        return member

    # -- membership --------------------------------------

    def start_node(self, node_id: str) -> dict:
        """Start one node into the drill mesh with a
        fresh registry replica and liveness tracker.
        The first node ever started is remembered as
        the registrar — the drill's kill target. A
        duplicate start, or one past the policy's
        bound, is refused by name."""
        with self._lock:
            if node_id in self._members:
                return self._record("start", node_id, None, "refused", "duplicate_node")
            if len(self._members) >= self._policy["max_nodes"]:
                return self._record("start", node_id, None, "refused", "mesh_full")
            registry = RegistryService(registry_node_id=node_id, now=self._now)
            tracker = LivenessTracker(
                observer_node_id=node_id, policy=self._liveness_policy, now=self._now
            )
            member = _Member(node_id, len(self._members) + 1, registry, tracker)
            self._members[node_id] = member
            if self._first_node_id is None:
                self._first_node_id = node_id
            if self._kills:
                self._joins_after_kill += 1
            self._emit(
                "death_drill.node_started",
                node_id,
                {"node_id": node_id, "started_order": member.order},
            )
            return self._record("start", node_id, None, "accepted", None)

    def registry(self, node_id: str) -> RegistryService:
        """A live node's registry replica — the handle
        callers read entries through. A dead or unknown
        node has none and asking raises by name."""
        with self._lock:
            return self._live(node_id).registry

    def tracker(self, node_id: str) -> LivenessTracker:
        """A live node's liveness tracker. A dead or
        unknown node has none and asking raises."""
        with self._lock:
            return self._live(node_id).tracker

    def status(self, node_id: str) -> str:
        """One node's standing: ``up`` or ``down``."""
        with self._lock:
            return self._member(node_id).status

    # -- registry acts -------------------------------------

    def register(self, node_id: str, *, via: str, census: dict, models: list) -> dict:
        """Register one node's census + models at the
        replica named by ``via`` — judged: an unknown
        or down registrar door, a down registering
        node, or a registration the Y-053 door itself
        refuses is refused by name, and a refusal
        between survivors after the kill is counted a
        disruption (the mesh failing at its own work,
        not a door correctly naming the dead)."""
        with self._lock:
            registrar = self._members.get(via)
            if registrar is None:
                return self._record("register", node_id, via, "refused", "unknown_node")
            member = self._members.get(node_id)
            if member is None:
                return self._record("register", node_id, via, "refused", "unknown_node")
            if registrar.status != "up" or member.status != "up":
                return self._record("register", node_id, via, "refused", "node_down")
            if not isinstance(census, dict) or census.get("node_id") != node_id:
                # The act names one node; a census naming
                # another is the registry door's own
                # refusal, judged here before any replica
                # is asked to register the wrong node.
                if self._kills:
                    self._disruptions += 1
                return self._record(
                    "register", node_id, via, "refused", "registry_refused"
                )
            try:
                entry = registrar.registry.register_local(
                    copy.deepcopy(census), copy.deepcopy(models)
                )
            except RegistryError:
                if self._kills:
                    self._disruptions += 1
                return self._record(
                    "register", node_id, via, "refused", "registry_refused"
                )
            if entry is None:  # pragma: no cover - register_local raises instead
                if self._kills:
                    self._disruptions += 1
                return self._record(
                    "register", node_id, via, "refused", "registry_refused"
                )
            self._registrations_accepted += 1
            if self._kills:
                self._registrations_after_kill += 1
            self._emit(
                "death_drill.registered",
                via,
                {"node_id": node_id, "via": via},
            )
            return self._record("register", node_id, via, "accepted", None)

    def sync(self, a: str, b: str) -> dict:
        """Sync two nodes' registry replicas both ways
        under the Y-053 rules — judged like every act:
        unknown, down, or self syncs refused by name;
        a refused sync between survivors after the
        kill is a disruption."""
        with self._lock:
            left = self._members.get(a)
            right = self._members.get(b)
            if left is None or right is None:
                return self._record("sync", a, b, "refused", "unknown_node")
            if a == b:
                return self._record("sync", a, b, "refused", "self_sync")
            if left.status != "up" or right.status != "up":
                return self._record("sync", a, b, "refused", "node_down")
            left.registry.sync_with(right.registry)
            self._syncs += 1
            if self._kills:
                self._syncs_after_kill += 1
            self._emit("death_drill.synced", a, {"node_id": a, "peer_id": b})
            return self._record("sync", a, b, "accepted", None)

    def entry(self, node_id: str, *, via: str) -> dict | None:
        """One node's registry entry as served whole by
        the survivor replica named by ``via`` — the
        post-kill lookup that proves the dead
        registrar's truth is still on the mesh's door.
        Counted as a lookup after the kill; asking a
        dead or unknown door raises by name."""
        with self._lock:
            replica = self._live(via).registry
            if self._kills:
                self._lookups_after_kill += 1
            return replica.entry(node_id)

    # -- heartbeats ------------------------------------------

    def heartbeat_round(self) -> dict:
        """Every up node beats once to every other up
        node's tracker. The dead send nothing and
        receive nothing — that silence, and only that
        silence, is what the survivors' trackers age.
        A round with no living pair is refused
        ``no_peers``."""
        with self._lock:
            up = [m for m in self._members.values() if m.status == "up"]
            if len(up) < 2:
                return self._record("heartbeat", None, None, "refused", "no_peers")
            sent = 0
            for sender in up:
                beat = _make_beat(sender.node_id, sender.beat_seq, self._now())
                sender.beat_seq += 1
                for receiver in up:
                    if receiver.node_id == sender.node_id:
                        continue
                    receiver.tracker.observe(copy.deepcopy(beat))
                    sent += 1
            self._heartbeats_sent += sent
            return self._record("heartbeat", None, None, "accepted", None)

    def sweep(self) -> None:
        """Age every live tracker on the drill's clock,
        recording each verdict transition on the
        trackers' own events (Y-054's rule)."""
        with self._lock:
            for member in self._members.values():
                if member.status == "up":
                    member.tracker.sweep()

    # -- the kill ----------------------------------------------

    def kill(self, node_id: str) -> dict:
        """Kill the first node ever started: its
        replica and tracker are dropped whole, and
        the registry union the mesh holds at that
        moment (victim included) is snapshotted so
        the report can count any of it that is later
        lost. Any other target is refused by name —
        this drill kills the registrar, no one else."""
        with self._lock:
            if self._first_node_id is None:
                return self._record("kill", node_id, None, "refused", "no_first_node")
            member = self._members.get(node_id)
            if member is None:
                return self._record("kill", node_id, None, "refused", "unknown_node")
            if node_id != self._first_node_id:
                return self._record("kill", node_id, None, "refused", "not_first_node")
            if member.status != "up":
                return self._record("kill", node_id, None, "refused", "already_dead")
            union: set[str] = set()
            for other in self._members.values():
                if other.status == "up":
                    union.update(e["node_id"] for e in other.registry.entries())
            self._entries_at_kill_ids = union
            member.entries_at_kill = len(member.registry.entries())
            member.registry = None
            member.tracker = None
            member.status = "down"
            self._kills += 1
            self._emit(
                "death_drill.node_killed",
                node_id,
                {"node_id": node_id, "entries_at_kill": len(union)},
            )
            return self._record("kill", node_id, None, "accepted", None)

    # -- the door ------------------------------------------------

    def _entries_lost(self) -> int:
        if not self._kills:
            return 0
        surviving: set[str] = set()
        for member in self._members.values():
            if member.status == "up":
                surviving.update(e["node_id"] for e in member.registry.entries())
        return len(self._entries_at_kill_ids - surviving)

    def report(self) -> dict:
        with self._lock:
            lost = self._entries_lost()
            effects = lost + self._disruptions
            survivors = [
                m
                for m in self._members.values()
                if m.status == "up" and m.node_id != self._first_node_id
            ]
            nodes = []
            for member in sorted(self._members.values(), key=lambda m: m.order):
                verdict = None
                missed = 0
                if (
                    member.status == "up"
                    and member.node_id != self._first_node_id
                    and self._first_node_id is not None
                ):
                    standing = member.tracker.liveness(self._first_node_id)
                    if standing is not None:
                        verdict = standing["verdict"]
                        missed = standing["missed_beats"]
                nodes.append(
                    {
                        "node_id": member.node_id,
                        "started_order": member.order,
                        "status": member.status,
                        "entries_held": (
                            len(member.registry.entries())
                            if member.status == "up"
                            else member.entries_at_kill
                        ),
                        "verdict_on_first": verdict,
                        "missed_beats_on_first": missed,
                    }
                )
            if not self._kills:
                verdict_word = "incomplete"
            elif effects > 0:
                verdict_word = "failed"
            elif (
                survivors
                and any(
                    m.tracker.liveness(self._first_node_id) is not None
                    for m in survivors
                )
                and all(
                    standing is None or standing["verdict"] == "gone"
                    for standing in (
                        m.tracker.liveness(self._first_node_id) for m in survivors
                    )
                )
                and self._registrations_after_kill >= 1
                and self._syncs_after_kill >= 1
                and self._joins_after_kill >= 1
            ):
                verdict_word = "passed"
            else:
                verdict_word = "incomplete"
            return validate_report(
                {
                    "version": 1,
                    "at": _fmt(self._now()),
                    "simulated": True,
                    "policy": copy.deepcopy(self._policy),
                    "first_node_id": self._first_node_id,
                    "nodes": nodes,
                    "nodes_started": len(self._members),
                    "kills": self._kills,
                    "heartbeats_sent": self._heartbeats_sent,
                    "registrations_accepted": self._registrations_accepted,
                    "registrations_after_kill": self._registrations_after_kill,
                    "syncs": self._syncs,
                    "syncs_after_kill": self._syncs_after_kill,
                    "joins_after_kill": self._joins_after_kill,
                    "lookups_after_kill": self._lookups_after_kill,
                    "entries_at_kill": len(self._entries_at_kill_ids),
                    "entries_lost": lost,
                    "disruptions": self._disruptions,
                    "effects_beyond_missing_heartbeat": effects,
                    "verdict": verdict_word,
                    "refusals_by_reason": dict(
                        sorted(self._refusals_by_reason.items())
                    ),
                    "records": copy.deepcopy(self._records),
                }
            )
