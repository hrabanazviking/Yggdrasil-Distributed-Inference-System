"""Gossip of state — Y-055.

Registry state (Y-053) and health state (Y-030 heartbeats,
judged by Y-054) do not spread by every node syncing with
every other node — that is a census, not an epidemic.
This slice is the epidemic carriage Y-049's benchmark
chose and Y-050's fabric models: **anti-entropy gossip**.
In each round every node exchanges state with a small,
fanout-limited set of peers — push-pull, both directions
in one exchange — and the state that spreads is applied
by the *receiving stores' own merge rules*, never by a
parallel logic invented here that could diverge from
them:

- registry entries and withdrawal tombstones travel by
  :meth:`RegistryService.sync_with`, the Y-053 pairwise
  merge itself (newer seq replaces whole, same-seq
  conflicts settle deterministically, a tombstone blocks
  resurrection by a stale registration wherever it
  arrives);
- heartbeats travel as validated ``node_health`` v1
  records and are fed to the receiving node's own
  :class:`LivenessTracker` (Y-054), whose sequence
  freshness rules judge them (a newer seq refreshes, a
  duplicate refreshes nothing, an older seq is stale).
  A node keeps the newest beat it has seen for every
  subject in a forwarding store, so a beat it learned
  second-hand is gossiped onward in later rounds; a
  duplicate or stale beat arriving again is counted
  apart and changes nothing — gossip may repeat itself
  forever without resurrecting a withdrawn entry or
  refreshing a dead peer.

Peer choice is deterministic, so runs replay exactly:
in round ``r`` a node talks to the ``fanout`` peers that
follow it in the sorted ring of node ids, the starting
point rotating by ``fanout`` places each round. The
fanout bound is per node per round and is counted — the
report's ``max_fanout_used`` is the largest number of
exchanges any one node initiated in any one round, and
it never exceeds the policy's fanout. A partition is
modelled in the open, as in the fabric (Y-050):
``cut(nodes)`` splits the named nodes away — exchanges
across the seam are refused by name
(``partitioned_peer``, counted and evented) and nothing
crosses — and ``heal()`` reunites the mesh, after which
ordinary rounds converge the two sides.

Convergence is a measured claim, not a hope. A mesh is
converged when every node holds the *same* registry
entries and tombstones, the *same* newest heartbeat for
every subject, and a liveness standing for every other
subject at that beat's sequence. :func:`measure_convergence`
runs that measurement at the roadmap's own sizes — 3,
10, and 30 simulated nodes, every node registering
itself and beating once at the origin moment — and
answers validated ``gossip_convergence`` v1 records:
rounds elapsed, virtual seconds elapsed (rounds × the
policy's round interval, on the measurement's own
virtual clock), exchanges and messages spent. The
numbers are the suite's to assert and the operator's
to read; they are reported, never rounded into a claim
about a real LAN.

Events are Y-013 envelopes from the ``fabric``
component — round completions, convergence, refused
exchanges — names and counts only, never payload
content (a beat's or an entry's contents never enter an
event, a report, or a measurement).

Honesty: this is an in-process epidemic model — every
record carries ``simulated: true`` as a schema
constant, no socket is opened, no LAN is crossed, and
gossip here rides direct method calls between the
stores, not the Y-050 fabric or Y-051 TLS (real
carriage is Y-062; hardware proof is Part VIII's).
What is proven is the *propagation rules*: epidemic
reach, fanout discipline, partition and heal,
duplicate/stale harmlessness under the receiving
stores' own rules, and convergence measured at three
mesh sizes. No node-agent seam: as with the registry
(Y-053), gossip is a mesh component's own act — the
agent's fabric words remain its Y-054 liveness seam
until real carriage at Y-062 gives it a wire. The CRDT
formalisation of these same stores is Y-056.
"""

import copy
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.health_endpoint import HealthError, build_node_health, validate_node_health
from yggdrasil.liveness import LivenessTracker
from yggdrasil.observability import make_event
from yggdrasil.registry_service import RegistryService

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

