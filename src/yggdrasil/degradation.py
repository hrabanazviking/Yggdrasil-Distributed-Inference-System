"""Graceful degradation — Y-040.

A node that loses its accelerator mid-life has two ways to
lie: keep advertising the capability it no longer has, or go
silent and let the mesh guess. This slice is the third way —
the house way: keep serving on CPU, and *declare* the reduced
capability, in validated data, the moment a census refresh
measures the loss.

- **Baseline vs present.** A :class:`DegradationTracker`
  watches the node's censuses (Y-022). The *baseline* is every
  accelerator kind the node has ever measured — its known
  full capability. The *present* is the latest census's kinds.
  Kinds in the baseline but not the present are **lost**, and
  they are named, never smoothed over. A kind appearing for
  the first time *grows* the baseline: gaining hardware is an
  upgrade, not a degradation, and is never reported as one.
- **Four honest statuses.** ``full`` (baseline present,
  nothing lost), ``degraded_partial`` (some kinds lost, some
  remain — the node still accelerates, on fewer devices),
  ``degraded_cpu_only`` (every baseline kind lost — the node
  serves on CPU alone, and ``serving_on_cpu_only`` says so in
  so many words), and ``cpu_only_by_design`` (the baseline is
  empty — a VM citizen, a phone shape: CPU-only is its
  *design*, and this slice never dresses a by-design node as
  a degraded one, nor a degraded one as by-design). A tracker
  that has observed nothing reports ``observations: 0`` —
  the tell that no capability has been measured at all.
- **VRAM is tracked the same way.** The baseline VRAM is the
  largest figure ever measured; the present figure rides
  beside it (``None`` when the latest census measured none —
  absence read as absence, as everywhere in this tree).
- **Resident impact, judged by format.** At each observation
  the caller may hand in the lifecycle's resident records
  (Y-027) and the store's entries (Y-026). Every model
  resident on the *accelerator* pool is sorted by its pinned
  format against the Y-038 fitness policy's own format table
  (read, never duplicated): a format needing no accelerator
  (gguf, ggml) is a **cpu_fallback** model — it can keep
  serving from the system pool; a format requiring a kind
  that is no longer present is **stranded** — it cannot run
  on this node until the accelerator returns, and it is
  named; a format whose accelerator is still present is
  unaffected. A model whose format the policy does not name
  is stranded, never assumed CPU-servable (the policy's own
  ``on_unknown_format: refuse`` law). Stranded models that
  are in use are named separately (``stranded_in_use``): a
  running task's model is never yanked from under it.
- **Transitions are history.** Every change of status or of
  the lost set is recorded as a transition (from/to status,
  kinds lost and regained, the moment), counted as a
  degradation when it crosses into a degraded status and as
  a recovery when it crosses back to ``full`` — regaining an
  accelerator restores the *declaration* at once; it does
  not magically reload models (residency is Y-027's act, and
  the agent's re-placement at recovery is deliberately not
  claimed here). Each crossing is also a Y-013 EventEnvelope
  from the ``node_agent`` component — ``degradation.entered``
  / ``degradation.changed`` / ``degradation.recovered`` —
  kinds and statuses only, no payload content.

The node agent (wired at this slice) grows the seam:
``degradation_tracker=`` at construction, and every
``refresh_census()`` feeds the fresh census to the tracker
with the lifecycle's residents and the store's entries.
When the fresh state is degraded, the agent then *acts* on
the assessment through the lifecycle: cpu_fallback residents
are re-placed into the system pool (unload + load at
``system`` placement, costs measured by Y-027 as always),
stranded residents that are not in use are unloaded, and
in-use residents are left running and named in the refresh
result's ``degradation_acts`` — the acts, their deferrals,
and any failures are reported there, never hidden.
``degradation_state()`` / ``degradation_report()`` read the
tracker back, and ``status()`` carries the state. Without a
tracker wired, the agent degrades exactly as before:
unknowingly — which is why all four citizen factories wire
one over their factory-measured census, so the baseline is
the machine's real capability from the first refresh.

Honesty: degradation here is *measured*, not simulated by
this module — the tracker judges the censuses it is handed,
and on a real node those censuses come from the real probes.
No hardware loss has been staged for this slice: the
acceptance losses are probe fixtures shaped like losses,
labelled as such in the tests, and a real accelerator dying
under a live citizen remains NOT PROVEN until it happens on
named hardware and is recorded. Nothing here restarts an
engine, touches a driver, or promises the Hailo chip
anything — the lost kinds are whatever the census measured,
and a ``hailo`` loss would be declared exactly like a
``cuda`` one, no more and no less.
"""

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.census import CensusError, validate_census
from yggdrasil.observability import make_event
from yggdrasil.quant_fitness import load_policy

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

