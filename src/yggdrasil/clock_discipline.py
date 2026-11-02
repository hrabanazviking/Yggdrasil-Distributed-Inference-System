"""Clock discipline — Y-068.

Y-054's liveness tracker already refuses to age a peer
on the peer's own timestamp: ageing is measured on the
observer's clock, because no node's temporal logic may
trust another node's clock. This slice builds the
other half of that discipline, kept by
:class:`ClockWatch`: nodes *report* their clock skew,
and temporal logic never trusts a single node's clock.

- **A skew is a measurement between two clocks, made
  by an observer.** An observation names an observer,
  a node, the time the node claimed, and the time on
  the observer's own clock when the claim was heard:
  ``skew = claimed - observed``. The watch computes
  the skew from those two reported times; it never
  takes a node's word for its own skew.
- **No single clock is trusted — the law is schema
  constants.** The ``clock_policy`` v1 fixes
  ``single_clock_trusted: false`` and
  ``consensus_method: "median"``. A node's standing
  is the *median* of its observers' votes (each
  observer contributes its latest sample only, so one
  loud observer cannot flood the vote, and one wild
  vote cannot drag a median the way it drags a mean),
  and no verdict on a node exists at all until at
  least ``min_observers`` distinct observers have
  reported on it — below that the node is
  ``uncorroborated``, however plausible its clock
  looks.
- **Standings are named.** A corroborated median
  within ``max_trusted_skew_seconds`` is ``trusted``;
  beyond it ``skewed``; beyond
  ``severe_skew_seconds`` ``severe``. The ordering
  max-trusted < severe is enforced by the watch on
  top of the schema, as the liveness policy's lines
  are enforced by its tracker.
- **Temporal logic comes to this door.** ``judge``
  is the only road by which a claimed timestamp from
  a node may enter temporal logic: an unknown node
  is refused ``unknown_node``, an uncorroborated one
  ``uncorroborated``, a severe one
  ``severely_skewed`` — all by name, all counted. A
  trusted or skewed node's timestamp is accepted
  only *corrected*: ``corrected_at`` is the claimed
  time minus the corroborated median skew, so what
  temporal logic uses is the mesh's consensus about
  the moment, never the node's own word for it.
- **Freshness is by sequence, per observer.** Each
  (observer, node) pair's samples carry a sequence;
  an older one is refused ``stale_sequence``, an
  equal one ``duplicate_sequence`` — a replayed clock
  report refreshes nothing, the same rule the
  registry, discovery, and liveness keep. A node
  observing itself is refused ``self_observation``;
  unknown doors, duplicate enrolments, a full watch,
  and malformed times are all refused by name and
  counted apart.
- **Real beats feed the watch without rewiring.** A
  validated Y-030 ``node_health`` beat carries its
  node's claimed time and sequence; ``observe_beat``
  reads exactly those fields into an observation
  judged by the same rules (a beat that fails its
  own contract is refused ``malformed_beat``).

The mesh consensus offset — the median of the
corroborated, non-severe nodes' median skews, or
``null`` when nothing is corroborated — is reported
as a measured standing, never used to silently
correct anyone: corrections happen per timestamp,
at the judge door, and are counted.

Honesty: everything here is SIMULATED —
``simulated: true`` is a schema constant on the
record and the report. Times are reported values
judged in-process; no socket is opened, no LAN is
crossed, no node's clock is read directly or set
(real carriage was Y-062's own proof). What is
proven is the *discipline*: corroboration before
trust, a median that resists outliers, correction
instead of belief, and named refusals. Part VI's
temporal projection consumes this door; it does not
exist yet, and this slice claims nothing for it.
"""

import copy
import json
import statistics
import threading
from datetime import UTC, datetime, timedelta
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


_POLICY_VALIDATOR = _validator("clock_policy.schema.json")
_RECORD_VALIDATOR = _validator("clock_record.schema.json")
_REPORT_VALIDATOR = _validator("clock_report.schema.json")


