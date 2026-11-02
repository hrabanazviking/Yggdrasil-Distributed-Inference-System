"""Heartbeats and liveness — Y-054.

A node reports *itself* present or gone in its Y-030
heartbeat; it never reports itself ``suspect``. Suspect is
a peer's verdict, and this slice builds the peer: a
:class:`LivenessTracker` held by one observing node, fed
the heartbeats (validated ``node_health`` v1 records,
Y-030) it hears from its peers, ageing each silent peer
**present → suspect → gone** against timings that are
data — a validated ``liveness_policy`` v1 (expected
heartbeat interval, the silence that makes a peer
suspect, the longer silence that makes it gone, and the
tracker's peer bound). The defaults (beat every 10 s,
suspect after 30 s, gone after 90 s) are a policy,
labelled as one — an operator's numbers, not facts about
any LAN — and the ordering interval < suspect < gone is
enforced by the tracker on top of the schema, because a
policy whose lines cross would age a peer gone before it
was ever suspect.

Ageing is measured on the **observer's own clock** —
the moment a beat was *heard here*, never the beat's own
timestamp (clock discipline is Y-068's subject, and no
node's temporal logic trusts a peer's clock). A beat's
sequence is its freshness proof, following the mesh's
one rule from discovery (Y-052) and the registry
(Y-053): only a sequence *newer* than the last accepted
one refreshes a peer. A replayed or duplicated beat
(sequence equal to the last accepted) is counted as a
duplicate and refreshes nothing — a recording of a dead
node's heartbeat, gossiped round the mesh forever, must
not keep the corpse ``present``; an older sequence is
refused ``stale_sequence`` by name. Every refusal is
named, counted in the report's ``refused_reasons``, and
recorded as a ``liveness.beat_refused`` event:
``malformed_beat``, ``own_heartbeat`` (a tracker judges
peers; the observer's own liveness is its health
endpoint's word, not this tracker's), ``suspect_self_report``
(a beat claiming ``suspect`` contradicts Y-030's rule and
is refused whole), ``stale_sequence``, and
``peer_table_full`` (the policy's bound — a new peer
heard while the table is full is refused by name, never
silently dropped and never evicting a tracked peer).

A peer whose own heartbeat reports ``gone`` (its agent
stopped or retired — a graceful goodbye, the Y-030
endpoint answering 503 with a full beat) is aged
``gone`` at once, on the basis ``self_reported_gone``,
without waiting out the silence lines; a later present
beat with a newer sequence is the peer's return, and it
ages back to ``present``. Verdicts are computed fresh on
every asking from the measured silence; ``sweep()``
walks every peer, and a verdict that has changed since
the last sweep (or since the beat that set it) is
counted as a transition and recorded as a
``liveness.verdict_changed`` event naming the peer, the
old and new verdicts, and the measured age — a peer's
falling silent is an event, not a silence.
``report()`` sweeps first, so the counts it publishes
are the verdicts as they stand at its own moment.

Each peer's standing is a validated ``liveness_record``
v1: the verdict and its basis, the last accepted beat's
sequence and own timestamp, when it was last heard on
this clock, the measured silence since (``age_seconds``),
the beats that silence has swallowed at the policy's
expected interval (``missed_beats`` — the count of whole
intervals elapsed since the last accepted beat), and the
policy lines the verdict was aged against, so a record
carries the numbers its verdict rests on.

Honesty: the tracker is an in-process judge — records
and reports carry ``simulated: true`` as a schema
constant, no socket is opened, no heartbeat crosses a
LAN inside this slice, and no peer is contacted. What
is proven is the *ageing rules*: freshness by sequence,
ageing by the observer's clock, the graceful-goodbye
and return paths, the counted refusals, and the
transition events. Epidemic gossip carrying heartbeats
between nodes is Y-055; real carriage of beats over the
fabric (Y-050) and TLS (Y-051) is Y-062; hardware proof
is Part VIII's. The node agent (Y-021) grows an optional
``liveness_tracker=`` seam in this slice: when one is
wired, ``observe_peer_heartbeat`` / ``peer_liveness`` /
``sweep_liveness`` delegate to it and ``status()``
carries its report; citizen factories wire none (which
peers a node watches is deployment wiring).
"""

import copy
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.health_endpoint import HealthError, validate_node_health
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


_POLICY_VALIDATOR = _validator("liveness_policy.schema.json")
_RECORD_VALIDATOR = _validator("liveness_record.schema.json")
_REPORT_VALIDATOR = _validator("liveness_report.schema.json")


