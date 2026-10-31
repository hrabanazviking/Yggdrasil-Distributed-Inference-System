"""Multi-engine node — Y-045.

Until this slice a node agent could be *told about* several
engines (the census carries a list, Y-022), but nothing in the
tree owned the fact that they live on the **same node**, sharing
the same slots and the same memory. An Aesir residency and an
Ollama residency were two private stories; a task naming a model
was handed to whichever adapter the caller happened to hold,
and no record said which engine a resident model belonged to.

This slice is that owner: :class:`MultiEngineHost`.

- **Side by side, each in its own words.** The host holds the
  node's engine adapters (Y-023 – Y-025) by their own
  ``engine`` names — two adapters claiming one name are refused
  at construction. ``census_engines()`` answers every hosted
  adapter's own validated discovery, in hosting order, for the
  Y-022 collector's engines seam: Aesir's ledger grade and
  Ollama's unproven grade ride side by side, neither averaged
  into the other. ``census_models()`` answers the *union* of
  the engines' model lists for the collector's models seam —
  while the report keeps each engine's list separate, so a
  model two engines both list is two listings in the report
  and one name in the census, never silently merged where the
  difference matters.
- **Slots accounted across engines.** Residency still belongs
  to the one model lifecycle (Y-027): its ``max_slots`` and its
  VRAM/RAM pools are the node's, shared. The host adds the
  missing attribution — ``assign(model, engine)`` loads the
  model through the lifecycle (hash-verified presence, quant
  fitness, budgets, measured cost: all Y-027/Y-038's gates,
  unchanged) and records a validated ``engine_assignment`` v1
  naming the engine that residency serves. A model resident
  for one engine cannot be assigned to another — one residency,
  one engine; unassign it first — and assigning to an engine
  that does not list the model is refused by name before any
  slot is spent: an engine is never dressed with a model its
  own surface does not name. When the shared slots run out,
  Y-027's eviction and refusal rules decide, across engines
  alike — an Ollama load can evict an Aesir residency under the
  same LRU law, and the report's per-engine ``slots_used``
  always sums to the node's ``slots.used``.
- **Tasks routed by the model's own facts.** ``run_task``
  picks the engine a task runs on: an explicitly named engine
  wins (and must be hosted, and must list the task's model);
  else the model's assignment decides; else the one hosted
  engine that lists the model; else the task is refused by
  name — ``model_not_present`` when no hosted engine lists it,
  and an ambiguity refusal naming every candidate when several
  engines list an unassigned model. Choosing between engines
  that could both serve is Verðandi's judgement (Part IV);
  this host refuses to guess it. The chosen adapter's own
  ``run_task`` serves the task — its refusals (Aesir's
  resident-only rule, either engine's unservable kinds) stay
  the engine's own, under its own Y-007 names — and the host
  counts each run against the engine that ran it (completed /
  failed / refused, from the result envelope's own status).
- **The report is the node's whole engine story.**
  ``report()`` answers the validated ``multi_engine_report``
  v1: per-engine discovery as the engine itself reports it
  *now*, its model list (an unreachable engine lists nothing —
  its discovery already says why), its slot share and run
  counts, the shared slot ledger, every assignment, and
  ``loader_simulated`` carried from the lifecycle, so a
  residency built on the simulated loader is never dressed
  as a hardware fact.

Every assignment, unassignment, and routing refusal is
recorded as a Y-013 EventEnvelope from the ``node_agent``
component in the host's own ``events`` list (names and counts
only, never payload content). The node agent grows the seam
at this slice: ``multi_engine=`` at construction,
``assign_model_to_engine`` / ``unassign_model`` /
``multi_engine_report()``, and ``status()`` carries the
report. Without a host wired, the agent accounts nothing
across engines, exactly as before.

Honesty: the host wires adapters it is *handed* — transports
are deployment wiring (the socket-carrying seam of Y-023 –
Y-025), so no citizen factory wires a host at this slice and
no live Aesir or Ollama server is claimed here. The in-tree
acceptance drives the real adapters over fake transports
speaking Aesir's and Ollama's own wire shapes. Discovery is
asked fresh on every report and routing decision — the host
keeps no cached picture of an engine that may have died.
"""

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.model_lifecycle import ModelLifecycle, ModelLifecycleError
from yggdrasil.observability import make_event

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"


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


_ASSIGNMENT_VALIDATOR = _validator("engine_assignment.schema.json")
_REPORT_VALIDATOR = _validator("multi_engine_report.schema.json")


