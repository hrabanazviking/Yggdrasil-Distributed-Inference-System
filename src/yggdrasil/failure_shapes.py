"""Failure shapes — Y-032.

Until this slice the node's failures travelled under their
Y-007 error names, but nothing *sorted* them: an engine dying,
a task eating all the memory, a node cooking, and a model file
quietly rotting on disk are four different accidents, and a
node (and later Verðandi, Part IV) that treats them as one
generic ``failed`` will take the wrong medicine for three of
them. This slice is the sorting, the containment, and the
distinct report for each:

- **engine_crash** (``engine_error`` / ``engine_unreachable``):
  the engine that served a task died or vanished mid-work.
  Containment: that engine — and only that engine — is
  *quarantined*: ``engine_available()`` says False for it
  until an operator (or a later health probe slice) clears it
  by name with ``clear_engine()``. The node itself stays up,
  its other engines stay available, and the failed task keeps
  the partial text the sandbox already preserved (Y-029).
- **oom** (``oom``, or a ``MemoryError``, or a reading set in
  which memory is fully consumed): a task or the node ran out
  of memory. Containment: the task is terminated (its sandbox
  result is the termination record), the incident latches
  ``oom_active`` and recommends load shedding in its report
  until a later check sees memory below the ceiling again —
  new work's admission stays the governor's act (Y-028);
  this monitor names the accident, it does not re-decide
  admission.
- **thermal_throttle** (``thermal_shed``, or a governor
  verdict whose reasons carry ``thermal`` /
  ``thermal_hard``): the node is throttling under heat.
  Containment is the governor's own load shedding, named
  here as the failure it is; the incident is recorded once
  when throttling *starts* (severity ``critical`` at the
  hard line, ``warning`` at the shed line) and a
  ``failure.thermal_cleared`` event marks its end — a node
  hovering at the line does not spam a new incident per
  reading (the governor's hysteresis, Y-028, is the latch's
  shape too).
- **model_corrupt** (``model_hash_mismatch``, or a model
  store verification whose status is ``corrupt`` or
  ``missing``): the bytes on disk are not the bytes the pin
  names (Y-026). Containment: the model is *quarantined* —
  it was already never counted present by the store; here it
  is named, excluded by ``model_available()``, and, when a
  model lifecycle (Y-027) is wired and the corrupt model is
  somehow resident, unloaded through the lifecycle (an
  in-use model that refuses unload stays quarantined with
  ``unloaded: False`` in the evidence — quarantined first,
  unloaded when its tasks release it). A later check that
  finds the model verified again lifts the quarantine and
  records ``failure.model_restored`` — restoring the pinned
  bytes is the operator's act, and the monitor says when
  the store agrees it happened.

Detection has three doors, all honest about their source:

1. ``check_all()`` / its parts (``check_models()``,
   ``check_thermal()``, ``check_memory()``) poll the wired
   components — store re-hash, governor verdict, governor
   readings. A component that is not wired is not polled,
   and its shape is simply not claimed: no store, no
   corrupt-model detection.
2. ``ingest_result()`` / ``ingest_sandbox_report()`` sort a
   finished task's failure by its Y-007 code into a shape.
   Codes outside the four shapes (``queue_full``,
   ``deadline_unmeetable``, ``cancelled_by_caller``, …)
   return None: they are outcomes, not node failures, and
   this monitor does not inflate them into incidents.
3. ``ingest_exception()`` sorts a raised exception the same
   way — by an ``error_code`` attribute when it carries a
   Y-007 name, by type for ``MemoryError``, else by
   ``engine_error`` for an engine-scoped failure the caller
   names with ``engine=``.

Every incident is a validated ``failure_incident`` v1, and
``report()`` answers the validated ``failure_report`` v1:
totals by shape, the quarantines and latches still active,
and every incident in order. Every detection, quarantine,
and clearing is also a Y-013 EventEnvelope from the
``node_agent`` component in the monitor's own ``events`` —
identifiers, shapes, codes, and containment only, never
payload content and never an exception's raw message beyond
the incident's own bounded ``detail`` field.

Honesty: containment here is *bookkeeping with teeth at the
seams this tree owns* — quarantines the router and operator
can read, a resident corrupt model actually unloaded, the
governor's shed named. This module kills no engine process
(no process client exists in the tree — the Part I gate's
banned-import scan stays green), confines no memory at the
OS level, and claims no hardware: detection on named
hardware belongs to the citizens (Y-033 – Y-035) and the
telemetry slice (Y-044).
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.observability import make_event

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

SHAPES = ("engine_crash", "oom", "thermal_throttle", "model_corrupt")

# The Y-007 codes each shape is recognised by. A code in no
# row here is an outcome, not one of Y-032's failure shapes.
_CODE_TO_SHAPE = {
    "oom": "oom",
    "model_hash_mismatch": "model_corrupt",
    "thermal_shed": "thermal_throttle",
    "engine_error": "engine_crash",
    "engine_unreachable": "engine_crash",
}

_SHAPE_ERROR = {
    "engine_crash": "engine_error",
    "oom": "oom",
    "thermal_throttle": "thermal_shed",
    "model_corrupt": "model_hash_mismatch",
}

_CONTAINMENT = {
    "engine_crash": "engine_quarantined",
    "oom": "task_terminated_and_load_shed",
    "thermal_throttle": "load_shed",
    "model_corrupt": "model_quarantined",
}


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


_INCIDENT_VALIDATOR = _validator("failure_incident.schema.json")
_REPORT_VALIDATOR = _validator("failure_report.schema.json")
_ERROR_CODES = set(
    json.loads((_SCHEMAS / "error_codes.schema.json").read_text())["$defs"]["error_code"]["enum"]
)


class FailureError(Exception):
    """Any failure-monitor failure: an unshaped signal, a
    wired component that cannot be read."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise FailureError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_incident(record: dict) -> dict:
    return _validate(_INCIDENT_VALIDATOR, record, "failure incident")