class LivenessError(Exception):
    """The liveness tracker's own failure: a policy whose
    ageing lines cross, a record or report that fails its
    schema, or a use of the API that would manufacture a
    verdict — ageing a peer this tracker has never heard."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise LivenessError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_policy(record: dict) -> dict:
    """A liveness policy against its schema *and* its
    ordering law: the expected interval must fall before
    the suspect line, and the suspect line before the
    gone line, or no peer could ever age in order."""
    record = _validate(_POLICY_VALIDATOR, record, "liveness policy")
    if not (
        record["heartbeat_interval_seconds"]
        < record["suspect_after_seconds"]
        < record["gone_after_seconds"]
    ):
        raise LivenessError(
            "liveness policy lines cross: heartbeat_interval_seconds "
            f"({record['heartbeat_interval_seconds']}) < suspect_after_seconds "
            f"({record['suspect_after_seconds']}) < gone_after_seconds "
            f"({record['gone_after_seconds']}) is required"
        )
    return record


def validate_record(record: dict) -> dict:
    return _validate(_RECORD_VALIDATOR, record, "liveness record")


def validate_report(record: dict) -> dict:
    return _validate(_REPORT_VALIDATOR, record, "liveness report")


def default_policy() -> dict:
    """Liveness ageing as data: a peer is expected to beat
    every 10 seconds, is aged suspect after 30 seconds of
    silence, and gone after 90. A policy, labelled as one —
    an operator's numbers, not facts about a LAN."""
    return validate_policy(
        {
            "version": 1,
            "heartbeat_interval_seconds": 10,
            "suspect_after_seconds": 30,
            "gone_after_seconds": 90,
            "max_peers": 256,
        }
    )


