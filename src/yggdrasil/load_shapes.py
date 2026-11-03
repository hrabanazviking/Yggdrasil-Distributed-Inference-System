"""Load shapes — Y-072.

Every Part III slice before this one counts what it
does; Y-064 puts those counts on the operator's
door. What no slice had yet published is the mesh's
own *running cost*: what heartbeats and gossip —
the traffic the mesh spends on itself, carrying no
one's task — cost as the mesh grows. This slice
measures that cost and publishes it as data,
kept by :class:`LoadShapes`:

- **The plan is data.** A validated
  ``load_shapes_policy`` v1 names the mesh sizes to
  measure (the roadmap's own gossip sizes by
  default: 3, 10, 30), the steady-state window in
  virtual seconds, the per-convergence round
  budget, and the Y-055 gossip + Y-054 liveness
  policies the measurement runs under. Two laws
  ride on top of the schema, enforced by the
  measurer: the window must divide evenly by the
  heartbeat interval (a window that ends mid-beat
  would publish a fraction of a beat as a whole
  cost), and the gossip policy must be able to
  hold the largest planned size.
- **The counts come from the components.** The
  policy's law is schema constants:
  ``counts_come_from_components`` always true,
  ``hardware_claim_allowed`` always false. A
  measurement builds a *fresh* Y-055
  :class:`GossipMesh` of real components — every
  node registering its fixture census through
  Y-053's registry and beating through Y-030's
  heartbeat builder — and reads every number off
  the mesh's own report: exchanges, messages,
  rounds, beats stored / duplicate / stale,
  refused exchanges. The measurer invents no
  count; the only arithmetic it performs is the
  two published rates (messages per node, and
  messages per node per virtual second over the
  *measured* elapsed time), each named as
  computed in the shape that carries it.
- **Two phases, counted apart.** The *join* is a
  fresh mesh run to convergence — what it costs
  a mesh of this size to become one mesh. The
  *window* is steady state: at each heartbeat
  interval every node publishes a fresh beat
  (sequence rising, so Y-054/Y-055 freshness
  judges it a new word, never a duplicate), and
  each beat-round is gossiped to convergence
  before the next — what it costs the mesh to
  *stay* one mesh while its heart beats. A phase
  that spends its round budget unconverged is
  published ``converged: false`` with its counts
  standing as counted — a failed convergence is
  a measurement, never an error dressed as one.
- **Sizes are measured, not asserted.** The
  shape's byte numbers are canonical-JSON
  serialisations of the real records the mesh
  holds at the window's end: one heartbeat
  (``heartbeat_bytes``), one registry entry
  (``registry_entry_bytes``), and one node's
  whole state offer — its entries, tombstones,
  and held beats (``state_offer_bytes``), the
  payload one anti-entropy exchange direction
  offers. In-process gossip moves objects, not
  bytes; these are the sizes of the objects
  whose movement the counts describe, labelled
  as exactly that.
- **The door judges its acts.** Measuring a size
  is an act judged by named record: a size
  outside the plan is refused ``size_not_planned``,
  a size measured twice on one door
  ``duplicate_size`` (the published table never
  quietly holds two shapes for one size), and a
  size that is not a positive count
  ``malformed_size`` — each refused act counted
  apart, measuring nothing.

Honesty: everything here is SIMULATED —
``simulated: true`` is a schema constant on the
shape, the record, and the report. The meshes
are in-process simulator citizens on a virtual
clock (Y-014/Y-055); no socket is opened, no LAN
is crossed, and the published numbers are the
simulator's counted cost of the mesh's own
rules — evidence about the *protocol's* shape,
never about any device's network. Real carriage
was proven at Y-062; what the same shapes cost
on real hardware is Part VIII's road, and the
policy's ``hardware_claim_allowed: false``
constant exists so no reader can mistake this
table for it.
"""

import copy
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil import gossip as _gossip
from yggdrasil import liveness as _liveness
from yggdrasil.gossip import GossipMesh, VirtualClock
from yggdrasil.health_endpoint import build_node_health
from yggdrasil.observability import make_event
from yggdrasil.simulator import make_fake_census

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


