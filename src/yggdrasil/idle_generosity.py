"""Idle generosity — Y-041.

An idle node advertises spare capacity; a busy one
withdraws it — the present-tense truth Verðandi routes on
(Part IV). An advertisement that outlives the idleness it
described is a lie the router would place work on, so this
slice's rule is strict: generosity is *evaluated fresh*
from measured load every time it is asked for, it is never
cached as a standing promise, and every withdrawal names
its reasons in the data.

- **Policy as data** (``generosity_policy`` v1): how idle is
  idle is the operator's call, not a fact about a machine —
  the most tasks that may be in flight (default 0), the
  deepest the local queue may be (default 0), the least
  spare-memory fraction that must remain after the node's
  present load (default 0.25), and whether a shedding
  governor or a draining lifecycle withdraws the
  advertisement (both default true). ``default_policy()``
  is labelled a policy, never a measurement.
- **The advertisement** (``spare_capacity`` v1) carries the
  verdict (``generous`` / ``withdrawn``), every reason a
  withdrawal happened — ``not_joined``, ``draining``,
  ``in_flight``, ``queue_held``, ``governor_shedding``,
  ``memory_tight``, ``memory_unmeasured`` — the load it was
  judged from (in-flight count, queue depth and free slots,
  free model slots), the spare it can honestly offer
  (memory bytes and fraction, CPU share — each ``null``
  when unmeasured, never invented), and the capacity the
  spare rides on: the accelerator kinds the node's latest
  census actually measured (a degraded node's reduced
  truth, Y-040 — never its baseline) and the models
  presently loaded.
- **Memory is judged, not assumed.** A node whose memory
  has not been measured cannot prove it has spare, so it
  withdraws with ``memory_unmeasured`` rather than
  advertising a guess. An unknown CPU reading blocks
  nothing: its spare share is simply reported ``null``.
- **Transitions are history.** The first evaluation and
  every flip are recorded in the ``generosity_report`` v1
  (from/to status, reasons, the moment) and emitted as
  Y-013 EventEnvelopes from the ``node_agent`` component —
  ``generosity.advertised`` / ``generosity.withdrawn`` —
  counts and reason names only, no payload content.
  ``generous_since`` marks when the current generous spell
  began and clears the moment it ends.

The node agent (wired at this slice) grows the seam:
``idle_generosity=`` at construction, a
``generosity_advertisement()`` that builds the load from
the agent's own present facts — its lifecycle state, its
in-flight leases, the local queue's report (Y-031), the
governor's verdict and readings (Y-028), the lifecycle's
slot budgets (Y-027), and the latest census — a
``generosity_report()``, and ``status()`` carrying the
last advertisement. Without one wired, the agent
advertises nothing, exactly as before. All four citizen
factories wire one.

Honesty: this slice advertises to *no one* — there is no
fabric yet (Part III), so an advertisement is a validated
local record the fabric will carry later, and no peer has
seen one. The load figures are whatever the wired
components measured; the acceptance tests inject fixture
loads and say so. Nothing here places, accepts, or routes
a task: accepting offered work is the router's country
(Part IV), and this slice's spare is an offer of capacity,
never a promise that any particular task will fit — the
governor (Y-028) still admits or refuses each task on its
own declared footprint when it arrives.
"""

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.observability import make_event

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

GENEROUS = "generous"
WITHDRAWN = "withdrawn"
UNEVALUATED = "unevaluated"

REASON_NOT_JOINED = "not_joined"
REASON_DRAINING = "draining"
REASON_IN_FLIGHT = "in_flight"
REASON_QUEUE_HELD = "queue_held"
REASON_SHEDDING = "governor_shedding"
REASON_MEMORY_TIGHT = "memory_tight"
REASON_MEMORY_UNMEASURED = "memory_unmeasured"


def _load_schema(name: str) -> dict:
    return json.loads((_SCHEMAS / name).read_text())


def _validator(name: str):
    """A validator for one schema, with every contract schema
    registered so relative $refs resolve — the registry pattern
    the earlier slices build."""
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


_POLICY_VALIDATOR = _validator("generosity_policy.schema.json")
_ADVERT_VALIDATOR = _validator("spare_capacity.schema.json")
_REPORT_VALIDATOR = _validator("generosity_report.schema.json")