class LivenessTracker:
    """One observing node's verdicts on its peers' liveness.

    ``observer_node_id`` names the node holding the
    tracker (the event source). ``now`` is injectable so
    ageing is testable and replayable; every age is
    measured on this clock, from the moment a beat was
    heard here. Events are Y-013 envelopes from the
    ``fabric`` component, kept on ``events`` and also
    handed to ``event_sink`` when one is given. All state
    changes are lock-guarded.
    """

    def __init__(
        self,
        *,
        observer_node_id: str,
        policy: dict | None = None,
        now=None,
        event_sink=None,
    ):
        if not isinstance(observer_node_id, str) or not observer_node_id:
            raise LivenessError("a liveness tracker needs an observer_node_id")
        self._observer = observer_node_id
        self._now = now or (lambda: datetime.now(UTC))
        self._policy = validate_policy(
            dict(policy) if policy is not None else default_policy()
        )
        self._event_sink = event_sink
        self._lock = threading.RLock()
        # node_id -> {last_seq, last_beat_at, last_seen (datetime),
        #             beats_accepted, self_gone, verdict}
        self._peers: dict[str, dict] = {}
        self._events: list[dict] = []
        self._event_seq = 0
        self._accepted = 0
        self._refused = 0
        self._duplicates = 0
        self._refused_reasons: dict[str, int] = {}
        self._transitions = 0

    # -- identity and events ---------------------------------

    @property
    def observer_node_id(self) -> str:
        return self._observer

    @property
    def policy(self) -> dict:
        return copy.deepcopy(self._policy)

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    def _emit(self, type_: str, payload: dict) -> None:
        self._event_seq += 1
        event = make_event(
            type=type_,
            node_id=self._observer,
            component="fabric",
            seq=self._event_seq,
            at=self._now(),
            payload=payload,
        )
        self._events.append(event)
        if self._event_sink is not None:
            self._event_sink(event)

    def _refuse(self, reason: str, peer_node_id) -> None:
        self._refused += 1
        self._refused_reasons[reason] = self._refused_reasons.get(reason, 0) + 1
        self._emit(
            "liveness.beat_refused",
            {"peer_node_id": peer_node_id, "reason": reason},
        )

    # -- the ageing judgement ----------------------------------

    def _judge(self, state: dict, moment: datetime) -> tuple[str, str, float]:
        """One peer's verdict at one moment, from the
        measured silence on this tracker's clock."""
        age = max(0.0, (moment - state["last_seen"]).total_seconds())
        if state["self_gone"]:
            return "gone", "self_reported_gone", age
        if age >= self._policy["gone_after_seconds"]:
            return "gone", "silence", age
        if age >= self._policy["suspect_after_seconds"]:
            return "suspect", "silence", age
        return "present", "fresh", age

    def _record(self, node_id: str, state: dict, moment: datetime) -> dict:
        verdict, basis, age = self._judge(state, moment)
        record = {
            "version": 1,
            "node_id": node_id,
            "verdict": verdict,
            "basis": basis,
            "last_seq": state["last_seq"],
            "last_beat_at": state["last_beat_at"],
            "last_seen_at": _fmt(state["last_seen"]),
            "age_seconds": round(age, 3),
            "missed_beats": int(age // self._policy["heartbeat_interval_seconds"]),
            "suspect_after_seconds": self._policy["suspect_after_seconds"],
            "gone_after_seconds": self._policy["gone_after_seconds"],
            "beats_accepted": state["beats_accepted"],
            "simulated": True,
        }
        return validate_record(record)

    def _set_verdict(self, node_id: str, state: dict, moment: datetime) -> dict:
        """Recompute one peer's verdict, recording the
        change as a counted transition + event when it
        differs from the verdict the tracker last held."""
        record = self._record(node_id, state, moment)
        previous = state["verdict"]
        if previous is not None and record["verdict"] != previous:
            self._transitions += 1
            self._emit(
                "liveness.verdict_changed",
                {
                    "peer_node_id": node_id,
                    "from_verdict": previous,
                    "to_verdict": record["verdict"],
                    "age_seconds": record["age_seconds"],
                },
            )
        state["verdict"] = record["verdict"]
        return record

    # -- hearing heartbeats --------------------------------------

    def observe(self, beat: dict) -> dict | None:
        """Hear one peer heartbeat (a validated Y-030
        ``node_health`` v1 record). Returns the peer's
        liveness record as it now stands, or ``None`` when
        the beat was refused — every refusal named and
        counted, nothing silently dropped, and a replayed
        beat never refreshing a silent peer."""
        with self._lock:
            heard_at = self._now()
            if not isinstance(beat, dict):
                return self._refuse("malformed_beat", None)
            peer_node_id = beat.get("node_id")
            try:
                record = validate_node_health(copy.deepcopy(beat))
            except HealthError:
                return self._refuse(
                    "malformed_beat",
                    peer_node_id if isinstance(peer_node_id, str) else None,
                )
            peer_node_id = record["node_id"]
            if peer_node_id == self._observer:
                return self._refuse("own_heartbeat", peer_node_id)
            if record["liveness"] == "suspect":
                return self._refuse("suspect_self_report", peer_node_id)
            state = self._peers.get(peer_node_id)
            if state is not None:
                if record["seq"] < state["last_seq"]:
                    return self._refuse("stale_sequence", peer_node_id)
                if record["seq"] == state["last_seq"]:
                    self._duplicates += 1
                    return self._record(peer_node_id, state, heard_at)
            elif len(self._peers) >= self._policy["max_peers"]:
                return self._refuse("peer_table_full", peer_node_id)
            if state is None:
                state = {
                    "last_seq": record["seq"],
                    "last_beat_at": record["at"],
                    "last_seen": heard_at,
                    "beats_accepted": 0,
                    "self_gone": False,
                    "verdict": None,
                }
                self._peers[peer_node_id] = state
            state["last_seq"] = record["seq"]
            state["last_beat_at"] = record["at"]
            state["last_seen"] = heard_at
            state["beats_accepted"] += 1
            state["self_gone"] = record["liveness"] == "gone"
            self._accepted += 1
            return self._set_verdict(peer_node_id, state, heard_at)

    # -- ageing ---------------------------------------------------

    def sweep(self) -> list[dict]:
        """Age every tracked peer against the present
        moment on this tracker's clock, recording each
        verdict change as a transition + event. Returns
        every peer's record, sorted by node id."""
        with self._lock:
            moment = self._now()
            return [
                self._set_verdict(node_id, self._peers[node_id], moment)
                for node_id in sorted(self._peers)
            ]

    def liveness(self, node_id: str) -> dict | None:
        """One named peer's verdict record as it stands at
        this moment — computed fresh, recording nothing —
        or ``None`` when this tracker has never heard the
        peer: named and unknown is an honest answer, never
        a manufactured verdict."""
        with self._lock:
            state = self._peers.get(node_id)
            if state is None:
                return None
            return self._record(node_id, state, self._now())

    def peers(self) -> list[dict]:
        """Every tracked peer's record at this moment,
        computed fresh and recording nothing, sorted by
        node id. (``sweep()`` is the call that records
        transitions.)"""
        with self._lock:
            moment = self._now()
            return [
                self._record(node_id, self._peers[node_id], moment)
                for node_id in sorted(self._peers)
            ]

    # -- the counted state ------------------------------------------

    def report(self) -> dict:
        """This tracker's counted state as a validated
        ``liveness_report`` v1 — sweeping first, so the
        verdicts and counts are those of this moment: the
        policy, every peer's record, the present / suspect
        / gone counts, every beat accepted, refused and
        why, duplicates counted apart, and the transitions
        recorded — names and counts only."""
        with self._lock:
            records = self.sweep()
            record = {
                "version": 1,
                "at": _fmt(self._now()),
                "observer_node_id": self._observer,
                "simulated": True,
                "policy": copy.deepcopy(self._policy),
                "peers": records,
                "present_count": sum(
                    1 for r in records if r["verdict"] == "present"
                ),
                "suspect_count": sum(
                    1 for r in records if r["verdict"] == "suspect"
                ),
                "gone_count": sum(1 for r in records if r["verdict"] == "gone"),
                "beats_accepted": self._accepted,
                "beats_refused": self._refused,
                "beats_duplicate": self._duplicates,
                "refused_reasons": dict(sorted(self._refused_reasons.items())),
                "transitions": self._transitions,
            }
            return validate_report(record)


__all__ = [
    "LivenessError",
    "LivenessTracker",
    "default_policy",
    "validate_policy",
    "validate_record",
    "validate_report",
]