EPOCH = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)


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


_POLICY_VALIDATOR = _validator("gossip_policy.schema.json")
_REPORT_VALIDATOR = _validator("gossip_report.schema.json")
_CONVERGENCE_VALIDATOR = _validator("gossip_convergence.schema.json")


class GossipError(Exception):
    """The gossip mesh's own failure: a policy, report, or
    measurement that fails its schema, or a use of the API
    that would manufacture propagation — a node publishing
    another node's heartbeat as its origin word, a mesh
    past its policy bound, an exchange with oneself."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise GossipError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_policy(record: dict) -> dict:
    return _validate(_POLICY_VALIDATOR, record, "gossip policy")


def validate_report(record: dict) -> dict:
    return _validate(_REPORT_VALIDATOR, record, "gossip report")


def validate_convergence(record: dict) -> dict:
    return _validate(_CONVERGENCE_VALIDATOR, record, "gossip convergence")


def default_policy() -> dict:
    """Gossip as data: each node exchanges with at most 3
    peers per round, one round stands for 5 virtual
    seconds (the anti-entropy round Y-049's benchmark
    measured for the chosen gossip), and one mesh holds
    at most 256 nodes. A policy, labelled as one — an
    operator's numbers, not facts about a LAN."""
    return validate_policy(
        {
            "version": 1,
            "fanout": 3,
            "round_interval_seconds": 5,
            "max_nodes": 256,
        }
    )


class VirtualClock:
    """The measurement clock: a fixed epoch advancing only
    by hand, so convergence runs replay deterministically
    and every node's ageing is judged on the same virtual
    time. (Test and measurement clocks elsewhere in the
    tree follow the same shape.)"""

    def __init__(self, start: datetime = EPOCH):
        self.moment = start

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> None:
        self.moment += timedelta(seconds=seconds)


class GossipNode:
    """One node's gossip holding: its Y-053 registry
    replica, its Y-054 liveness tracker, and the newest
    heartbeat it has seen for every subject — the
    forwarding store that lets a beat learned second-hand
    be gossiped onward. Beats are applied through the
    tracker's own freshness rules; the store keeps only
    a strictly newer sequence for a subject, so a stale
    beat can never displace a fresher one it carries."""

    def __init__(self, *, node_id: str, now=None):
        if not isinstance(node_id, str) or not node_id:
            raise GossipError("a gossip node needs a node_id")
        self._node_id = node_id
        self._now = now or (lambda: datetime.now(UTC))
        self.registry = RegistryService(registry_node_id=node_id, now=self._now)
        self.liveness = LivenessTracker(observer_node_id=node_id, now=self._now)
        self._beats: dict[str, dict] = {}
        self._lock = threading.RLock()
        self.beats_stored = 0
        self.beats_duplicate = 0
        self.beats_stale = 0
        self.refused_reasons: dict[str, int] = {}

    @property
    def node_id(self) -> str:
        return self._node_id

    # -- registry words, delegated to the replica itself ------

    def register_local(self, census: dict, models: list) -> dict:
        return self.registry.register_local(census, models)

    def register(self, registration: dict) -> dict | None:
        return self.registry.register(registration)

    def withdraw(self, node_id: str, *, seq: int) -> bool:
        return self.registry.withdraw(node_id, seq=seq)

    # -- heartbeats ---------------------------------------------

    def _count_refusal(self, reason: str) -> None:
        self.refused_reasons[reason] = self.refused_reasons.get(reason, 0) + 1

    def publish_beat(self, beat: dict) -> bool:
        """This node's own heartbeat enters its forwarding
        store as the origin word. A node may only publish
        a beat naming itself — originating another node's
        health word would manufacture truth, and is refused
        by name. Returns True when the beat was new."""
        if not isinstance(beat, dict) or beat.get("node_id") != self._node_id:
            raise GossipError(
                f"node {self._node_id} cannot publish a heartbeat "
                f"for {beat.get('node_id') if isinstance(beat, dict) else None!r}"
            )
        return self.receive_beat(beat)

    def receive_beat(self, beat: dict) -> bool:
        """Hear one gossiped heartbeat. A strictly newer
        sequence for its subject is stored, forwarded
        onward in later rounds, and — for another node's
        beat — fed to this node's own liveness tracker,
        whose rules judge it. A duplicate refreshes
        nothing and a stale beat displaces nothing; both
        are counted apart. Returns True only when a newer
        beat was stored."""
        with self._lock:
            if not isinstance(beat, dict):
                self._count_refusal("malformed_beat")
                return False
            try:
                record = validate_node_health(copy.deepcopy(beat))
            except HealthError:
                self._count_refusal("malformed_beat")
                return False
            subject = record["node_id"]
            held = self._beats.get(subject)
            if held is not None:
                if record["seq"] == held["seq"]:
                    self.beats_duplicate += 1
                    return False
                if record["seq"] < held["seq"]:
                    self.beats_stale += 1
                    self._count_refusal("stale_sequence")
                    return False
            self._beats[subject] = record
            self.beats_stored += 1
            if subject != self._node_id:
                self.liveness.observe(record)
            return True

    def beats(self) -> dict:
        """The forwarding store: the newest beat held for
        every subject, as copies, keyed by subject."""
        with self._lock:
            return copy.deepcopy(self._beats)

    def beat_for(self, subject: str) -> dict | None:
        with self._lock:
            held = self._beats.get(subject)
            return copy.deepcopy(held) if held is not None else None


