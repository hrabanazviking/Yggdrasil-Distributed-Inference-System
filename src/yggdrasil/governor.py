"""Resource governor — Y-028.

The census (Y-022) measures a machine and the model lifecycle
(Y-027) charges resident models against memory budgets, but
nothing yet *acts* on heat, pressure, and load. This slice is
the node's politeness, as code — the accelerator-first law's
other half: a node that is cooking, memory-pressed, or
CPU-saturated sheds load *before* it cooks, by refusing new
work gracefully, never by killing work already running.

- **Ceilings as data.** A ``GovernorPolicy`` (schema v1) names
  one node's ceilings: the temperature at which shedding
  starts (``thermal_shed_c``) and the lower temperature at
  which it may stop (``thermal_resume_c`` — the gap between
  them is hysteresis, so a node hovering at the line does not
  flap between accepting and refusing), an optional hard
  temperature (``thermal_hard_c``) at which resident-model
  shedding is *recommended*, the largest fraction of total
  memory the node's work may occupy
  (``max_memory_fraction``), and the largest share of the
  node's CPU new work may claim in total (``max_cpu_share``).
  A policy whose resume line is not below its shed line, or
  whose hard line is not above it, is refused at construction.
- **Readings are measured, never assumed.** A reading set is
  ``thermal_c`` / ``memory_used_bytes`` /
  ``memory_total_bytes`` / ``cpu_share_used``, any of which may
  be None — unknown. An unknown reading disables only its own
  check and is reported as null in the state: the governor
  never treats a missing thermal sensor as a cool machine
  (this VM exposes none, exactly as the census says), and
  never invents a memory total. The default provider reads
  the real machine from files and stdlib only —
  ``/proc/meminfo`` (MemTotal/MemAvailable), ``/proc/loadavg``
  (1-minute load over core count), and the thermal zones the
  census probes — and tests inject their own readings.
- **Graceful refusal.** ``admit()`` answers a validated
  ``GovernorDecision``: admitted, or refused *by name* — the
  reasons list says which ceilings bite (``thermal`` /
  ``thermal_hard`` / ``memory`` / ``cpu``), and the decision
  carries the projected memory fraction and CPU share the
  task would have produced, so a refusal is a number, not a
  shrug. While any ceiling bites, the governor is *shedding*:
  new tasks are refused, tasks already in flight are never
  touched — shedding is a door closing gently, not a hand
  pushing work off the table. Thermal shedding latches: once
  started it holds until the temperature falls below the
  resume line, however briefly it dipped under the shed line.
- **Resident shedding is recommended, never automatic.** At
  the hard temperature the state sets
  ``shed_resident_models_recommended`` — the operator (or a
  later slice) may unload through the lifecycle (Y-027),
  whose own rules already protect in-use models. The governor
  itself unloads nothing and cancels nothing.

Every transition (shedding started / cleared) and every
refusal is recorded as a Y-013 EventEnvelope from the
``node_agent`` component in the governor's own ``events``
list. The node agent (wired at this slice) consults the
governor before taking a task lease, and reports its verdict
in ``health()`` and ``status()``. No network path exists in
this module; acting on real heat on named hardware is Y-044,
and the sandbox that runs behind the door is Y-029.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.census import system_probe_set
from yggdrasil.observability import make_event

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

# The defaults an operator starts from; a node's own policy
# file (deployment wiring) may tighten or loosen them.
DEFAULT_POLICY = {
    "version": 1,
    "thermal_shed_c": 80.0,
    "thermal_resume_c": 72.0,
    "thermal_hard_c": 90.0,
    "max_memory_fraction": 0.9,
    "max_cpu_share": 0.9,
}

_READING_KEYS = {"thermal_c", "memory_used_bytes", "memory_total_bytes", "cpu_share_used"}


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


_POLICY_VALIDATOR = _validator("governor_policy.schema.json")
_STATE_VALIDATOR = _validator("governor_state.schema.json")
_DECISION_VALIDATOR = _validator("governor_decision.schema.json")


class GovernorError(Exception):
    """Any governor failure: a bad policy, an unshaped reading
    set, a missing readings source."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise GovernorError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_policy(policy: dict) -> dict:
    """A policy against its schema *and* its ordering rules:
    resume below shed, hard above shed — hysteresis needs a
    gap, and a hard line at or under the shed line is a lie."""
    _validate(_POLICY_VALIDATOR, policy, "governor policy")
    if not policy["thermal_resume_c"] < policy["thermal_shed_c"]:
        raise GovernorError(
            "thermal_resume_c must be below thermal_shed_c "
            f"({policy['thermal_resume_c']} !< {policy['thermal_shed_c']})"
        )
    hard = policy.get("thermal_hard_c")
    if hard is not None and not hard > policy["thermal_shed_c"]:
        raise GovernorError(
            "thermal_hard_c must be above thermal_shed_c "
            f"({hard} !> {policy['thermal_shed_c']})"
        )
    return policy