_POLICY_VALIDATOR = _validator("load_shapes_policy.schema.json")
_SHAPE_VALIDATOR = _validator("load_shape.schema.json")
_RECORD_VALIDATOR = _validator("load_shapes_record.schema.json")
_REPORT_VALIDATOR = _validator("load_shapes_report.schema.json")

_FIXTURE_MODEL = {
    "name": "fixture-7b",
    "family": "fixture",
    "quants": [{"name": "Q4_K_M", "format": "gguf", "size_gb": 4.0}],
    "capabilities": ["generate", "stream"],
    "runnable_on": [],
}


class LoadShapesError(Exception):
    """The measurer's own failure: a policy, shape,
    record, or report that fails its schema, or a
    plan whose own numbers cross — a window that
    does not divide by the heartbeat interval, or a
    planned size the gossip policy cannot hold.
    (Judged measurement acts are refused by named
    record instead; see ``measure``.)"""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record, kind: str):
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise LoadShapesError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_policy(record: dict) -> dict:
    """A measurement plan against its schema *and*
    its own crossing laws: the embedded gossip and
    liveness policies must pass their own
    validators (the liveness ordering law among
    them), the window must divide evenly by the
    heartbeat interval, and the gossip policy must
    be able to hold the largest planned size."""
    record = _validate(_POLICY_VALIDATOR, record, "load shapes policy")
    try:
        _gossip.validate_policy(copy.deepcopy(record["gossip"]))
        _liveness.validate_policy(copy.deepcopy(record["liveness"]))
    # An embedded policy failing its own law is this
    # plan's defect, named as one.
    except Exception as exc:
        raise LoadShapesError(
            f"load shapes policy embeds an unlawful policy: {exc}"
        ) from exc
    interval = record["liveness"]["heartbeat_interval_seconds"]
    if record["window_seconds"] % interval != 0:
        raise LoadShapesError(
            "load shapes window does not divide by the heartbeat "
            f"interval: window_seconds ({record['window_seconds']}) % "
            f"heartbeat_interval_seconds ({interval}) != 0"
        )
    largest = max(record["node_counts"])
    if largest > record["gossip"]["max_nodes"]:
        raise LoadShapesError(
            f"load shapes plan names {largest} nodes but the gossip "
            f"policy holds at most {record['gossip']['max_nodes']}"
        )
    return record


def validate_shape(record: dict) -> dict:
    return _validate(_SHAPE_VALIDATOR, record, "load shape")


def validate_record(record: dict) -> dict:
    return _validate(_RECORD_VALIDATOR, record, "load shapes record")


def validate_report(record: dict) -> dict:
    return _validate(_REPORT_VALIDATOR, record, "load shapes report")


def default_policy() -> dict:
    """The published plan: the roadmap's own gossip
    sizes (3, 10, 30 citizens), a 60-virtual-second
    steady window (six heartbeats at Y-054's 10 s
    interval), Y-055's round budget of 100, and the
    two component policies at their own defaults —
    fanout 3, one round standing for 5 virtual
    seconds. A policy, labelled as one: an
    operator's plan, not a fact about a LAN."""
    return validate_policy(
        {
            "version": 1,
            "counts_come_from_components": True,
            "hardware_claim_allowed": False,
            "node_counts": [3, 10, 30],
            "window_seconds": 60,
            "max_rounds": 100,
            "gossip": _gossip.default_policy(),
            "liveness": _liveness.default_policy(),
        }
    )