FULL = "full"
DEGRADED_PARTIAL = "degraded_partial"
DEGRADED_CPU_ONLY = "degraded_cpu_only"
CPU_ONLY_BY_DESIGN = "cpu_only_by_design"
STATUSES = (FULL, DEGRADED_PARTIAL, DEGRADED_CPU_ONLY, CPU_ONLY_BY_DESIGN)
_DEGRADED = (DEGRADED_PARTIAL, DEGRADED_CPU_ONLY)


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


_STATE_VALIDATOR = _validator("degradation_state.schema.json")
_REPORT_VALIDATOR = _validator("degradation_report.schema.json")


class DegradationError(Exception):
    """Any degradation failure: a foreign or unshaped census,
    a record that fails its contract schema."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise DegradationError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_state(record: dict) -> dict:
    """A degradation state against its schema."""
    return _validate(_STATE_VALIDATOR, record, "degradation state")


def validate_report(record: dict) -> dict:
    """A degradation report against its schema."""
    return _validate(_REPORT_VALIDATOR, record, "degradation report")


def accelerator_kinds(census: dict) -> tuple[str, ...]:
    """A census's real accelerator kinds, sorted — the honest
    ``none`` entry is not a kind a node can lose."""
    return tuple(
        sorted(
            entry["kind"]
            for entry in census.get("accelerators", [])
            if entry.get("kind") != "none"
        )
    )


class DegradationTracker:
    """One node's degradation ledger: baseline vs present,
    judged on every census it is handed.

    ``policy`` is the Y-038 quant-fitness policy (its format
    table decides CPU fallback); the tree's own policy file is
    loaded by default. ``now`` is an injectable clock.
    """

    def __init__(self, node_id: str, *, policy: dict | None = None, now=None):
        if not isinstance(node_id, str) or not node_id:
            raise DegradationError("a degradation tracker needs a node_id")
        self.node_id = node_id
        policy = policy if policy is not None else load_policy()
        self._format_requirements = {
            row["format"]: row["requires_accelerator"]
            for row in policy["format_requirements"]
        }
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._baseline: set[str] = set()
        self._baseline_vram_gb: float | None = None
        self._current: tuple[str, ...] = ()
        self._current_vram_gb: float | None = None
        self._status: str = CPU_ONLY_BY_DESIGN
        self._degraded_since: datetime | None = None
        self._observations = 0
        self._history: list[dict] = []
        self._degradations = 0
        self._recoveries = 0
        self._residents: dict = {
            "accelerator": [],
            "cpu_fallback": [],
            "stranded": [],
            "stranded_in_use": [],
        }
        self._events: list[dict] = []
        self._event_seq = 0

    # -- facts ----------------------------------------------------------

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    @property
    def degraded(self) -> bool:
        """True while the node's declared status is degraded."""
        return self._status in _DEGRADED

    @property
    def status_name(self) -> str:
        return self._status

    @property
    def lost_accelerators(self) -> tuple[str, ...]:
        return tuple(sorted(self._baseline - set(self._current)))

    # -- internals --------------------------------------------------------

    def _emit(self, type: str, state: dict) -> dict:
        event = make_event(
            type=type,
            node_id=self.node_id,
            component="node_agent",
            payload={
                "status": state["status"],
                "lost_accelerators": state["lost_accelerators"],
                "current_accelerators": state["current_accelerators"],
                "serving_on_cpu_only": state["serving_on_cpu_only"],
            },
            seq=self._event_seq,
            at=self._now(),
        )
        self._event_seq += 1
        self._events.append(event)
        return event

    def _assess_residents(self, residents, store_entries, current: set[str]) -> dict:
        """Sort the accelerator-pool residents by the loss:
        unaffected, CPU-servable, stranded — by pinned format
        against the fitness policy's table, never by hope."""
        formats: dict[str, str] = {}
        if store_entries is not None:
            entries = (
                store_entries.values()
                if isinstance(store_entries, dict)
                else store_entries
            )
            for entry in entries:
                if isinstance(entry, dict) and "name" in entry:
                    formats[entry["name"]] = entry.get("format")
        impact: dict[str, list[str]] = {
            "accelerator": [],
            "cpu_fallback": [],
            "stranded": [],
            "stranded_in_use": [],
        }
        for resident in residents or []:
            if resident.get("placement") != "accelerator":
                continue  # already serving from the system pool
            name = resident["name"]
            fmt = formats.get(name)
            if fmt is None or fmt not in self._format_requirements:
                # An unnamed format is never assumed CPU-servable.
                impact["stranded"].append(name)
            else:
                required = self._format_requirements[fmt]
                if required is None:
                    impact["cpu_fallback"].append(name)
                elif required in current:
                    impact["accelerator"].append(name)
                else:
                    impact["stranded"].append(name)
            if name in impact["stranded"] and resident.get("in_use", 0) > 0:
                impact["stranded_in_use"].append(name)
        return {key: sorted(names) for key, names in impact.items()}

    # -- observation ------------------------------------------------------

    def observe_census(
        self,
        census: dict,
        *,
        residents=(),
        store_entries=None,
    ) -> dict:
        """Judge one fresh census against the baseline and
        record the verdict: the validated degradation_state v1.

        ``residents`` are the lifecycle's loaded-model records
        (Y-027) and ``store_entries`` the store's entries
        (Y-026); together they let the state name what the
        loss does to the models resident on the accelerator.
        A census naming another node is refused by name — one
        node's loss is never booked to another's ledger."""
        try:
            validate_census(census)
        except CensusError as exc:
            raise DegradationError(str(exc)) from exc
        if census["node_id"] != self.node_id:
            raise DegradationError(
                f"census names node {census['node_id']!r}, this tracker "
                f"belongs to {self.node_id!r}"
            )
        with self._lock:
            previous_status = self._status
            previous_lost = set(self.lost_accelerators)
            current = set(accelerator_kinds(census))
            self._baseline |= current
            vram = census.get("vram_gb")
            if vram is not None:
                self._baseline_vram_gb = (
                    float(vram)
                    if self._baseline_vram_gb is None
                    else max(self._baseline_vram_gb, float(vram))
                )
            self._current = tuple(sorted(current))
            self._current_vram_gb = float(vram) if vram is not None else None
            lost = self._baseline - current
            if not self._baseline:
                status = CPU_ONLY_BY_DESIGN
            elif not lost:
                status = FULL
            elif current:
                status = DEGRADED_PARTIAL
            else:
                status = DEGRADED_CPU_ONLY
            self._observations += 1
            self._residents = self._assess_residents(residents, store_entries, current)
            state = self._state_record(status=status, lost=lost)
            if self._observations > 1 and (status != previous_status or lost != previous_lost):
                was_degraded = previous_status in _DEGRADED
                is_degraded = status in _DEGRADED
                if is_degraded and not was_degraded:
                    self._degradations += 1
                    self._degraded_since = self._now()
                    state = self._state_record(status=status, lost=lost)
                elif was_degraded and status == FULL:
                    self._recoveries += 1
                    self._degraded_since = None
                    state = self._state_record(status=status, lost=lost)
                self._history.append(
                    {
                        "at": _fmt(self._now()),
                        "from_status": previous_status,
                        "to_status": status,
                        "lost_accelerators": sorted(lost),
                        "regained_accelerators": sorted(previous_lost - lost),
                    }
                )
                state = self._state_record(status=status, lost=lost)
                if is_degraded and not was_degraded:
                    self._emit("degradation.entered", state)
                elif was_degraded and status == FULL:
                    self._emit("degradation.recovered", state)
                elif was_degraded and is_degraded:
                    self._emit("degradation.changed", state)
            self._status = status
            return state

    def _state_record(self, *, status: str, lost: set[str]) -> dict:
        return _validate(
            _STATE_VALIDATOR,
            {
                "version": 1,
                "node_id": self.node_id,
                "at": _fmt(self._now()),
                "status": status,
                "baseline_accelerators": sorted(self._baseline),
                "current_accelerators": list(self._current),
                "lost_accelerators": sorted(lost),
                "serving_on_cpu_only": status == DEGRADED_CPU_ONLY,
                "baseline_vram_gb": self._baseline_vram_gb,
                "current_vram_gb": self._current_vram_gb,
                "degraded_since": (
                    _fmt(self._degraded_since) if self._degraded_since else None
                ),
                "observations": self._observations,
                "transitions": len(self._history),
                "residents": {k: list(v) for k, v in self._residents.items()},
            },
            "degradation state",
        )

    # -- reading the ledger back ------------------------------------------

    def state(self) -> dict:
        """The current validated degradation_state v1 — before
        any observation it reports ``observations: 0`` and the
        by-design status: nothing measured, nothing claimed."""
        with self._lock:
            return self._state_record(
                status=self._status, lost=set(self.lost_accelerators)
            )

    def report(self) -> dict:
        """The validated degradation_report v1: the totals and
        every transition, in the order they were declared."""
        with self._lock:
            return _validate(
                _REPORT_VALIDATOR,
                {
                    "version": 1,
                    "node_id": self.node_id,
                    "at": _fmt(self._now()),
                    "current_status": self._status,
                    "observations": self._observations,
                    "transitions": len(self._history),
                    "degradations": self._degradations,
                    "recoveries": self._recoveries,
                    "history": [dict(row) for row in self._history],
                },
                "degradation report",
            )


__all__ = [
    "CPU_ONLY_BY_DESIGN",
    "DEGRADED_CPU_ONLY",
    "DEGRADED_PARTIAL",
    "FULL",
    "STATUSES",
    "DegradationError",
    "DegradationTracker",
    "accelerator_kinds",
    "validate_report",
    "validate_state",
]