class ClockError(Exception):
    """The clock watch's own failure: a policy whose
    skew lines cross, a record or report that fails
    its schema, or a use of the API that would
    manufacture a standing — asking for an unknown
    node's standing. (Judged acts are refused by
    named record instead; see ``observe`` and
    ``judge``.)"""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value) -> datetime:
    """One reported time, as a datetime: accepts a
    datetime or an ISO-8601 string (a trailing ``Z``
    is UTC); a naive value is read as UTC. Anything
    else raises ValueError, which callers judge as a
    malformed sample — never a manufactured time."""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        moment = datetime.fromisoformat(text)
    else:
        raise TypeError(f"not a time: {type(value).__name__}")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _validate(validator, record, kind: str):
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise ClockError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_policy(record: dict) -> dict:
    """A clock policy against its schema *and* its
    ordering law: the trusted line must fall before
    the severe line, or a node could be severe before
    it was ever skewed."""
    record = _validate(_POLICY_VALIDATOR, record, "clock policy")
    if not record["max_trusted_skew_seconds"] < record["severe_skew_seconds"]:
        raise ClockError(
            "clock policy lines cross: max_trusted_skew_seconds "
            f"({record['max_trusted_skew_seconds']}) < severe_skew_seconds "
            f"({record['severe_skew_seconds']}) is required"
        )
    return record


def validate_record(record: dict) -> dict:
    return _validate(_RECORD_VALIDATOR, record, "clock record")


def validate_report(record: dict) -> dict:
    return _validate(_REPORT_VALIDATOR, record, "clock report")


def default_policy() -> dict:
    """Clock discipline as data: a node needs three
    corroborating observers, a corroborated median
    skew of at most 5 seconds is trusted, beyond 60
    seconds its timestamps are refused, and one watch
    holds at most 32 nodes. A policy, labelled as
    one — an operator's numbers, not facts about any
    clock."""
    return validate_policy(
        {
            "version": 1,
            "single_clock_trusted": False,
            "consensus_method": "median",
            "min_observers": 3,
            "max_trusted_skew_seconds": 5,
            "severe_skew_seconds": 60,
            "max_nodes": 32,
        }
    )