def _canonical_bytes(record) -> int:
    """The size of one real record as canonical
    JSON — the serialisation the fabric's own
    digest (Y-050) is taken over."""
    return len(
        json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _fixture_beat(node_id: str, seq: int, clock: VirtualClock) -> dict:
    """One node's heartbeat at one sequence, built
    through the real Y-030 builder — the same
    fixture shape Y-055's convergence measurement
    beats with, its sequence the caller's."""
    health = {
        "node_id": node_id,
        "at": _fmt(clock()),
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


class LoadShapes:
    """The load-shape measurer: one door holding
    one plan, the shapes it has measured, and the
    record of every act. ``now`` is injectable so
    records, reports, and events are testable; it
    stamps the door's own bookkeeping only — every
    measurement runs on its own fresh virtual
    clock, so shapes replay identically regardless
    of the door's clock."""

    def __init__(self, policy: dict | None = None, *, now=None):
        self._policy = validate_policy(
            copy.deepcopy(policy) if policy is not None else default_policy()
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._shapes: dict[int, dict] = {}
        self._records: list[dict] = []
        self._refusals_by_reason: dict[str, int] = {}
        self._events: list[dict] = []
        self._event_seq = 0

    @property
    def policy(self) -> dict:
        return copy.deepcopy(self._policy)

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    def _emit(self, type_: str, payload: dict) -> None:
        self._event_seq += 1
        self._events.append(
            make_event(
                type=type_,
                node_id="load-shapes",
                component="fabric",
                seq=self._event_seq,
                at=self._now(),
                payload=payload,
            )
        )

    def _record(self, node_count, verdict, reason, total_messages) -> dict:
        record = {
            "version": 1,
            "seq": len(self._records) + 1,
            "at": _fmt(self._now()),
            "action": "measure",
            "node_count": node_count,
            "verdict": verdict,
            "reason": reason,
            "total_messages": total_messages,
            "simulated": True,
        }
        checked = validate_record(record)
        self._records.append(checked)
        if verdict == "refused":
            self._refusals_by_reason[reason] = (
                self._refusals_by_reason.get(reason, 0) + 1
            )
            self._emit(
                "load_shapes.measure_refused",
                {"node_count": node_count, "reason": reason},
            )
        return checked

    # -- the measurement ------------------------------------

    def _run_measurement(self, node_count: int) -> dict:
        """One size, measured end to end on a fresh
        mesh of the real components. Every count is
        read off the mesh's own reports; the join
        snapshot is taken before the window opens,
        so the window's deltas are its own."""
        gossip_policy = copy.deepcopy(self._policy["gossip"])
        interval = self._policy["liveness"]["heartbeat_interval_seconds"]
        round_seconds = gossip_policy["round_interval_seconds"]
        max_rounds = self._policy["max_rounds"]
        clock = VirtualClock()
        mesh = GossipMesh(policy=gossip_policy, now=clock)
        names = [f"node-{i + 1:02d}" for i in range(node_count)]
        for name in names:
            node = mesh.add_node(name)
            census = make_fake_census(name, models=("fixture-7b",))
            node.register_local(census, [copy.deepcopy(_FIXTURE_MODEL)])
            node.publish_beat(_fixture_beat(name, 0, clock))
        join_ok = mesh.run_until_converged(max_rounds=max_rounds)
        join = mesh.report()
        steps = self._policy["window_seconds"] // interval
        beats_published = 0
        window_ok = True
        for step in range(1, steps + 1):
            for name in names:
                mesh.node(name).publish_beat(_fixture_beat(name, step, clock))
                beats_published += 1
            if not mesh.run_until_converged(max_rounds=max_rounds):
                window_ok = False
        after = mesh.report()
        window_rounds = after["rounds_run"] - join["rounds_run"]
        total_messages = after["messages_exchanged"]
        total_exchanges = after["exchanges"]
        elapsed = after["elapsed_seconds"]
        first = mesh.node(names[0])
        held_beats = first.beats()
        entries = first.registry.entries()
        own_entry = next(
            (entry for entry in entries if entry["node_id"] == names[0]),
            entries[0] if entries else {},
        )
        shape = {
            "version": 1,
            "node_count": node_count,
            "simulated": True,
            "converged": bool(join_ok and window_ok),
            "join_rounds": join["rounds_run"],
            "join_elapsed_seconds": join["elapsed_seconds"],
            "join_exchanges": join["exchanges"],
            "join_messages": join["messages_exchanged"],
            "window_seconds": self._policy["window_seconds"],
            "window_beats_published": beats_published,
            "window_rounds": window_rounds,
            "window_elapsed_seconds": window_rounds * round_seconds,
            "window_exchanges": total_exchanges - join["exchanges"],
            "window_messages": total_messages - join["messages_exchanged"],
            "total_exchanges": total_exchanges,
            "total_messages": total_messages,
            "beats_stored": after["beats_stored"],
            "beats_duplicate": after["beats_duplicate"],
            "beats_stale": after["beats_stale"],
            "exchanges_refused": after["exchanges_refused"],
            "heartbeat_bytes": (
                _canonical_bytes(held_beats[names[0]]) if names[0] in held_beats else 0
            ),
            "registry_entry_bytes": _canonical_bytes(own_entry) if entries else 0,
            "state_offer_bytes": _canonical_bytes(
                {
                    "entries": entries,
                    "tombstones": first.registry.tombstones(),
                    "beats": held_beats,
                }
            ),
            "messages_per_node": round(total_messages / node_count, 6),
            "messages_per_node_per_second": (
                round(total_messages / node_count / elapsed, 6) if elapsed else 0.0
            ),
        }
        return validate_shape(shape)

    def measure(self, node_count) -> dict:
        """Measure one planned size — the door's one
        act, judged by named record. A size that is
        not a positive count is refused
        ``malformed_size``, a size outside the plan
        ``size_not_planned``, and a size already
        measured on this door ``duplicate_size``;
        each refusal is counted apart and measures
        nothing. An accepted measurement stands in
        the report as that size's one shape, and
        its total message count stands in the
        record. (A measurement holds the door's
        lock for its whole run: this is an
        instrument, not a service.)"""
        with self._lock:
            if (
                not isinstance(node_count, int)
                or isinstance(node_count, bool)
                or node_count < 1
            ):
                recorded = (
                    node_count
                    if isinstance(node_count, int) and not isinstance(node_count, bool)
                    else None
                )
                return self._record(recorded, "refused", "malformed_size", None)
            if node_count not in self._policy["node_counts"]:
                return self._record(
                    node_count, "refused", "size_not_planned", None
                )
            if node_count in self._shapes:
                return self._record(
                    node_count, "refused", "duplicate_size", None
                )
            shape = self._run_measurement(node_count)
            self._shapes[node_count] = shape
            self._emit(
                "load_shapes.measured",
                {
                    "node_count": node_count,
                    "total_messages": shape["total_messages"],
                    "converged": shape["converged"],
                },
            )
            return self._record(
                node_count, "accepted", None, shape["total_messages"]
            )

    def measure_all(self) -> list[dict]:
        """Measure every size in the plan, in plan
        order — the road that publishes the whole
        table. Returns each size's record."""
        with self._lock:
            return [self.measure(count) for count in self._policy["node_counts"]]

    # -- the published table ----------------------------------

    def report(self) -> dict:
        """The published load shapes as a validated
        ``load_shapes_report`` v1: the plan, one
        shape per measured size in plan order, the
        totals across measured sizes, every refusal
        counted apart by name, and every act's
        record."""
        with self._lock:
            shapes = [
                copy.deepcopy(self._shapes[count])
                for count in self._policy["node_counts"]
                if count in self._shapes
            ]
            return validate_report(
                {
                    "version": 1,
                    "at": _fmt(self._now()),
                    "simulated": True,
                    "policy": copy.deepcopy(self._policy),
                    "shapes": shapes,
                    "sizes_measured": len(shapes),
                    "total_messages": sum(s["total_messages"] for s in shapes),
                    "total_exchanges": sum(s["total_exchanges"] for s in shapes),
                    "refusals_by_reason": dict(
                        sorted(self._refusals_by_reason.items())
                    ),
                    "records": copy.deepcopy(self._records),
                }
            )


def measure_load_shapes(policy: dict | None = None, *, now=None) -> dict:
    """The published table in one call: a fresh door
    under the given (or default) plan measures every
    planned size and returns its validated report."""
    door = LoadShapes(policy=policy, now=now)
    door.measure_all()
    return door.report()


__all__ = [
    "LoadShapes",
    "LoadShapesError",
    "default_policy",
    "measure_load_shapes",
    "validate_policy",
    "validate_record",
    "validate_report",
    "validate_shape",
]
