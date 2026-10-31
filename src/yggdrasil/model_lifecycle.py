"""Model lifecycle — Y-027.

The store (Y-026) proves which model files a node *holds*.
This slice decides which of them are *resident*: loaded into
slots, charged against the node's memory budgets, ready for an
engine to serve — and what it costs, in measured time, to
change that residency. Verðandi's warmth math (Y-087: a node
with the model already loaded beats a faster node that must
swap) is only as honest as these numbers, so every cost here
is a timer reading around a real loader call, and a cost that
has never been measured is reported as ``None`` — unknown —
never estimated from a file size or a sibling model.

- **Slots and budgets.** The node offers ``max_slots`` slots
  and two memory pools: the accelerator pool (VRAM budget) and
  the system pool (RAM budget). A loaded model occupies one
  slot and charges its store-pinned ``size_bytes`` against one
  pool. Placement ``"auto"`` follows the accelerator-first law:
  the accelerator pool when the node has one and the model
  fits there, else the system pool, else refusal. An explicit
  placement is honoured exactly — never silently re-placed.
  Budgets may be given directly or derived from a Y-022 census
  with :func:`budgets_from_census` (``vram_gb`` / ``ram_gb``,
  converted at 1024³; a census with no ``vram_gb`` means no
  accelerator pool, budget 0 — absence read as absence).
- **Loading is gated on presence.** A model loads only when
  the store verifies it *now* (Y-026's re-hash): a corrupt,
  missing, or unregistered model is refused by name with its
  true status. Loading an already-loaded model is idempotent —
  the resident record is returned, its measured cost unchanged.
- **Loading is gated on quant fitness when a judge is wired.**
  The optional ``fitness_checker`` seam (Y-038) is called with
  the store entry after presence verifies and before any
  placement or loader call: a verdict that is not fit refuses
  the load by name with every reason the judge named — a
  quant too large for this node, or in a format needing an
  accelerator it lacks, is refused at placement, not
  discovered at crash time. Unwired, no fitness is claimed
  and loading behaves exactly as before.
- **Eviction and swaps.** When a load cannot fit, the manager
  evicts least-recently-used residents of that pool that are
  evictable — not pinned, not in use — measuring each unload.
  A load that displaced at least one resident is a *swap*; the
  swap ledger counts swaps, evictions, and the total measured
  swap time (evict unloads + the incoming load). When nothing
  evictable can make room, the load is refused by name with
  the model's size and the pool's budget in the message.
- **Use leases.** ``acquire`` / ``release`` mark a resident
  model in use (a running task, Y-029, holds one); an in-use
  model is never evicted, and an explicit ``unload`` of an
  in-use model is refused. ``pin`` guards eviction only; the
  operator may always unload a pinned model by name.
- **The loader seam.** Actually moving weights into an engine
  is the loader's act, injected at construction like the
  adapters' transports — an object with ``load(name, *,
  size_bytes, placement) -> handle`` and ``unload(handle, *,
  name, placement)``. The default is :class:`SimulatedLoader`,
  an in-process recorder that moves no weights at all; every
  report carries ``loader_simulated`` from the loader's own
  ``simulated`` attribute, so a residency built on the
  simulated loader is never dressed as a hardware fact.
  Load/unload failures from a loader propagate as
  :class:`ModelLifecycleError` and consume no slot.
- **Facts for Verðandi.** ``report()`` (alias
  ``verdandi_facts()``) answers the validated
  ``model_lifecycle_report`` v1 shape: budgets, usage, the
  resident records, and one swap fact per model the store
  knows — loaded or not — carrying the last *measured* load
  and unload costs and ``swap_in_cost_ms`` (the last measured
  load cost, or ``None`` when the model has never been loaded
  here). Part IV reads this shape; nothing here routes.

Every load, unload, eviction, and refusal is recorded as a
Y-013 EventEnvelope from the ``node_agent`` component in the
manager's own ``events`` list. No network path exists in this
module; the governor that sheds load under heat is Y-028,
the sandbox that runs tasks against resident models is Y-029.
"""

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.model_store import ModelStore, ModelStoreError
from yggdrasil.observability import make_event

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