class GenerosityError(Exception):
    """Any idle-generosity failure: an unshaped policy or
    load, a record that fails its contract schema."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise GenerosityError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_policy(policy: dict) -> dict:
    """A generosity policy against its schema."""
    return _validate(_POLICY_VALIDATOR, policy, "generosity policy")


def validate_advertisement(record: dict) -> dict:
    """A spare-capacity advertisement against its schema."""
    return _validate(_ADVERT_VALIDATOR, record, "spare capacity advertisement")


def validate_report(record: dict) -> dict:
    """A generosity report against its schema."""
    return _validate(_REPORT_VALIDATOR, record, "generosity report")


def default_policy() -> dict:
    """The default generosity thresholds — a policy, not a fact:
    idle means nothing in flight, nothing queued, at least a
    quarter of memory spare, and no shedding or draining node
    calls itself generous."""
    return validate_policy(
        {
            "version": 1,
            "max_in_flight": 0,
            "max_queue_depth": 0,
            "min_spare_memory_fraction": 0.25,
            "withdraw_when_shedding": True,
            "withdraw_when_draining": True,
        }
    )


def _count(load: dict, name: str) -> int:
    value = load.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise GenerosityError(f"load.{name} must be a non-negative integer")
    return value


def _optional_count(load: dict, name: str) -> int | None:
    value = load.get(name)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise GenerosityError(f"load.{name} must be a non-negative integer or null")
    return value


class IdleGenerosity:
    """One node's generosity ledger: fresh verdicts from
    measured load, with every transition historied.

    ``policy`` is a ``generosity_policy`` v1 (the default
    when none is handed in). ``now`` is an injectable clock.
    """

    def __init__(self, node_id: str, policy: dict | None = None, *, now=None):
        if not isinstance(node_id, str) or not node_id:
            raise GenerosityError("an idle-generosity ledger needs a node_id")
        self.node_id = node_id
        self._policy = validate_policy(dict(policy)) if policy is not None else default_policy()
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._status: str = UNEVALUATED
        self._generous_since: datetime | None = None
        self._observations = 0
        self._generous_observations = 0
        self._withdrawn_observations = 0
        self._history: list[dict] = []
        self._last: dict | None = None
        self._events: list[dict] = []
        self._event_seq = 0

    # -- facts ----------------------------------------------------------

    @property
    def policy(self) -> dict:
        return dict(self._policy)

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    @property
    def generous(self) -> bool:
        """True while the last evaluation advertised spare."""
        return self._status == GENEROUS

    def current(self) -> dict | None:
        """The last advertisement, or None before the first
        evaluation — no verdict is claimed before one is made."""
        return self._last

    # -- internals --------------------------------------------------------

    def _emit(self, type: str, advert: dict) -> dict:
        event = make_event(
            type=type,
            node_id=self.node_id,
            component="node_agent",
            payload={
                "status": advert["status"],
                "reasons": advert["reasons"],
                "in_flight": advert["load"]["in_flight"],
                "queue_depth": advert["load"]["queue_depth"],
                "spare_memory_bytes": advert["spare"]["memory_bytes"],
            },
            seq=self._event_seq,
            at=self._now(),
        )
        self._event_seq += 1
        self._events.append(event)
        return event

    # -- evaluation -------------------------------------------------------

    def evaluate(self, load: dict) -> dict:
        """Judge one present-tense load and return the validated
        ``spare_capacity`` v1 advertisement.

        ``load`` names the node's present facts: ``lifecycle_state``
        (required), ``in_flight`` and ``queue_depth`` (required
        counts), and optionally ``queue_free_slots``,
        ``model_slots_free``, ``governor_shedding``,
        ``memory_total_bytes``, ``memory_used_bytes``,
        ``cpu_share_used``, ``accelerators`` (the latest census's
        measured kinds), and ``models_loaded``. A missing memory
        reading is read as unmeasured — never as plenty."""
        if not isinstance(load, dict):
            raise GenerosityError("a generosity evaluation needs a load record")
        lifecycle = load.get("lifecycle_state")
        if not isinstance(lifecycle, str) or not lifecycle:
            raise GenerosityError("load.lifecycle_state must name the lifecycle state")
        in_flight = _count(load, "in_flight")
        queue_depth = _count(load, "queue_depth")
        queue_free = _optional_count(load, "queue_free_slots")
        slots_free = _optional_count(load, "model_slots_free")
        shedding = load.get("governor_shedding", False)
        if not isinstance(shedding, bool):
            raise GenerosityError("load.governor_shedding must be a boolean")
        accelerators = load.get("accelerators") or []
        if not isinstance(accelerators, list) or not all(
            isinstance(kind, str) for kind in accelerators
        ):
            raise GenerosityError("load.accelerators must be a list of kind names")
        models_loaded = load.get("models_loaded") or []
        if not isinstance(models_loaded, list) or not all(
            isinstance(name, str) for name in models_loaded
        ):
            raise GenerosityError("load.models_loaded must be a list of model names")

        total = load.get("memory_total_bytes")
        used = load.get("memory_used_bytes")
        for name, value in (("memory_total_bytes", total), ("memory_used_bytes", used)):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise GenerosityError(f"load.{name} must be a non-negative integer or null")
        cpu_used = load.get("cpu_share_used")
        if cpu_used is not None and (
            not isinstance(cpu_used, (int, float))
            or isinstance(cpu_used, bool)
            or not 0 <= cpu_used <= 1
        ):
            raise GenerosityError("load.cpu_share_used must be a share in [0, 1] or null")

        spare_bytes: int | None = None
        spare_fraction: float | None = None
        if total is not None and used is not None and total > 0:
            spare_bytes = max(0, total - used)
            spare_fraction = round(spare_bytes / total, 6)

        reasons: list[str] = []
        if lifecycle == "draining":
            if self._policy["withdraw_when_draining"]:
                reasons.append(REASON_DRAINING)
        elif lifecycle != "joined":
            reasons.append(REASON_NOT_JOINED)
        if in_flight > self._policy["max_in_flight"]:
            reasons.append(REASON_IN_FLIGHT)
        if queue_depth > self._policy["max_queue_depth"]:
            reasons.append(REASON_QUEUE_HELD)
        if shedding and self._policy["withdraw_when_shedding"]:
            reasons.append(REASON_SHEDDING)
        if spare_fraction is None:
            reasons.append(REASON_MEMORY_UNMEASURED)
        elif spare_fraction < self._policy["min_spare_memory_fraction"]:
            reasons.append(REASON_MEMORY_TIGHT)

        generous = not reasons
        with self._lock:
            self._observations += 1
            if generous:
                self._generous_observations += 1
            else:
                self._withdrawn_observations += 1
            new_status = GENEROUS if generous else WITHDRAWN
            at = self._now()
            if new_status != self._status:
                self._history.append(
                    {
                        "at": _fmt(at),
                        "from_status": self._status,
                        "to_status": new_status,
                        "reasons": list(reasons),
                    }
                )
                self._generous_since = at if generous else None
                transition = True
            else:
                transition = False
            advert = {
                "version": 1,
                "node_id": self.node_id,
                "at": _fmt(at),
                "generous": generous,
                "status": new_status,
                "reasons": reasons,
                "policy": dict(self._policy),
                "load": {
                    "in_flight": in_flight,
                    "queue_depth": queue_depth,
                    "queue_free_slots": queue_free,
                    "model_slots_free": slots_free,
                },
                "spare": {
                    "memory_bytes": spare_bytes,
                    "memory_fraction": spare_fraction,
                    "cpu_share": (
                        round(1.0 - float(cpu_used), 6) if cpu_used is not None else None
                    ),
                },
                "capacity": {
                    "accelerators": sorted(set(accelerators)),
                    "models_loaded": list(models_loaded),
                },
                "observations": self._observations,
                "transitions": len(self._history),
                "generous_since": (
                    _fmt(self._generous_since) if self._generous_since else None
                ),
            }
            validate_advertisement(advert)
            self._status = new_status
            self._last = advert
            if transition:
                self._emit(
                    "generosity.advertised" if generous else "generosity.withdrawn",
                    advert,
                )
            return advert

    def report(self) -> dict:
        """The validated generosity_report v1: every transition,
        with each withdrawal's reasons named."""
        with self._lock:
            return validate_report(
                {
                    "version": 1,
                    "node_id": self.node_id,
                    "at": _fmt(self._now()),
                    "current_status": self._status,
                    "observations": self._observations,
                    "generous_observations": self._generous_observations,
                    "withdrawn_observations": self._withdrawn_observations,
                    "transitions": len(self._history),
                    "generous_since": (
                        _fmt(self._generous_since) if self._generous_since else None
                    ),
                    "history": [dict(row) for row in self._history],
                }
            )


__all__ = [
    "GENEROUS",
    "UNEVALUATED",
    "WITHDRAWN",
    "GenerosityError",
    "IdleGenerosity",
    "default_policy",
    "validate_advertisement",
    "validate_policy",
    "validate_report",
]