def default_policy() -> dict:
    """The starting ceilings, validated — an operator's
    policy, not a measured fact about any machine."""
    return validate_policy(dict(DEFAULT_POLICY))


def system_readings(root: Path | str = "/") -> dict:
    """The real machine's current readings, from files and
    stdlib only. Thermal comes from the census probe set;
    memory from /proc/meminfo (used = total − available, both
    measured; a meminfo with no MemAvailable yields an unknown
    used value rather than a guess); CPU share is the 1-minute
    load average over the core count. Anything unreadable is
    None — unknown, never zero."""
    base = Path(root)
    thermal = system_probe_set(base).thermal_c()
    memory_used = None
    memory_total = None
    try:
        fields = {}
        for line in (base / "proc" / "meminfo").read_text(errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] in ("MemTotal:", "MemAvailable:"):
                fields[parts[0]] = int(parts[1]) * 1024  # KiB to bytes
        memory_total = fields.get("MemTotal:")
        if memory_total is not None and "MemAvailable:" in fields:
            memory_used = memory_total - fields["MemAvailable:"]
    except (OSError, ValueError):
        memory_used = memory_total = None
    cpu_share = None
    try:
        load1 = float((base / "proc" / "loadavg").read_text().split()[0])
        cores = os.cpu_count() or 1
        cpu_share = round(load1 / cores, 4)
    except (OSError, ValueError, IndexError):
        cpu_share = None
    return {
        "thermal_c": thermal,
        "memory_used_bytes": memory_used,
        "memory_total_bytes": memory_total,
        "cpu_share_used": cpu_share,
    }