ACCELERATOR = "accelerator"
SYSTEM = "system"
PLACEMENTS = (ACCELERATOR, SYSTEM)
AUTO = "auto"


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


_REPORT_VALIDATOR = _validator("model_lifecycle_report.schema.json")
_LOADED_VALIDATOR = _validator("loaded_model.schema.json")


class ModelLifecycleError(Exception):
    """Any lifecycle failure: an absent model, an over-budget
    load, an unload of a model in use, a loader failure."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise ModelLifecycleError(f"{kind} fails its schema: {errors[0].message}")
    return record


class SimulatedLoader:
    """The default loader: SIMULATED. Records load/unload calls
    and returns in-process handles; it moves no weights into any
    engine and proves nothing about any hardware. Reports built
    on it carry ``loader_simulated: True``."""

    simulated = True

    def __init__(self):
        self.calls: list[tuple] = []

    def load(self, name: str, *, size_bytes: int, placement: str) -> str:
        self.calls.append(("load", name, placement))
        return f"simulated:{name}:{placement}"

    def unload(self, handle: str, *, name: str, placement: str) -> None:
        self.calls.append(("unload", name, placement))


def budgets_from_census(census: dict, *, max_slots: int) -> dict:
    """Budgets from a Y-022 CapabilityCensus: ``vram_gb`` and
    ``ram_gb`` converted at 1024³ bytes. A census that reports
    no ``vram_gb`` yields an accelerator budget of 0 — the
    census's absence read as absence, never rounded up."""
    gib = 1024**3
    return {
        "vram_bytes": int(census.get("vram_gb", 0) * gib),
        "ram_bytes": int(census.get("ram_gb", 0) * gib),
        "max_slots": max_slots,
    }


@dataclass
class _Resident:
    """One loaded model. ``handle`` is the loader's own token —
    internal bookkeeping, never part of the contract shapes."""

    name: str
    placement: str
    size_bytes: int
    load_ms: float
    loaded_at: datetime
    pinned: bool
    handle: object
    in_use: int = 0
    last_used_seq: int = 0


@dataclass
class _History:
    """The measured past of one model on this node."""

    last_load_ms: float | None = None
    last_unload_ms: float | None = None