def validate_report(record: dict) -> dict:
    return _validate(_REPORT_VALIDATOR, record, "failure report")


def _empty_evidence() -> dict:
    return {
        "thermal_c": None,
        "memory_fraction": None,
        "memory_used_bytes": None,
        "memory_total_bytes": None,
        "expected_sha256": None,
        "actual_sha256": None,
        "model_status": None,
        "resident": None,
        "unloaded": None,
    }


def classify_error_code(error_code: str | None) -> str | None:
    """The Y-032 shape a Y-007 error code belongs to, or None
    when the code names an outcome outside the four shapes."""
    if error_code is None:
        return None
    return _CODE_TO_SHAPE.get(error_code)


class FailureMonitor:
    """One node's failure monitor: detection, containment, and
    the distinct report for the four shapes.

    ``model_store`` / ``governor`` / ``model_lifecycle`` are the
    node's own wired components (Y-026 / Y-028 / Y-027), polled
    by the ``check_*`` doors; any of them may be absent, and an
    absent component's shape is then detected only through the
    ingest doors. ``now`` is injectable so incident timestamps
    are testable.
    """

    def __init__(
        self,
        node_id: str,
        *,
        model_store=None,
        governor=None,
        model_lifecycle=None,
        now=None,
    ):
        if not isinstance(node_id, str) or not node_id:
            raise FailureError("a failure monitor needs a node_id")
        self.node_id = node_id
        self._store = model_store
        self._governor = governor
        self._lifecycle = model_lifecycle
        self._now = now or (lambda: datetime.now(UTC))
        self._incidents: list[dict] = []
        self._events: list[dict] = []
        self._seq = 0
        self._event_seq = 0
        self._quarantined_engines: set[str] = set()
        # model name -> (status, expected sha, actual sha) at quarantine
        self._quarantined_models: dict[str, tuple] = {}
        self._thermal_active = False
        self._oom_active = False

    # -- facts ----------------------------------------------------------

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    @property
    def incidents(self) -> list[dict]:
        return list(self._incidents)

    @property
    def quarantined_engines(self) -> list[str]:
        return sorted(self._quarantined_engines)

    @property
    def quarantined_models(self) -> list[str]:
        return sorted(self._quarantined_models)

    @property
    def thermal_throttling(self) -> bool:
        return self._thermal_active

    @property
    def oom_active(self) -> bool:
        return self._oom_active

    def engine_available(self, engine: str) -> bool:
        """False only for a quarantined engine — an engine this
        monitor has never seen fail is available."""
        return engine not in self._quarantined_engines

    def model_available(self, model: str) -> bool:
        """False only for a quarantined model."""
        return model not in self._quarantined_models

    # -- internals --------------------------------------------------------

    def _emit(self, type: str, payload: dict, *, task_id: str | None = None) -> dict:
        event = make_event(
            type=type,
            node_id=self.node_id,
            component="node_agent",
            payload=payload,
            seq=self._event_seq,
            task_id=task_id,
            at=self._now(),
        )
        self._event_seq += 1
        self._events.append(event)
        return event

    def _record(
        self,
        shape: str,
        *,
        error_code: str,
        source: str,
        subject: str | None,
        task_id: str | None,
        severity: str,
        containment: str,
        detail: str,
        evidence: dict,
    ) -> dict:
        incident = _validate(
            _INCIDENT_VALIDATOR,
            {
                "version": 1,
                "seq": self._seq,
                "node_id": self.node_id,
                "at": _fmt(self._now()),
                "shape": shape,
                "error_code": error_code,
                "source": source,
                "subject": subject,
                "task_id": task_id,
                "severity": severity,
                "containment": containment,
                "contained": True,
                # The incident's own bounded account of itself;
                # never payload content (ingest doors pass only
                # the failure's name/detail the envelope carried).
                "detail": detail[:500],
                "evidence": evidence,
            },
            "failure incident",
        )
        self._seq += 1
        self._incidents.append(incident)
        self._emit(
            f"failure.{shape}",
            {
                "shape": shape,
                "error_code": error_code,
                "subject": subject,
                "severity": severity,
                "containment": containment,
            },
            task_id=task_id,
        )
        return incident

    # -- the ingest doors: a finished task / a raised failure -------------

    def ingest_exception(
        self,
        exc: BaseException,
        *,
        engine: str | None = None,
        model: str | None = None,
        task_id: str | None = None,
    ) -> dict | None:
        """Sort one raised exception into its shape and contain
        it. ``MemoryError`` is oom by type; an exception carrying
        a Y-007 ``error_code`` is sorted by that code; any other
        exception the caller scopes to an engine (``engine=``)
        is that engine's crash. An unscoped, uncoded exception
        is not one of the four shapes: None."""
        if isinstance(exc, MemoryError):
            shape, code = "oom", "oom"
        else:
            code = getattr(exc, "error_code", None)
            if code not in _ERROR_CODES:
                code = None
            shape = classify_error_code(code)
            if shape is None and engine is not None:
                shape, code = "engine_crash", "engine_error"
            if shape is None:
                return None
        return self._contain_task_shape(
            shape,
            error_code=code or _SHAPE_ERROR[shape],
            engine=engine,
            model=model,
            task_id=task_id,
            detail=str(exc) or (code or shape),
            source="engine",
        )

    def ingest_result(self, result: dict) -> dict | None:
        """Sort a finished ResultEnvelope's failure (Y-007): a
        completed result, and a failure whose code lies outside
        the four shapes, both return None."""
        if not isinstance(result, dict):
            raise FailureError("a result envelope is a dict")
        if result.get("status") == "completed":
            return None
        error = result.get("error") or {}
        code = error.get("code")
        shape = classify_error_code(code)
        if shape is None:
            return None
        provenance = result.get("provenance") or {}
        return self._contain_task_shape(
            shape,
            error_code=code,
            engine=provenance.get("engine"),
            model=provenance.get("model"),
            task_id=result.get("task_id"),
            detail=error.get("detail") or code,
            source="engine",
        )

    def ingest_sandbox_report(
        self, report: dict, *, engine: str | None = None, model: str | None = None
    ) -> dict | None:
        """Sort a Y-029 sandbox report's failure the same way:
        its recorded Y-007 ``error_code`` decides the shape."""
        if not isinstance(report, dict):
            raise FailureError("a sandbox report is a dict")
        code = report.get("error_code")
        shape = classify_error_code(code)
        if shape is None:
            return None
        return self._contain_task_shape(
            shape,
            error_code=code,
            engine=engine,
            model=model,
            task_id=report.get("task_id"),
            detail=f"sandbox task ended {report.get('status')} with {code}",
            source="sandbox",
        )

    def _contain_task_shape(
        self,
        shape: str,
        *,
        error_code: str,
        engine: str | None,
        model: str | None,
        task_id: str | None,
        detail: str,
        source: str,
    ) -> dict:
        evidence = _empty_evidence()
        containment = _CONTAINMENT[shape]
        subject = engine
        if shape == "engine_crash":
            if engine:
                self._quarantined_engines.add(engine)
            severity = "critical"
        elif shape == "oom":
            self._oom_active = True
            severity = "critical"
            subject = engine or model
        elif shape == "thermal_throttle":
            self._thermal_active = True
            severity = "warning"
            subject = None
        else:  # model_corrupt
            severity = "critical"
            subject = model
            if model:
                already = self._quarantined_models.get(model)
                self._quarantined_models[model] = already or ("corrupt", None, None)
                containment = self._unload_if_resident(model, evidence)
        return self._record(
            shape,
            error_code=error_code,
            source=source,
            subject=subject,
            task_id=task_id,
            severity=severity,
            containment=containment,
            detail=detail,
            evidence=evidence,
        )

    def _unload_if_resident(self, model: str, evidence: dict) -> str:
        """A quarantined model's residency, dealt with: when a
        lifecycle is wired, a resident corrupt model is unloaded
        through it; an in-use refusal is recorded honestly in
        the evidence, containment stays quarantine."""
        if self._lifecycle is None:
            evidence["resident"] = None
            evidence["unloaded"] = None
            return "model_quarantined"
        resident = self._lifecycle.is_loaded(model)
        evidence["resident"] = resident
        if not resident:
            evidence["unloaded"] = False
            return "model_quarantined"
        try:
            self._lifecycle.unload(model)
        except Exception:  # noqa: BLE001 — in-use refusal: quarantine stands, unload pending
            evidence["unloaded"] = False
            return "model_quarantined"
        evidence["unloaded"] = True
        return "model_quarantined_and_unloaded"

    # -- the polling doors: the wired components, read now ---------------

    def check_models(self) -> list[dict]:
        """Re-hash the store (Y-026): every model whose status
        is corrupt or missing is quarantined under a new
        incident — once per distinct corruption (name + pinned
        hash + actual hash + status), so re-checking a known
        rot does not spam incidents. A model found verified
        again has its quarantine lifted, with a
        ``failure.model_restored`` event. No store wired: no
        claim, an empty list."""
        if self._store is None:
            return []
        try:
            reports = self._store.verify_all()
        except Exception as exc:
            raise FailureError(f"model store verification failed: {exc}") from exc
        fresh: list[dict] = []
        seen: set[str] = set()
        for report in reports:
            name = report["name"]
            seen.add(name)
            if report["status"] == "verified":
                if name in self._quarantined_models:
                    del self._quarantined_models[name]
                    self._emit(
                        "failure.model_restored",
                        {"shape": "model_corrupt", "subject": name},
                    )
                continue
            key = (report["status"], report["expected_sha256"], report["actual_sha256"])
            if self._quarantined_models.get(name) == key:
                continue  # this exact corruption is already contained
            self._quarantined_models[name] = key
            evidence = _empty_evidence()
            evidence["expected_sha256"] = report["expected_sha256"]
            evidence["actual_sha256"] = report["actual_sha256"]
            evidence["model_status"] = report["status"]
            containment = self._unload_if_resident(name, evidence)
            fresh.append(
                self._record(
                    "model_corrupt",
                    error_code="model_hash_mismatch",
                    source="model_store",
                    subject=name,
                    task_id=None,
                    severity="critical",
                    containment=containment,
                    detail=(
                        f"model {name} is {report['status']} in the store: its bytes "
                        "do not match the hash pinned at registration"
                    ),
                    evidence=evidence,
                )
            )
        return fresh

    def check_thermal(self) -> dict | None:
        """Read the governor's verdict (Y-028): throttling that
        has *started* since the last check is one incident;
        throttling that has ended emits ``failure.thermal_cleared``
        and returns None. No governor wired: no claim, None."""
        if self._governor is None:
            return None
        try:
            state = self._governor.evaluate()
        except Exception as exc:
            raise FailureError(f"governor evaluation failed: {exc}") from exc
        reasons = state["reasons"]
        throttling = "thermal" in reasons or "thermal_hard" in reasons
        if throttling and not self._thermal_active:
            self._thermal_active = True
            evidence = _empty_evidence()
            evidence["thermal_c"] = state["readings"]["thermal_c"]
            return self._record(
                "thermal_throttle",
                error_code="thermal_shed",
                source="governor",
                subject=None,
                task_id=None,
                severity="critical" if "thermal_hard" in reasons else "warning",
                containment="load_shed",
                detail=(
                    "governor reports thermal shedding "
                    f"({', '.join(reasons)}); new load is shed until the node cools"
                ),
                evidence=evidence,
            )
        if not throttling and self._thermal_active:
            self._thermal_active = False
            self._emit(
                "failure.thermal_cleared",
                {"shape": "thermal_throttle", "error_code": "thermal_shed"},
            )
        return None

    def check_memory(self, readings: dict | None = None) -> dict | None:
        """OOM from readings: a reading set in which memory is
        fully consumed (fraction >= 0.99, or used >= total when
        both are named) is an oom incident, latched once until
        a later check sees headroom again (then
        ``failure.oom_cleared``). Readings default to the
        governor's last verdict readings when a governor is
        wired; with neither, no claim, None. A reading set
        whose memory is *unknown* (None) claims nothing."""
        cleaned = readings
        if cleaned is None:
            if self._governor is None:
                return None
            state = self._governor.last_state
            if state is None:
                try:
                    state = self._governor.evaluate()
                except Exception as exc:
                    raise FailureError(f"governor evaluation failed: {exc}") from exc
            cleaned = state["readings"]
        if not isinstance(cleaned, dict):
            raise FailureError("memory readings are a dict")
        used = cleaned.get("memory_used_bytes")
        total = cleaned.get("memory_total_bytes")
        fraction = cleaned.get("memory_fraction")
        if fraction is None and used is not None and total:
            fraction = round(used / total, 4)
        exhausted = fraction is not None and fraction >= 0.99
        if exhausted and not self._oom_active:
            self._oom_active = True
            evidence = _empty_evidence()
            evidence["memory_fraction"] = fraction
            evidence["memory_used_bytes"] = used
            evidence["memory_total_bytes"] = total
            return self._record(
                "oom",
                error_code="oom",
                source="readings",
                subject=None,
                task_id=None,
                severity="critical",
                containment="task_terminated_and_load_shed",
                detail=(
                    f"memory exhausted: fraction {fraction} of the node's memory "
                    "in use; load shedding recommended until headroom returns"
                ),
                evidence=evidence,
            )
        if not exhausted and fraction is not None and self._oom_active:
            # Only a reading that actually measures headroom
            # clears the latch — an unknown reading clears nothing.
            self._oom_active = False
            self._emit("failure.oom_cleared", {"shape": "oom", "error_code": "oom"})
        return None

    def check_all(self, readings: dict | None = None) -> dict:
        """Every polling door, then the report: models, thermal,
        memory — in that order, so a hot node whose model also
        rotted reports both, each under its own shape."""
        self.check_models()
        self.check_thermal()
        self.check_memory(readings)
        return self.report()

    # -- operator doors -----------------------------------------------------

    def clear_engine(self, engine: str) -> bool:
        """Lift one engine's quarantine — the operator's (or a
        later probe's) assertion that the engine is back. False
        when the engine was not quarantined."""
        if engine not in self._quarantined_engines:
            return False
        self._quarantined_engines.discard(engine)
        self._emit(
            "failure.engine_cleared",
            {"shape": "engine_crash", "subject": engine},
        )
        return True

    # -- the report ------------------------------------------------------------

    def report(self) -> dict:
        """The validated failure_report v1: totals by shape,
        the quarantines and latches still active, every
        incident in detection order, and the uncontained
        count — 0 by construction (every incident here records
        its containment), kept in the shape so a future
        uncontained shape must say so in numbers."""
        by_shape = {shape: 0 for shape in SHAPES}
        for incident in self._incidents:
            by_shape[incident["shape"]] += 1
        return _validate(
            _REPORT_VALIDATOR,
            {
                "version": 1,
                "node_id": self.node_id,
                "at": _fmt(self._now()),
                "total_incidents": len(self._incidents),
                "by_shape": by_shape,
                "active": {
                    "quarantined_engines": sorted(self._quarantined_engines),
                    "quarantined_models": sorted(self._quarantined_models),
                    "thermal_throttling": self._thermal_active,
                    "oom_active": self._oom_active,
                },
                "incidents": list(self._incidents),
                "uncontained": 0,
            },
            "failure report",
        )


__all__ = [
    "SHAPES",
    "FailureError",
    "FailureMonitor",
    "classify_error_code",
    "validate_incident",
    "validate_report",
]