class GossipMesh:
    """A simulated mesh of gossip nodes under one policy
    and one (injectable) clock. ``now`` may be a
    :class:`VirtualClock` — rounds advance it by the
    policy's round interval, so liveness ageing and
    convergence time are measured on the same virtual
    time the gossip stands for. Events are Y-013
    envelopes from the ``fabric`` component, kept on
    ``events`` and also handed to ``event_sink`` when one
    is given. All state changes are lock-guarded."""

    def __init__(self, *, policy: dict | None = None, now=None, event_sink=None):
        self._policy = validate_policy(
            dict(policy) if policy is not None else default_policy()
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._event_sink = event_sink
        self._lock = threading.RLock()
        self._nodes: dict[str, GossipNode] = {}
        self._split: set[str] | None = None
        self._events: list[dict] = []
        self._event_seq = 0
        self._rounds_run = 0
        self._exchanges = 0
        self._messages = 0
        self._exchanges_refused = 0
        self._refused_reasons: dict[str, int] = {}
        self._max_fanout_used = 0
        self._measurements: list[dict] = []
        self._converged_announced = False

    # -- identity and events ---------------------------------

    @property
    def policy(self) -> dict:
        return copy.deepcopy(self._policy)

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    def _emit(self, type_: str, node_id: str, payload: dict) -> None:
        self._event_seq += 1
        event = make_event(
            type=type_,
            node_id=node_id,
            component="fabric",
            seq=self._event_seq,
            at=self._now(),
            payload=payload,
        )
        self._events.append(event)
        if self._event_sink is not None:
            self._event_sink(event)

    # -- membership and partition ------------------------------

    def add_node(self, node_id: str) -> GossipNode:
        with self._lock:
            if node_id in self._nodes:
                raise GossipError(f"node {node_id} is already in the mesh")
            if len(self._nodes) >= self._policy["max_nodes"]:
                raise GossipError(
                    f"the mesh is at the policy bound "
                    f"{self._policy['max_nodes']} nodes; {node_id} refused"
                )
            node = GossipNode(node_id=node_id, now=self._now)
            self._nodes[node_id] = node
            return node

    def node(self, node_id: str) -> GossipNode:
        try:
            return self._nodes[node_id]
        except KeyError:
            raise GossipError(f"node {node_id} is not in this mesh") from None

    def node_ids(self) -> list[str]:
        return sorted(self._nodes)

    def cut(self, nodes) -> None:
        """Split the named nodes away from the rest of the
        mesh, as in the fabric (Y-050). Each side keeps
        gossiping within itself; exchanges across the seam
        are refused by name until :meth:`heal`."""
        split = set(nodes)
        if not split:
            raise GossipError("a cut must name at least one node")
        unknown = split - set(self._nodes)
        if unknown:
            raise GossipError(f"cut names nodes not in the mesh: {sorted(unknown)}")
        with self._lock:
            self._split = split

    def heal(self) -> None:
        """Reunite a cut mesh. Ordinary rounds from here
        converge the two sides — the heal itself claims
        no convergence it has not measured."""
        with self._lock:
            self._split = None

    def _reachable(self, src: str, dst: str) -> bool:
        if self._split is None or src == dst:
            return True
        return (src in self._split) == (dst in self._split)

    # -- anti-entropy exchanges ----------------------------------

    def _targets(self, src: str, round_index: int) -> list[str]:
        """One node's peers for one round: the fanout peers
        following it in the sorted ring, the starting point
        rotating by fanout places each round — deterministic,
        so runs replay exactly, and bounded by the policy."""
        peers = [n for n in sorted(self._nodes) if n != src]
        if len(peers) <= self._policy["fanout"]:
            return peers
        position = sorted(self._nodes).index(src)
        start = (position + round_index * self._policy["fanout"]) % len(peers)
        return [
            peers[(start + k) % len(peers)] for k in range(self._policy["fanout"])
        ]

    def exchange(self, src_id: str, dst_id: str) -> bool:
        """One push-pull anti-entropy exchange between two
        nodes: each side's registry state is merged into
        the other by Y-053's own ``sync_with``, and each
        side's held heartbeats are offered to the other,
        judged by its own Y-054 tracker. Two messages are
        counted — one each way. Across a partition the
        exchange is refused by name, counted, and evented;
        nothing crosses the seam."""
        if src_id == dst_id:
            raise GossipError("a node cannot gossip with itself")
        src = self.node(src_id)
        dst = self.node(dst_id)
        with self._lock:
            if not self._reachable(src_id, dst_id):
                self._exchanges_refused += 1
                self._refused_reasons["partitioned_peer"] = (
                    self._refused_reasons.get("partitioned_peer", 0) + 1
                )
                self._emit(
                    "gossip.exchange_refused",
                    src_id,
                    {"peer_node_id": dst_id, "reason": "partitioned_peer"},
                )
                return False
            src_beats = src.beats()
            dst_beats = dst.beats()
            src.registry.sync_with(dst.registry)
            for subject in sorted(dst_beats):
                src.receive_beat(dst_beats[subject])
            for subject in sorted(src_beats):
                dst.receive_beat(src_beats[subject])
            self._exchanges += 1
            self._messages += 2
            return True

    def run_round(self) -> dict:
        """One epidemic round: every node, in sorted order,
        initiates exchanges with its fanout-limited peers
        for this round. The clock, when it can advance,
        advances by the policy's round interval at the
        round's end — a round stands for that much virtual
        time. Returns the round's own counts."""
        with self._lock:
            round_index = self._rounds_run
            exchanges_before = self._exchanges
            refused_before = self._exchanges_refused
            initiated: dict[str, int] = {}
            for src_id in sorted(self._nodes):
                count = 0
                for dst_id in self._targets(src_id, round_index):
                    if self.exchange(src_id, dst_id):
                        count += 1
                initiated[src_id] = count
                self._max_fanout_used = max(
                    self._max_fanout_used, count
                )
            advance = getattr(self._now, "advance", None)
            if callable(advance):
                advance(self._policy["round_interval_seconds"])
            self._rounds_run += 1
            converged = self.converged()
            summary = {
                "round": self._rounds_run,
                "exchanges": self._exchanges - exchanges_before,
                "exchanges_refused": self._exchanges_refused - refused_before,
                "max_initiated": max(initiated.values(), default=0),
                "converged": converged,
            }
            self._emit(
                "gossip.round_completed",
                min(self._nodes) if self._nodes else "gossip",
                {
                    "round": summary["round"],
                    "exchanges": summary["exchanges"],
                    "exchanges_refused": summary["exchanges_refused"],
                    "converged": converged,
                },
            )
            if converged and not self._converged_announced:
                self._converged_announced = True
                self._emit(
                    "gossip.converged",
                    min(self._nodes) if self._nodes else "gossip",
                    {"rounds": self._rounds_run, "nodes": len(self._nodes)},
                )
            return summary

    # -- convergence ----------------------------------------------

    def converged(self) -> bool:
        """The mesh's convergence test, in full: every node
        holds the same registry entries and tombstones, the
        same newest heartbeat for every subject, and a
        liveness standing for every other subject at that
        beat's sequence. An empty mesh is not converged —
        there is nothing to agree."""
        with self._lock:
            if not self._nodes:
                return False
            nodes = [self._nodes[k] for k in sorted(self._nodes)]
            first = nodes[0]
            entries = first.registry.entries()
            tombstones = first.registry.tombstones()
            beats = first.beats()
            for node in nodes[1:]:
                if node.registry.entries() != entries:
                    return False
                if node.registry.tombstones() != tombstones:
                    return False
                if node.beats() != beats:
                    return False
            for node in nodes:
                for subject, beat in beats.items():
                    if subject == node.node_id:
                        continue
                    record = node.liveness.liveness(subject)
                    if record is None or record["last_seq"] != beat["seq"]:
                        return False
            return True

    def run_until_converged(self, max_rounds: int = 100) -> bool:
        """Run rounds until the mesh converges or the round
        budget is spent. Returns the measured verdict —
        False is an honest answer under a partition, not
        an error."""
        for _ in range(max_rounds):
            if self.converged():
                return True
            self.run_round()
        return self.converged()

    # -- the counted state -----------------------------------------

    def record_measurement(self, measurement: dict) -> dict:
        """Keep one validated convergence measurement on
        this mesh's report — the numbers a measurement run
        (see :func:`measure_convergence`) actually took."""
        record = validate_convergence(copy.deepcopy(measurement))
        with self._lock:
            self._measurements.append(record)
        return copy.deepcopy(record)

    def report(self) -> dict:
        """This mesh's counted state as a validated
        ``gossip_report`` v1: every node's holdings and
        liveness counts, rounds and virtual time, exchanges
        and messages, refusals and why, the largest fanout
        used, duplicate and stale gossip counted apart,
        whether the mesh stands converged, and any
        measurements recorded — names and counts only."""
        with self._lock:
            summaries = []
            for node_id in sorted(self._nodes):
                node = self._nodes[node_id]
                peers = node.liveness.peers()
                summaries.append(
                    {
                        "node_id": node_id,
                        "entry_count": len(node.registry.entries()),
                        "tombstone_count": len(node.registry.tombstones()),
                        "beats_held": len(node.beats()),
                        "present_count": sum(
                            1 for r in peers if r["verdict"] == "present"
                        ),
                        "suspect_count": sum(
                            1 for r in peers if r["verdict"] == "suspect"
                        ),
                        "gone_count": sum(
                            1 for r in peers if r["verdict"] == "gone"
                        ),
                    }
                )
            refused = dict(self._refused_reasons)
            for node in self._nodes.values():
                for reason, count in node.refused_reasons.items():
                    refused[reason] = refused.get(reason, 0) + count
            record = {
                "version": 1,
                "at": _fmt(self._now()),
                "simulated": True,
                "policy": copy.deepcopy(self._policy),
                "node_ids": sorted(self._nodes),
                "nodes": summaries,
                "rounds_run": self._rounds_run,
                "elapsed_seconds": (
                    self._rounds_run * self._policy["round_interval_seconds"]
                ),
                "exchanges": self._exchanges,
                "messages_exchanged": self._messages,
                "exchanges_refused": self._exchanges_refused,
                "refused_reasons": dict(sorted(refused.items())),
                "max_fanout_used": self._max_fanout_used,
                "beats_stored": sum(
                    n.beats_stored for n in self._nodes.values()
                ),
                "beats_duplicate": sum(
                    n.beats_duplicate for n in self._nodes.values()
                ),
                "beats_stale": sum(n.beats_stale for n in self._nodes.values()),
                "converged": self.converged(),
                "measurements": copy.deepcopy(self._measurements),
            }
            return validate_report(record)


def _fixture_beat(node_id: str, clock: VirtualClock) -> dict:
    """One node's origin heartbeat for a measurement run,
    built through the real Y-030 builder — the same shape
    the liveness suite beats with."""
    health = {
        "node_id": node_id,
        "at": _fmt(clock()),
        "liveness": "present",
        "queue_depth": 0,
        "models_loaded": [],
    }
    return build_node_health(
        node_id=node_id,
        seq=0,
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


def measure_convergence(
    node_counts=(3, 10, 30),
    *,
    policy: dict | None = None,
    max_rounds: int = 100,
) -> list[dict]:
    """Measure convergence at each named mesh size, in the
    simulator: a fresh mesh of that many nodes, every node
    registering itself (its fixture census and one fixture
    model) and publishing its own heartbeat at the origin
    moment, then epidemic rounds until every node holds
    the same registry entries and the same health
    standings — or the round budget runs out, reported
    ``converged: false`` in the open. Each measurement is
    a validated ``gossip_convergence`` v1: rounds and
    virtual seconds elapsed, exchanges and messages
    spent, the entries and health subjects converged on.
    Fully SIMULATED, on a fresh virtual clock per size,
    so the whole measurement replays identically."""
    from yggdrasil.simulator import make_fake_census

    chosen = validate_policy(dict(policy) if policy is not None else default_policy())
    measurements = []
    for count in node_counts:
        clock = VirtualClock()
        mesh = GossipMesh(policy=chosen, now=clock)
        names = [f"node-{i + 1:02d}" for i in range(count)]
        for name in names:
            node = mesh.add_node(name)
            census = make_fake_census(name, models=("fixture-7b",))
            node.register_local(
                census,
                [
                    {
                        "name": "fixture-7b",
                        "family": "fixture",
                        "quants": [
                            {"name": "Q4_K_M", "format": "gguf", "size_gb": 4.0}
                        ],
                        "capabilities": ["generate", "stream"],
                        "runnable_on": [],
                    }
                ],
            )
            node.publish_beat(_fixture_beat(name, clock))
        rounds_before = mesh.report()["rounds_run"]
        exchanges_before = mesh.report()["exchanges"]
        messages_before = mesh.report()["messages_exchanged"]
        converged = mesh.run_until_converged(max_rounds=max_rounds)
        after = mesh.report()
        measurements.append(
            validate_convergence(
                {
                    "version": 1,
                    "node_count": count,
                    "simulated": True,
                    "converged": converged,
                    "rounds": after["rounds_run"] - rounds_before,
                    "elapsed_seconds": (
                        (after["rounds_run"] - rounds_before)
                        * chosen["round_interval_seconds"]
                    ),
                    "exchanges": after["exchanges"] - exchanges_before,
                    "messages_exchanged": (
                        after["messages_exchanged"] - messages_before
                    ),
                    "registry_entries_held": (
                        after["nodes"][0]["entry_count"] if after["nodes"] else 0
                    ),
                    "health_subjects": (
                        after["nodes"][0]["beats_held"] if after["nodes"] else 0
                    ),
                    "fanout": chosen["fanout"],
                    "round_interval_seconds": chosen["round_interval_seconds"],
                }
            )
        )
    return measurements


__all__ = [
    "EPOCH",
    "GossipError",
    "GossipMesh",
    "GossipNode",
    "VirtualClock",
    "default_policy",
    "measure_convergence",
    "validate_convergence",
    "validate_policy",
    "validate_report",
]