class ModelLifecycle:
    """One node's residency manager: slots, budgets, measured
    swap costs. The clock (``now``) and the timer (``timer``,
    seconds-returning, ``time.perf_counter`` by default) are
    injectable so measured costs are testable exactly."""

    def __init__(
        self,
        node_id: str,
        store: ModelStore,
        *,
        vram_bytes: int = 0,
        ram_bytes: int = 0,
        max_slots: int = 4,
        loader=None,
        now=None,
        timer=None,
        fitness_checker=None,
    ):
        if vram_bytes < 0 or ram_bytes < 0:
            raise ModelLifecycleError("memory budgets may not be negative")
        if max_slots < 1:
            raise ModelLifecycleError("a node offers at least one model slot")
        self.node_id = node_id
        self._store = store
        self._budgets = {
            "vram_bytes": int(vram_bytes),
            "ram_bytes": int(ram_bytes),
            "max_slots": int(max_slots),
        }
        self._loader = loader if loader is not None else SimulatedLoader()
        # The quant-fitness judge (Y-038): a callable taking a
        # store entry and returning a verdict dict with a
        # ``fit`` flag and named ``reasons``. None = no fitness
        # gate wired; the manager claims none.
        self._fitness_checker = fitness_checker
        self._now = now or (lambda: datetime.now(UTC))
        self._timer = timer or time.perf_counter
        self._residents: dict[str, _Resident] = {}
        self._history: dict[str, _History] = {}
        self._seq = 0
        self._use_counter = 0
        self._swaps = 0
        self._evictions = 0
        self._total_swap_ms = 0.0
        self._events: list[dict] = []

    # -- facts ----------------------------------------------------------

    @property
    def loader_simulated(self) -> bool:
        return bool(getattr(self._loader, "simulated", False))

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

    def _pool_used(self, placement: str) -> int:
        return sum(
            r.size_bytes for r in self._residents.values() if r.placement == placement
        )

    def _pool_budget(self, placement: str) -> int:
        return self._budgets["vram_bytes" if placement == ACCELERATOR else "ram_bytes"]

    # -- records ----------------------------------------------------------

    def _record(self, resident: _Resident) -> dict:
        return _validate(
            _LOADED_VALIDATOR,
            {
                "name": resident.name,
                "placement": resident.placement,
                "size_bytes": resident.size_bytes,
                "load_ms": resident.load_ms,
                "loaded_at": _fmt(resident.loaded_at),
                "pinned": resident.pinned,
                "in_use": resident.in_use,
            },
            "loaded model record",
        )

    def loaded_models(self) -> list[str]:
        """The resident model names, in load order — the list
        the node agent's health report carries."""
        return [r.name for r in self._residents.values()]

    def resident(self, name: str) -> dict:
        """One resident model's validated record."""
        resident = self._residents.get(name)
        if resident is None:
            raise ModelLifecycleError(f"model {name!r} is not loaded on this node")
        return self._record(resident)

    def is_loaded(self, name: str) -> bool:
        return name in self._residents

    # -- loading ----------------------------------------------------------

    def _fits(self, placement: str, size_bytes: int, *, slots_free: int) -> bool:
        return (
            slots_free >= 1
            and self._pool_used(placement) + size_bytes <= self._pool_budget(placement)
        )

    def _evictable(self, placement: str) -> list[_Resident]:
        """Residents of one pool that may be displaced — not
        pinned, not in use — least recently used first."""
        candidates = [
            r
            for r in self._residents.values()
            if r.placement == placement and not r.pinned and r.in_use == 0
        ]
        return sorted(candidates, key=lambda r: r.last_used_seq)

    def _make_room(self, placement: str, size_bytes: int) -> list[tuple[str, float]]:
        """Evict from one pool until the incoming model fits or
        nothing evictable remains. Returns (name, measured
        unload ms) per eviction, in eviction order."""
        evicted: list[tuple[str, float]] = []
        slots_free = self._budgets["max_slots"] - len(self._residents)
        # A slot shortage is pool-agnostic: the LRU evictable
        # resident overall gives up its slot when the pools
        # themselves have room but no slot is free.
        while not self._fits(placement, size_bytes, slots_free=slots_free):
            if slots_free < 1:
                candidates = sorted(
                    (
                        r
                        for r in self._residents.values()
                        if not r.pinned and r.in_use == 0
                    ),
                    key=lambda r: r.last_used_seq,
                )
            else:
                candidates = self._evictable(placement)
            if not candidates:
                break
            victim = candidates[0]
            unload_ms = self._unload_resident(victim, evicted=True)
            evicted.append((victim.name, unload_ms))
            slots_free = self._budgets["max_slots"] - len(self._residents)
        return evicted

    def _choose_placement(self, requested: str, size_bytes: int) -> str:
        if requested in PLACEMENTS:
            return requested
        if requested != AUTO:
            raise ModelLifecycleError(
                f"placement {requested!r} is not one of: auto, accelerator, system"
            )
        # Accelerator-first: the accelerator pool when the node
        # has one the model could ever fit, else the system pool.
        if self._budgets["vram_bytes"] >= size_bytes:
            return ACCELERATOR
        return SYSTEM

    def load(
        self,
        name: str,
        *,
        placement: str = AUTO,
        pinned: bool = False,
    ) -> dict:
        """Load one store-verified model into a slot, measuring
        the load (and any swap it forces) with the timer. The
        resident record is returned, validated."""
        resident = self._residents.get(name)
        if resident is not None:
            self._touch(resident)
            return self._record(resident)
        try:
            report = self._store.verify(name)
        except ModelStoreError as exc:
            self._refuse(name, str(exc))
            raise ModelLifecycleError(str(exc)) from exc
        if report["status"] != "verified":
            message = (
                f"model {name!r} is {report['status']} in the store — "
                "only hash-verified models load"
            )
            self._refuse(name, message)
            raise ModelLifecycleError(message)
        entry = self._store.entry(name)
        if self._fitness_checker is not None:
            verdict = self._fitness_checker(entry)
            if verdict is not None and not verdict.get("fit", False):
                named = "; ".join(
                    f"{r['code']}: {r['detail']}" for r in verdict.get("reasons", [])
                ) or "judged unfit with no reason named"
                message = (
                    f"model {name!r} quant {entry['quant']!r} is not fit for "
                    f"this node — refused at placement ({named})"
                )
                self._refuse(name, message)
                raise ModelLifecycleError(message)
        size_bytes = entry["size_bytes"]
        pool = self._choose_placement(placement, size_bytes)
        evicted = self._make_room(pool, size_bytes)
        slots_free = self._budgets["max_slots"] - len(self._residents)
        if not self._fits(pool, size_bytes, slots_free=slots_free):
            message = (
                f"model {name!r} ({size_bytes} bytes) does not fit the "
                f"{pool} pool (budget {self._pool_budget(pool)} bytes, "
                f"{self._pool_used(pool)} in use, {slots_free} slot(s) free) "
                "and nothing evictable can make room"
            )
            self._refuse(name, message)
            raise ModelLifecycleError(message)
        start = self._timer()
        try:
            handle = self._loader.load(name, size_bytes=size_bytes, placement=pool)
        except Exception as exc:
            raise ModelLifecycleError(f"loader failed to load {name!r}: {exc}") from exc
        load_ms = round((self._timer() - start) * 1000.0, 3)
        resident = _Resident(
            name=name,
            placement=pool,
            size_bytes=size_bytes,
            load_ms=load_ms,
            loaded_at=self._now(),
            pinned=pinned,
            handle=handle,
        )
        self._residents[name] = resident
        self._touch(resident)
        history = self._history.setdefault(name, _History())
        history.last_load_ms = load_ms
        if evicted:
            self._swaps += 1
            swap_ms = round(load_ms + sum(ms for _, ms in evicted), 3)
            self._total_swap_ms = round(self._total_swap_ms + swap_ms, 3)
            self._emit(
                "model.swapped",
                {
                    "model": name,
                    "placement": pool,
                    "evicted": [n for n, _ in evicted],
                    "swap_ms": swap_ms,
                    "loader_simulated": self.loader_simulated,
                },
            )
        self._emit(
            "model.loaded",
            {
                "model": name,
                "placement": pool,
                "size_bytes": size_bytes,
                "load_ms": load_ms,
                "pinned": pinned,
                "loader_simulated": self.loader_simulated,
            },
        )
        return self._record(resident)

    def _refuse(self, name: str, reason: str) -> None:
        self._emit("model.load_refused", {"model": name, "reason": reason})

    def _touch(self, resident: _Resident) -> None:
        self._use_counter += 1
        resident.last_used_seq = self._use_counter

    # -- unloading ----------------------------------------------------------

    def _unload_resident(self, resident: _Resident, *, evicted: bool) -> float:
        start = self._timer()
        try:
            self._loader.unload(
                resident.handle, name=resident.name, placement=resident.placement
            )
        except Exception as exc:
            raise ModelLifecycleError(
                f"loader failed to unload {resident.name!r}: {exc}"
            ) from exc
        unload_ms = round((self._timer() - start) * 1000.0, 3)
        del self._residents[resident.name]
        history = self._history.setdefault(resident.name, _History())
        history.last_unload_ms = unload_ms
        if evicted:
            self._evictions += 1
            self._emit(
                "model.evicted",
                {
                    "model": resident.name,
                    "placement": resident.placement,
                    "unload_ms": unload_ms,
                    "loader_simulated": self.loader_simulated,
                },
            )
        else:
            self._emit(
                "model.unloaded",
                {
                    "model": resident.name,
                    "placement": resident.placement,
                    "unload_ms": unload_ms,
                    "loader_simulated": self.loader_simulated,
                },
            )
        return unload_ms

    def unload(self, name: str) -> dict:
        """Unload one resident model, measuring the unload. An
        in-use model refuses by name — its tasks finish first.
        The measured record of the unload is returned."""
        resident = self._residents.get(name)
        if resident is None:
            raise ModelLifecycleError(f"model {name!r} is not loaded on this node")
        if resident.in_use > 0:
            raise ModelLifecycleError(
                f"model {name!r} is in use by {resident.in_use} task(s) — "
                "it unloads when they release it"
            )
        record = self._record(resident)
        unload_ms = self._unload_resident(resident, evicted=False)
        record["unload_ms"] = unload_ms
        return record

    # -- use leases ----------------------------------------------------------

    def acquire(self, name: str) -> dict:
        """Mark a resident model in use (a task is running
        against it): it cannot be evicted or unloaded until
        released. Returns the resident record."""
        resident = self._residents.get(name)
        if resident is None:
            raise ModelLifecycleError(
                f"model {name!r} is not loaded on this node — nothing to acquire"
            )
        resident.in_use += 1
        self._touch(resident)
        return self._record(resident)

    def release(self, name: str) -> dict:
        """Release one use of a resident model."""
        resident = self._residents.get(name)
        if resident is None:
            raise ModelLifecycleError(f"model {name!r} is not loaded on this node")
        if resident.in_use == 0:
            raise ModelLifecycleError(f"model {name!r} has no use to release")
        resident.in_use -= 1
        return self._record(resident)

    # -- the report Verðandi reads ------------------------------------------------

    def report(self) -> dict:
        """The validated model_lifecycle_report v1: budgets,
        usage, residents, and one swap fact per model the store
        knows. A swap-in cost is the last *measured* load cost
        or None — never an estimate."""
        known: list[dict] = []
        for entry in self._store.entries():
            name = entry["name"]
            resident = self._residents.get(name)
            history = self._history.get(name, _History())
            known.append(
                {
                    "name": name,
                    "loaded": resident is not None,
                    "placement": resident.placement if resident else None,
                    "size_bytes": entry["size_bytes"],
                    "swap_in_cost_ms": history.last_load_ms,
                    "last_load_ms": history.last_load_ms,
                    "last_unload_ms": history.last_unload_ms,
                }
            )
        record = {
            "node_id": self.node_id,
            "at": _fmt(self._now()),
            "loader_simulated": self.loader_simulated,
            "budgets": dict(self._budgets),
            "used": {
                "vram_bytes": self._pool_used(ACCELERATOR),
                "ram_bytes": self._pool_used(SYSTEM),
                "slots": len(self._residents),
            },
            "loaded": [self._record(r) for r in self._residents.values()],
            "known_models": known,
            "swaps": self._swaps,
            "evictions": self._evictions,
            "total_swap_ms": self._total_swap_ms,
        }
        return _validate(_REPORT_VALIDATOR, record, "model lifecycle report")

    def verdandi_facts(self) -> dict:
        """``report()`` under the name Part IV asks by: the
        residency + swap-cost facts Verðandi places by."""
        return self.report()


__all__ = [
    "ACCELERATOR",
    "AUTO",
    "SYSTEM",
    "ModelLifecycle",
    "ModelLifecycleError",
    "SimulatedLoader",
    "budgets_from_census",
]