class MultiEngineError(Exception):
    """Any multi-engine failure: a duplicate engine, an
    assignment an engine's own list does not support, a task
    no hosted engine can be chosen for."""

    def __init__(self, message: str, *, error_code: str | None = None):
        super().__init__(message)
        self.error_code = error_code


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise MultiEngineError(f"{kind} fails its schema: {errors[0].message}")
    return record


def _task_model(task: dict) -> str | None:
    """The model a task names — the adapters' own reading
    (Y-023/Y-024): ``requirements.model``, else a payload
    ``model`` string."""
    requirements = task.get("requirements") or {}
    if requirements.get("model"):
        return requirements["model"]
    payload = task.get("payload") or {}
    return payload.get("model") if isinstance(payload.get("model"), str) else None


class MultiEngineHost:
    """One node's engines, hosted side by side over one shared
    model lifecycle. ``adapters`` are the engine adapters
    (Y-023 – Y-025) in hosting order; ``lifecycle`` is the
    node's Y-027 residency manager, whose slots and pools are
    the shared budgets every assignment charges. The clock
    (``now``) is injectable so timestamps are testable."""

    def __init__(
        self,
        node_id: str,
        adapters,
        lifecycle: ModelLifecycle,
        *,
        now=None,
    ):
        adapters = list(adapters)
        if not adapters:
            raise MultiEngineError("a multi-engine host hosts at least one engine")
        seen: set[str] = set()
        for adapter in adapters:
            name = getattr(adapter, "engine", None)
            if not isinstance(name, str) or not name:
                raise MultiEngineError(
                    "every hosted adapter must name its engine (adapter.engine)"
                )
            if name in seen:
                raise MultiEngineError(
                    f"engine {name!r} is hosted twice — one node, one adapter per engine"
                )
            seen.add(name)
        if lifecycle.node_id != node_id:
            raise MultiEngineError(
                f"the lifecycle belongs to node {lifecycle.node_id!r}, not {node_id!r}"
            )
        self.node_id = node_id
        self._adapters = adapters
        self._by_engine = {a.engine: a for a in adapters}
        self._lifecycle = lifecycle
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.Lock()
        self._assignments: dict[str, dict] = {}  # model -> assignment record
        self._runs: dict[str, dict[str, int]] = {
            a.engine: {"run": 0, "completed": 0, "failed": 0, "refused": 0}
            for a in adapters
        }
        self._seq = 0
        self._events: list[dict] = []

    # -- facts ----------------------------------------------------------

    @property
    def engine_names(self) -> list[str]:
        """The hosted engine names, in hosting order."""
        return [a.engine for a in self._adapters]

    @property
    def lifecycle(self) -> ModelLifecycle:
        return self._lifecycle

    @property
    def events(self) -> list[dict]:
        return list(self._events)

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

    # -- discovery (asked fresh, never cached) ----------------------------

    def _discovery(self, adapter) -> dict:
        """One adapter's own discovery, as it reports it now.
        An adapter whose discovery itself raises has failed in
        a way its contract never allows (unreachable is a
        returned finding in Y-023 – Y-025), so it propagates
        named as this engine's error."""
        try:
            return adapter.discover()
        except Exception as exc:
            raise MultiEngineError(
                f"engine {adapter.engine!r} failed discovery: {exc}"
            ) from exc

    def _models_listed(self, adapter, discovery: dict) -> list[str]:
        """One engine's own model list. An unreachable engine
        lists nothing — its discovery already says why, and the
        host does not interrogate a dead server for its shelf.
        A reachable engine whose listing fails is named."""
        if not discovery.get("reachable"):
            return []
        try:
            return list(adapter.list_models())
        except Exception as exc:
            raise MultiEngineError(
                f"engine {adapter.engine!r} is reachable but its model "
                f"list could not be read: {exc}"
            ) from exc

    def census_engines(self) -> list[dict]:
        """Every hosted engine's validated discovery, in
        hosting order — the Y-022 collector's engines seam,
        filled by all of this node's engines at once."""
        return [self._discovery(a) for a in self._adapters]

    def census_models(self) -> list[str]:
        """The union of the hosted engines' model lists, in
        hosting order, each name once — the Y-022 collector's
        models seam. Per-engine lists (where a name listed by
        two engines stays two listings) live in ``report()``."""
        names: list[str] = []
        for adapter in self._adapters:
            for name in self._models_listed(adapter, self._discovery(adapter)):
                if name not in names:
                    names.append(name)
        return names

    # -- assignment: slots accounted across engines -------------------------

    def assign(
        self,
        model: str,
        engine: str,
        *,
        placement: str = "auto",
        pinned: bool = False,
    ) -> dict:
        """Assign one model to one engine: the engine must be
        hosted and must list the model on its own surface; the
        load itself is the lifecycle's (hash-verified, budgeted,
        measured, evicting under the shared LRU law). Returns
        the validated ``engine_assignment`` v1. Re-assigning a
        resident model to its own engine is idempotent; to
        another engine it is refused — unassign first."""
        adapter = self._by_engine.get(engine)
        if adapter is None:
            raise MultiEngineError(
                f"engine {engine!r} is not hosted on this node "
                f"(hosted: {', '.join(self.engine_names)})",
                error_code="no_capable_node",
            )
        with self._lock:
            existing = self._assignments.get(model)
            if existing is not None:
                if existing["engine"] == engine:
                    return dict(existing)
                raise MultiEngineError(
                    f"model {model!r} is already assigned to engine "
                    f"{existing['engine']!r} on this node — one residency, "
                    "one engine; unassign it first",
                    error_code="no_capable_node",
                )
            listed = self._models_listed(adapter, self._discovery(adapter))
            if model not in listed:
                raise MultiEngineError(
                    f"engine {engine!r} does not list model {model!r} — "
                    "an engine is never assigned a model its own surface "
                    "does not name",
                    error_code="model_not_present",
                )
            try:
                resident = self._lifecycle.load(
                    model, placement=placement, pinned=pinned
                )
            except ModelLifecycleError as exc:
                raise MultiEngineError(str(exc)) from exc
            record = _validate(
                _ASSIGNMENT_VALIDATOR,
                {
                    "version": 1,
                    "engine": engine,
                    "model": model,
                    "placement": resident["placement"],
                    "size_bytes": resident["size_bytes"],
                    "load_ms": resident["load_ms"],
                    "loaded_at": resident["loaded_at"],
                    "assigned_at": _fmt(self._now()),
                    "pinned": resident["pinned"],
                    "in_use": resident["in_use"],
                },
                "engine assignment",
            )
            self._assignments[model] = record
            self._emit(
                "multi_engine.model_assigned",
                {
                    "model": model,
                    "engine": engine,
                    "placement": record["placement"],
                    "size_bytes": record["size_bytes"],
                },
            )
            return dict(record)

    def unassign(self, model: str) -> dict:
        """Unassign one model: the lifecycle unloads it (an
        in-use model refuses there, by name, and keeps its
        assignment) and the assignment record is returned as
        it stood."""
        with self._lock:
            record = self._assignments.get(model)
            if record is None:
                raise MultiEngineError(
                    f"model {model!r} has no engine assignment on this node"
                )
            if self._lifecycle.is_loaded(model):
                try:
                    self._lifecycle.unload(model)
                except ModelLifecycleError as exc:
                    raise MultiEngineError(str(exc)) from exc
            # A residency already evicted under the shared LRU
            # law holds no slot: the assignment simply ends.
            del self._assignments[model]
            self._emit(
                "multi_engine.model_unassigned",
                {"model": model, "engine": record["engine"]},
            )
            return dict(record)

    def assignment(self, model: str) -> dict | None:
        """One model's assignment record, refreshed against the
        lifecycle's live resident record (in_use moves after
        assignment), or None when the model is unassigned."""
        record = self._assignments.get(model)
        if record is None:
            return None
        return self._refresh(record)

    def _refresh(self, record: dict) -> dict:
        """An assignment with its resident facts read live from
        the lifecycle — a residency evicted by another engine's
        load under the shared LRU law drops its assignment
        rather than leaving a record that claims a slot the
        model no longer holds."""
        try:
            resident = self._lifecycle.resident(record["model"])
        except ModelLifecycleError:
            self._assignments.pop(record["model"], None)
            return None  # type: ignore[return-value]
        refreshed = dict(record)
        refreshed["in_use"] = resident["in_use"]
        refreshed["pinned"] = resident["pinned"]
        return _validate(_ASSIGNMENT_VALIDATOR, refreshed, "engine assignment")

    def assignments(self) -> list[dict]:
        """Every live assignment, in assignment order —
        evicted residencies drop out (see ``_refresh``)."""
        live: list[dict] = []
        for model in list(self._assignments):
            record = self._refresh(self._assignments[model])
            if record is not None:
                live.append(record)
        return live

    # -- routing and running --------------------------------------------------

    def route(self, task: dict, *, engine: str | None = None) -> str:
        """The engine a task runs on, chosen by the model's own
        facts (see the module docstring for the order). A task
        naming no model can only be routed by an explicitly
        named engine. Refusals are named MultiEngineErrors with
        their Y-007 codes; nothing is guessed."""
        if not isinstance(task, dict) or not isinstance(task.get("id"), str):
            raise MultiEngineError("run_task needs a task envelope with an id")
        model = _task_model(task)
        if engine is not None:
            adapter = self._by_engine.get(engine)
            if adapter is None:
                self._refuse_route(task, model, engine, "no_capable_node")
                raise MultiEngineError(
                    f"engine {engine!r} is not hosted on this node "
                    f"(hosted: {', '.join(self.engine_names)})",
                    error_code="no_capable_node",
                )
            if model is not None:
                listed = self._models_listed(adapter, self._discovery(adapter))
                if model not in listed:
                    self._refuse_route(task, model, engine, "model_not_present")
                    raise MultiEngineError(
                        f"engine {engine!r} does not list model {model!r}",
                        error_code="model_not_present",
                    )
            return engine
        if model is None:
            self._refuse_route(task, None, None, "no_capable_node")
            raise MultiEngineError(
                "the task names no model and no engine was given — "
                "there is nothing to route by",
                error_code="no_capable_node",
            )
        assigned = self.assignment(model)
        if assigned is not None:
            return assigned["engine"]
        candidates = [
            a.engine
            for a in self._adapters
            if model in self._models_listed(a, self._discovery(a))
        ]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            self._refuse_route(task, model, None, "model_not_present")
            raise MultiEngineError(
                f"no hosted engine lists model {model!r} "
                f"(hosted: {', '.join(self.engine_names)})",
                error_code="model_not_present",
            )
        self._refuse_route(task, model, None, "no_capable_node")
        raise MultiEngineError(
            f"model {model!r} is listed by several hosted engines "
            f"({', '.join(candidates)}) and assigned to none — "
            "choosing between them is the router's judgement (Part IV), "
            "not this host's guess",
            error_code="no_capable_node",
        )

    def _refuse_route(
        self, task: dict, model: str | None, engine: str | None, code: str
    ) -> None:
        self._emit(
            "multi_engine.route_refused",
            {
                "task_id": task.get("id"),
                "model": model,
                "engine": engine,
                "error_code": code,
            },
        )

    def run_task(self, task: dict, *, engine: str | None = None):
        """Route one task and run it on the chosen engine's own
        ``run_task`` door. Returns the adapter's
        (ResultEnvelope, StreamEvents) pair unchanged; the run
        is counted against the engine that ran it, by the
        result's own status."""
        chosen = self.route(task, engine=engine)
        adapter = self._by_engine[chosen]
        result, events = adapter.run_task(task, self.node_id)
        counts = self._runs[chosen]
        counts["run"] += 1
        status = result.get("status")
        if status in ("completed", "failed", "refused"):
            counts[status] += 1
        return result, events

    # -- the report -------------------------------------------------------------

    def report(self) -> dict:
        """The validated ``multi_engine_report`` v1: every
        hosted engine's fresh discovery and model list, its
        slot share (which always sums to the shared ledger),
        its run counts, every live assignment, and the
        lifecycle's ``loader_simulated`` carried honestly."""
        live_assignments = self.assignments()
        lifecycle_report = self._lifecycle.report()
        engines: list[dict] = []
        listings = 0
        reachable = 0
        for adapter in self._adapters:
            discovery = self._discovery(adapter)
            listed = self._models_listed(adapter, discovery)
            listings += len(listed)
            if discovery.get("reachable"):
                reachable += 1
            engines.append(
                {
                    "engine": adapter.engine,
                    "discovery": discovery,
                    "models_listed": listed,
                    "slots_used": sum(
                        1 for a in live_assignments if a["engine"] == adapter.engine
                    ),
                    "tasks_run": self._runs[adapter.engine]["run"],
                    "tasks_completed": self._runs[adapter.engine]["completed"],
                    "tasks_failed": self._runs[adapter.engine]["failed"],
                    "tasks_refused": self._runs[adapter.engine]["refused"],
                }
            )
        used = lifecycle_report["used"]["slots"]
        record = {
            "version": 1,
            "node_id": self.node_id,
            "at": _fmt(self._now()),
            "engines": engines,
            "slots": {
                "max_slots": lifecycle_report["budgets"]["max_slots"],
                "used": used,
                "free": lifecycle_report["budgets"]["max_slots"] - used,
            },
            "assignments": live_assignments,
            "totals": {
                "engines_hosted": len(self._adapters),
                "engines_reachable": reachable,
                "listings": listings,
                "assignments": len(live_assignments),
                "tasks_run": sum(c["run"] for c in self._runs.values()),
            },
            "loader_simulated": lifecycle_report["loader_simulated"],
        }
        return _validate(_REPORT_VALIDATOR, record, "multi-engine report")


__all__ = [
    "MultiEngineError",
    "MultiEngineHost",
]