class ResourceGovernor:
    """One node's governor: ceilings + the last verdict.

    ``readings`` is a callable returning a reading set (the
    seam tests and deployment wiring use); the default is the
    real machine via :func:`system_readings`. The clock is
    injectable so verdict timestamps are testable.
    """

    def __init__(self, node_id: str, policy: dict | None = None, *, readings=None, now=None):
        if not isinstance(node_id, str) or not node_id:
            raise GovernorError("a governor needs a node_id")
        self.node_id = node_id
        self._policy = validate_policy(dict(policy) if policy is not None else default_policy())
        self._readings = readings if readings is not None else system_readings
        self._now = now or (lambda: datetime.now(UTC))
        self._thermal_latched = False
        self._shedding = False
        self._last_state: dict | None = None
        self._seq = 0
        self._events: list[dict] = []
        self._admissions = 0
        self._refusals = 0

    # -- facts ----------------------------------------------------------

    @property
    def policy(self) -> dict:
        return dict(self._policy)

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    @property
    def shedding(self) -> bool:
        return self._shedding

    @property
    def last_state(self) -> dict | None:
        """The last evaluated GovernorState, or None before
        the first evaluation — status reports read this
        without forcing a fresh measurement."""
        return self._last_state

    @property
    def admissions(self) -> int:
        return self._admissions

    @property
    def refusals(self) -> int:
        return self._refusals

    def _emit(self, type: str, payload: dict) -> dict:
        event = make_event(
            type=type,
            node_id=self.node_id,
            component="node_agent",
            payload=payload,
            seq=self._seq,
            at=self._now(),
        )
        self._seq += 1
        self._events.append(event)
        return event

    # -- readings ----------------------------------------------------------

    def _clean_readings(self, readings: dict | None) -> dict:
        if readings is None:
            try:
                readings = self._readings()
            except Exception as exc:
                raise GovernorError(f"readings provider failed: {exc}") from exc
        if not isinstance(readings, dict):
            raise GovernorError(f"a reading set is a dict, got {type(readings).__name__}")
        unknown = set(readings) - _READING_KEYS
        if unknown:
            raise GovernorError(f"unknown reading key(s): {sorted(unknown)}")
        cleaned = {key: readings.get(key) for key in sorted(_READING_KEYS)}
        for key in ("memory_used_bytes", "memory_total_bytes"):
            value = cleaned[key]
            if value is not None and (not isinstance(value, int) or value < 0):
                raise GovernorError(f"reading {key} must be a non-negative int or None")
        for key in ("thermal_c", "cpu_share_used"):
            value = cleaned[key]
            if value is not None:
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise GovernorError(f"reading {key} must be a number or None")
                cleaned[key] = float(value)
        if cleaned["cpu_share_used"] is not None and cleaned["cpu_share_used"] < 0:
            raise GovernorError("reading cpu_share_used may not be negative")
        return cleaned

    # -- evaluation ----------------------------------------------------------

    def evaluate(self, readings: dict | None = None) -> dict:
        """Evaluate one reading set against the ceilings and
        answer the validated GovernorState. Thermal shedding
        latches: above the shed line it starts, and it clears
        only below the resume line. Transitions are recorded
        as events; an unchanged verdict records nothing."""
        cleaned = self._clean_readings(readings)
        policy = self._policy
        reasons: list[str] = []

        thermal = cleaned["thermal_c"]
        if thermal is not None:
            if thermal >= policy["thermal_shed_c"]:
                self._thermal_latched = True
            elif thermal < policy["thermal_resume_c"]:
                self._thermal_latched = False
        if self._thermal_latched:
            reasons.append("thermal")
            hard = policy.get("thermal_hard_c")
            if thermal is not None and hard is not None and thermal >= hard:
                reasons.append("thermal_hard")

        memory_fraction = None
        used, total = cleaned["memory_used_bytes"], cleaned["memory_total_bytes"]
        if used is not None and total:
            memory_fraction = round(used / total, 4)
            if memory_fraction >= policy["max_memory_fraction"]:
                reasons.append("memory")

        cpu = cleaned["cpu_share_used"]
        if cpu is not None and cpu >= policy["max_cpu_share"]:
            reasons.append("cpu")

        shedding = bool(reasons)
        hard = "thermal_hard" in reasons
        state = {
            "node_id": self.node_id,
            "at": _fmt(self._now()),
            "shedding": shedding,
            "state": "critical" if hard else ("shedding" if shedding else "within_ceilings"),
            "reasons": reasons,
            "shed_resident_models_recommended": hard,
            "readings": {
                "thermal_c": thermal,
                "memory_used_bytes": used,
                "memory_total_bytes": total,
                "memory_fraction": memory_fraction,
                "cpu_share_used": cpu,
            },
            "policy": dict(policy),
        }
        _validate(_STATE_VALIDATOR, state, "governor state")
        if shedding and not self._shedding:
            self._emit("governor.shedding_started", {"reasons": list(reasons)})
        elif self._shedding and not shedding:
            self._emit("governor.shedding_cleared", {"reasons": []})
        self._shedding = shedding
        self._last_state = state
        return state

    def state(self, readings: dict | None = None) -> dict:
        """The current verdict: a fresh evaluation when a
        reading set (or a provider) is at hand, else the last
        verdict — and before any evaluation, an honest
        all-unknown evaluation, never an assumed calm."""
        if readings is not None:
            return self.evaluate(readings)
        return self.evaluate(None)

    # -- admission ----------------------------------------------------------

    def admit(
        self,
        readings: dict | None = None,
        *,
        task_id: str | None = None,
        memory_bytes: int = 0,
        cpu_share: float = 0.0,
    ) -> dict:
        """Decide one new task against fresh readings *and*
        its own footprint: refused when the node is already
        shedding, or when the task itself would push memory or
        CPU past its ceiling (the projected numbers ride in the
        decision). In-flight work is never reconsidered — this
        door only faces forward."""
        if not isinstance(memory_bytes, int) or memory_bytes < 0:
            raise GovernorError("requested memory_bytes must be a non-negative int")
        if not isinstance(cpu_share, (int, float)) or isinstance(cpu_share, bool):
            raise GovernorError("requested cpu_share must be a number")
        if cpu_share < 0:
            raise GovernorError("requested cpu_share may not be negative")
        state = self.evaluate(readings)
        reasons = list(state["reasons"])
        cleaned = state["readings"]

        projected_memory = cleaned["memory_fraction"]
        if cleaned["memory_used_bytes"] is not None and cleaned["memory_total_bytes"]:
            projected_memory = round(
                (cleaned["memory_used_bytes"] + memory_bytes)
                / cleaned["memory_total_bytes"],
                4,
            )
            if projected_memory > self._policy["max_memory_fraction"] and "memory" not in reasons:
                reasons.append("memory")

        projected_cpu = cleaned["cpu_share_used"]
        if cleaned["cpu_share_used"] is not None:
            projected_cpu = round(cleaned["cpu_share_used"] + float(cpu_share), 4)
            if projected_cpu > self._policy["max_cpu_share"] and "cpu" not in reasons:
                reasons.append("cpu")

        admitted = not reasons
        decision = {
            "node_id": self.node_id,
            "at": _fmt(self._now()),
            "task_id": task_id,
            "admitted": admitted,
            "shedding": state["shedding"],
            "reasons": reasons,
            "requested_memory_bytes": int(memory_bytes),
            "requested_cpu_share": float(cpu_share),
            "projected_memory_fraction": projected_memory,
            "projected_cpu_share": projected_cpu,
        }
        _validate(_DECISION_VALIDATOR, decision, "governor decision")
        if admitted:
            self._admissions += 1
        else:
            self._refusals += 1
            self._emit(
                "governor.task_refused",
                {"task_id": task_id, "reasons": list(reasons)},
            )
        return decision


__all__ = [
    "DEFAULT_POLICY",
    "GovernorError",
    "ResourceGovernor",
    "default_policy",
    "system_readings",
    "validate_policy",
]