class ClockWatch:
    """The mesh's clock watch: enrolments, per-observer
    skew samples, per-node standings judged by median,
    and the judge door temporal logic must use.
    ``now`` is injectable so reports are testable; it
    stamps records and reports only — no standing is
    ever computed from it."""

    def __init__(self, policy: dict | None = None, *, now=None):
        self._policy = validate_policy(
            copy.deepcopy(policy) if policy is not None else default_policy()
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._nodes: dict[str, dict] = {}
        self._records: list[dict] = []
        self._refusals_by_reason: dict[str, int] = {}
        self._samples_accepted = 0
        self._judgements = 0
        self._judgements_corrected = 0
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
                component="fabric",
                seq=self._event_seq,
                at=self._now(),
                payload=payload,
            )
        )

    def _record(
        self, action, node_id, observer_id, verdict, reason,
        skew_seconds=None, corrected_at=None,
    ) -> dict:
        record = {
            "version": 1,
            "seq": len(self._records) + 1,
            "at": _fmt(self._now()),
            "action": action,
            "node_id": node_id,
            "observer_id": observer_id,
            "verdict": verdict,
            "reason": reason,
            "skew_seconds": skew_seconds,
            "corrected_at": corrected_at,
            "simulated": True,
        }
        checked = validate_record(record)
        self._records.append(checked)
        if verdict == "refused":
            self._refusals_by_reason[reason] = (
                self._refusals_by_reason.get(reason, 0) + 1
            )
            self._emit(
                "clock.act_refused",
                node_id or observer_id or "watch",
                {
                    "action": action,
                    "node_id": node_id or "unknown",
                    "reason": reason,
                },
            )
        return checked

    # -- standings ----------------------------------------

    def _standing(self, node_id: str) -> dict:
        state = self._nodes[node_id]
        votes = list(state["votes"].values())
        observers = len(votes)
        median = round(float(statistics.median(votes)), 3) if votes else None
        max_abs = round(max(abs(v) for v in votes), 3) if votes else None
        if not votes:
            verdict = "unknown"
        elif observers < self._policy["min_observers"]:
            verdict = "uncorroborated"
        elif abs(median) <= self._policy["max_trusted_skew_seconds"]:
            verdict = "trusted"
        elif abs(median) <= self._policy["severe_skew_seconds"]:
            verdict = "skewed"
        else:
            verdict = "severe"
        return {
            "node_id": node_id,
            "samples": state["samples"],
            "observers": observers,
            "median_skew_seconds": median,
            "max_abs_skew_seconds": max_abs,
            "verdict": verdict,
            "trusted": verdict == "trusted",
        }

    def standing(self, node_id: str) -> dict:
        """One enrolled node's standing as it stands
        now. An unknown node raises by name — named
        and unknown is an honest answer, never a
        manufactured verdict."""
        with self._lock:
            if node_id not in self._nodes:
                raise ClockError(f"unknown_node: node {node_id} was never enrolled")
            return self._standing(node_id)

    # -- enrolment ------------------------------------------

    def enrol(self, node_id: str) -> dict:
        """Enrol one node with the watch. A duplicate
        enrolment, or one past the policy's bound, is
        refused by name."""
        with self._lock:
            if not isinstance(node_id, str) or not node_id:
                return self._record(
                    "enrol", None, None, "refused", "malformed_sample"
                )
            if node_id in self._nodes:
                return self._record(
                    "enrol", node_id, None, "refused", "duplicate_node"
                )
            if len(self._nodes) >= self._policy["max_nodes"]:
                return self._record("enrol", node_id, None, "refused", "mesh_full")
            self._nodes[node_id] = {"votes": {}, "last_seq": {}, "samples": 0}
            self._emit("clock.node_enrolled", node_id, {"node_id": node_id})
            return self._record("enrol", node_id, None, "accepted", None)

    # -- observations -----------------------------------------

    def observe(self, observer_id, node_id, claimed_at, observed_at, seq) -> dict:
        """Hear one observer's measurement of one node's
        clock: the node claimed ``claimed_at``; the
        observer's own clock read ``observed_at`` when
        it heard the claim. The skew is computed here,
        never taken on anyone's word. Judged by name:
        unknown doors, a self-observation, a stale or
        duplicate sequence, and malformed times are
        refused and counted apart."""
        return self._observe(
            "observe", observer_id, node_id, claimed_at, observed_at, seq
        )

    def _observe(self, action, observer_id, node_id, claimed_at, observed_at, seq):
        with self._lock:
            if node_id not in self._nodes:
                return self._record(
                    action, node_id if isinstance(node_id, str) else None,
                    observer_id if isinstance(observer_id, str) else None,
                    "refused", "unknown_node",
                )
            if observer_id not in self._nodes:
                return self._record(
                    action, node_id,
                    observer_id if isinstance(observer_id, str) else None,
                    "refused", "unknown_observer",
                )
            if observer_id == node_id:
                return self._record(
                    action, node_id, observer_id, "refused", "self_observation"
                )
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
                return self._record(
                    action, node_id, observer_id, "refused", "malformed_sample"
                )
            state = self._nodes[node_id]
            last = state["last_seq"].get(observer_id)
            if last is not None and seq < last:
                return self._record(
                    action, node_id, observer_id, "refused", "stale_sequence"
                )
            if last is not None and seq == last:
                return self._record(
                    action, node_id, observer_id, "refused", "duplicate_sequence"
                )
            try:
                claimed = _parse(claimed_at)
                observed = _parse(observed_at)
            except (ValueError, TypeError):
                return self._record(
                    action, node_id, observer_id, "refused", "malformed_sample"
                )
            skew = round((claimed - observed).total_seconds(), 3)
            state["last_seq"][observer_id] = seq
            state["votes"][observer_id] = skew
            state["samples"] += 1
            self._samples_accepted += 1
            self._emit(
                "clock.sample_recorded",
                node_id,
                {"node_id": node_id, "observer_id": observer_id,
                 "skew_seconds": skew},
            )
            return self._record(
                action, node_id, observer_id, "accepted", None, skew_seconds=skew
            )

    def observe_beat(self, observer_id, beat, observed_at) -> dict:
        """Hear one node's clock through its validated
        Y-030 heartbeat: the beat's own ``at`` is the
        node's claimed time and its ``seq`` the sample
        sequence — read, not rewired. A beat that
        fails its own contract is refused
        ``malformed_beat``."""
        with self._lock:
            if not isinstance(beat, dict):
                return self._record(
                    "observe_beat", None,
                    observer_id if isinstance(observer_id, str) else None,
                    "refused", "malformed_beat",
                )
            try:
                checked = validate_node_health(copy.deepcopy(beat))
            except HealthError:
                return self._record(
                    "observe_beat",
                    beat.get("node_id") if isinstance(beat.get("node_id"), str)
                    else None,
                    observer_id if isinstance(observer_id, str) else None,
                    "refused", "malformed_beat",
                )
            return self._observe(
                "observe_beat", observer_id, checked["node_id"],
                checked["at"], observed_at, checked["seq"],
            )

    # -- the judge door -----------------------------------------

    def judge(self, node_id, claimed_at) -> dict:
        """Judge one claimed timestamp from one node —
        the only road by which it may enter temporal
        logic. Accepted only corrected by the node's
        corroborated median skew (the correction rides
        the record as ``corrected_at``); refused by
        name when the node is unknown, uncorroborated,
        or severe, or the time is malformed."""
        with self._lock:
            if node_id not in self._nodes:
                return self._record(
                    "judge", node_id if isinstance(node_id, str) else None,
                    None, "refused", "unknown_node",
                )
            try:
                claimed = _parse(claimed_at)
            except (ValueError, TypeError):
                return self._record(
                    "judge", node_id, None, "refused", "malformed_sample"
                )
            standing = self._standing(node_id)
            if standing["verdict"] in ("unknown", "uncorroborated"):
                return self._record(
                    "judge", node_id, None, "refused", "uncorroborated"
                )
            if standing["verdict"] == "severe":
                return self._record(
                    "judge", node_id, None, "refused", "severely_skewed"
                )
            median = standing["median_skew_seconds"]
            corrected = claimed - timedelta(seconds=median)
            self._judgements += 1
            if median != 0:
                self._judgements_corrected += 1
            self._emit(
                "clock.timestamp_judged",
                node_id,
                {"node_id": node_id, "median_skew_seconds": median},
            )
            return self._record(
                "judge", node_id, None, "accepted", None,
                skew_seconds=median, corrected_at=_fmt(corrected),
            )

    # -- the door -------------------------------------------------

    def report(self) -> dict:
        with self._lock:
            nodes = [self._standing(node_id) for node_id in sorted(self._nodes)]
            corroborated = [
                n["median_skew_seconds"] for n in nodes
                if n["verdict"] in ("trusted", "skewed")
            ]
            consensus = (
                round(float(statistics.median(corroborated)), 3)
                if corroborated else None
            )
            return validate_report(
                {
                    "version": 1,
                    "at": _fmt(self._now()),
                    "simulated": True,
                    "policy": copy.deepcopy(self._policy),
                    "nodes": nodes,
                    "nodes_enrolled": len(nodes),
                    "samples_accepted": self._samples_accepted,
                    "judgements": self._judgements,
                    "judgements_corrected": self._judgements_corrected,
                    "consensus_offset_seconds": consensus,
                    "trusted_nodes": sum(
                        1 for n in nodes if n["verdict"] == "trusted"
                    ),
                    "skewed_nodes": sum(
                        1 for n in nodes if n["verdict"] == "skewed"
                    ),
                    "severe_nodes": sum(
                        1 for n in nodes if n["verdict"] == "severe"
                    ),
                    "uncorroborated_nodes": sum(
                        1 for n in nodes
                        if n["verdict"] in ("unknown", "uncorroborated")
                    ),
                    "refusals_by_reason": dict(
                        sorted(self._refusals_by_reason.items())
                    ),
                    "records": copy.deepcopy(self._records),
                }
            )


__all__ = [
    "ClockError",
    "ClockWatch",
    "default_policy",
    "validate_policy",
    "validate_record",
    "validate_report",
]
