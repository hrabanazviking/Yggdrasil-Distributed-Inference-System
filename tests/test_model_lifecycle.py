"""Model lifecycle tests — Y-027's acceptance.

The lifecycle must prove: (a) residency is budgeted — a load
lands in a slot and charges its store-pinned size against the
accelerator (VRAM) or system (RAM) pool, auto placement is
accelerator-first, an explicit placement is honoured exactly,
and budgets derive from a census with absence read as absence;
(b) loading is gated on presence — only a model the store
verifies *now* loads; corrupt, missing, and unregistered
models are refused by name with their true status; (c) costs
are measured, never estimated — load/unload times are timer
readings around the loader calls, a swap (a load that evicted)
is counted with its total measured cost, and a model never
loaded reports swap_in_cost_ms as None; (d) eviction is LRU
among the evictable — pinned and in-use models are never
evicted, an in-use model refuses explicit unload, and a load
that cannot fit even after eviction is refused by name with
its size and the pool budget; (e) the report is the validated
contract shape and carries loader_simulated honestly; (f) the
node agent integration — health() lists resident models once a
lifecycle is wired; (g) the founding README is untouched.
"""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.identity import (
    LocalKeyStore,
    NodeKeypair,
    create_mesh_root,
    issue_node_certificate,
)
from yggdrasil.model_lifecycle import (
    ModelLifecycle,
    ModelLifecycleError,
    SimulatedLoader,
    budgets_from_census,
)
from yggdrasil.model_store import ModelStore
from yggdrasil.node_agent import NodeAgent, NodeAgentError, NodeConfig

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
NODE_ID = "test-node-1"
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"


class Clock:
    """A hand-advanced clock: the timer readings are exact."""

    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class TimedLoader:
    """A loader that 'takes' a fixed time per call, by advancing
    the shared clock — measured costs are known exactly."""

    simulated = False

    def __init__(self, clock: Clock, *, load_s=0.25, unload_s=0.05, fail_on=()):
        self._clock = clock
        self._load_s = load_s
        self._unload_s = unload_s
        self._fail_on = set(fail_on)
        self.calls: list[tuple] = []

    def load(self, name, *, size_bytes, placement):
        if name in self._fail_on:
            raise RuntimeError("engine refused the load")
        self._clock.advance(self._load_s)
        self.calls.append(("load", name, placement))
        return f"handle:{name}"

    def unload(self, handle, *, name, placement):
        self._clock.advance(self._unload_s)
        self.calls.append(("unload", name, placement))


def make_store(tmp_path, sizes: dict[str, int]) -> ModelStore:
    store = ModelStore(tmp_path / "store", now=lambda: FIXED)
    for name, size in sizes.items():
        source = tmp_path / f"{name}.gguf"
        source.write_bytes(bytes([7]) * size)
        store.add(
            source,
            name=name,
            family="fixture",
            quant="Q4_K_M",
            format="gguf",
        )
    return store


def make_lifecycle(tmp_path, sizes, clock=None, loader=None, **budgets):
    store = make_store(tmp_path, sizes)
    clock = clock or Clock()
    loader = loader if loader is not None else TimedLoader(clock)
    budgets.setdefault("vram_bytes", 0)
    budgets.setdefault("ram_bytes", 10_000)
    budgets.setdefault("max_slots", 4)
    manager = ModelLifecycle(
        NODE_ID, store, loader=loader, now=lambda: FIXED, timer=clock, **budgets
    )
    return manager, store, clock


# -- placement and budgets -------------------------------------------------


def test_auto_placement_is_accelerator_first_with_measured_cost(tmp_path):
    manager, _, _ = make_lifecycle(tmp_path, {"alpha": 100}, vram_bytes=1_000)
    record = manager.load("alpha")
    assert record["placement"] == "accelerator"
    assert record["size_bytes"] == 100
    assert record["load_ms"] == 250.0  # the loader's 0.25 s, measured
    assert record["loaded_at"] == "2026-06-01T12:00:00Z"
    assert manager.loaded_models() == ["alpha"]


def test_auto_falls_to_system_when_no_accelerator_pool_fits(tmp_path):
    manager, _, _ = make_lifecycle(tmp_path, {"alpha": 500}, vram_bytes=100)
    record = manager.load("alpha")
    assert record["placement"] == "system"


def test_explicit_placement_honoured_and_refused_exactly(tmp_path):
    manager, _, _ = make_lifecycle(tmp_path, {"alpha": 100}, vram_bytes=0)
    record = manager.load("alpha", placement="system")
    assert record["placement"] == "system"
    sub = tmp_path / "b"
    sub.mkdir()
    manager2, _, _ = make_lifecycle(sub, {"alpha": 100}, vram_bytes=0)
    with pytest.raises(ModelLifecycleError, match="accelerator pool"):
        manager2.load("alpha", placement="accelerator")


def test_budgets_from_census_reads_absence_as_absence():
    gib = 1024**3
    budgets = budgets_from_census({"ram_gb": 16.0, "vram_gb": 8.0}, max_slots=3)
    assert budgets == {"ram_bytes": 16 * gib, "vram_bytes": 8 * gib, "max_slots": 3}
    budgets = budgets_from_census({"ram_gb": 4.0}, max_slots=1)
    assert budgets["vram_bytes"] == 0


# -- presence gate ------------------------------------------------------------


def test_unregistered_corrupt_and_missing_models_refused_by_name(tmp_path):
    manager, store, _ = make_lifecycle(tmp_path, {"alpha": 64, "beta": 64})
    with pytest.raises(ModelLifecycleError, match="not registered"):
        manager.load("ghost")
    # Tamper with beta's stored copy: corrupt, and refused as such.
    entry = store.entry("beta")
    (store.root / entry["path"]).write_bytes(b"x" * 64)
    with pytest.raises(ModelLifecycleError, match="corrupt"):
        manager.load("beta")
    # Delete alpha's copy: missing, and refused as such.
    (store.root / store.entry("alpha")["path"]).unlink()
    with pytest.raises(ModelLifecycleError, match="missing"):
        manager.load("alpha")
    assert manager.loaded_models() == []
    refused = [e for e in manager.events if e["type"] == "model.load_refused"]
    assert len(refused) == 3


def test_load_is_idempotent_and_does_not_remeasure(tmp_path):
    clock = Clock()
    manager, _, _ = make_lifecycle(tmp_path, {"alpha": 100}, clock=clock)
    first = manager.load("alpha")
    clock.advance(10.0)  # time passes; a second load must not re-charge
    second = manager.load("alpha")
    assert second["load_ms"] == first["load_ms"] == 250.0
    assert manager.report()["used"]["slots"] == 1


# -- eviction and swaps ---------------------------------------------------------


def test_lru_eviction_counts_a_measured_swap(tmp_path):
    manager, _, _ = make_lifecycle(
        tmp_path, {"a": 100, "b": 100, "c": 100}, vram_bytes=250
    )
    manager.load("a")
    manager.load("b")
    manager.load("a")  # touch a: b is now least recently used
    record = manager.load("c")
    assert record["placement"] == "accelerator"
    assert manager.loaded_models() == ["a", "c"]
    report = manager.report()
    assert report["swaps"] == 1
    assert report["evictions"] == 1
    # swap cost = b's unload (50 ms) + c's load (250 ms), measured
    assert report["total_swap_ms"] == 300.0
    fact_b = next(m for m in report["known_models"] if m["name"] == "b")
    assert fact_b["loaded"] is False
    assert fact_b["last_unload_ms"] == 50.0
    assert fact_b["swap_in_cost_ms"] == 250.0  # b's own measured load


def test_slot_shortage_evicts_even_when_pools_have_room(tmp_path):
    manager, _, _ = make_lifecycle(
        tmp_path, {"a": 10, "b": 10}, max_slots=1, ram_bytes=10_000
    )
    manager.load("a")
    manager.load("b")
    assert manager.loaded_models() == ["b"]
    assert manager.report()["swaps"] == 1


def test_pinned_and_in_use_models_are_never_evicted(tmp_path):
    manager, _, _ = make_lifecycle(
        tmp_path, {"a": 100, "b": 100, "c": 100}, vram_bytes=250
    )
    manager.load("a", pinned=True)
    manager.load("b")
    manager.acquire("b")
    with pytest.raises(ModelLifecycleError, match="does not fit"):
        manager.load("c")
    assert manager.loaded_models() == ["a", "b"]


def test_over_budget_refusal_names_size_and_budget(tmp_path):
    manager, _, _ = make_lifecycle(tmp_path, {"huge": 5000}, ram_bytes=1000)
    with pytest.raises(ModelLifecycleError, match=r"5000 bytes.*budget 1000"):
        manager.load("huge", placement="system")


# -- unload and use leases -------------------------------------------------------


def test_unload_measures_and_in_use_unload_refuses(tmp_path):
    manager, _, _ = make_lifecycle(tmp_path, {"alpha": 100})
    manager.load("alpha")
    manager.acquire("alpha")
    with pytest.raises(ModelLifecycleError, match="in use"):
        manager.unload("alpha")
    manager.release("alpha")
    record = manager.unload("alpha")
    assert record["unload_ms"] == 50.0
    assert manager.loaded_models() == []
    with pytest.raises(ModelLifecycleError, match="not loaded"):
        manager.unload("alpha")


def test_acquire_release_guards(tmp_path):
    manager, _, _ = make_lifecycle(tmp_path, {"alpha": 100})
    with pytest.raises(ModelLifecycleError, match="not loaded"):
        manager.acquire("alpha")
    manager.load("alpha")
    with pytest.raises(ModelLifecycleError, match="no use to release"):
        manager.release("alpha")
    assert manager.acquire("alpha")["in_use"] == 1
    assert manager.release("alpha")["in_use"] == 0


def test_loader_failure_consumes_no_slot(tmp_path):
    clock = Clock()
    loader = TimedLoader(clock, fail_on={"bad"})
    manager, _, _ = make_lifecycle(
        tmp_path, {"bad": 10, "good": 10}, clock=clock, loader=loader
    )
    with pytest.raises(ModelLifecycleError, match="loader failed"):
        manager.load("bad")
    assert manager.loaded_models() == []
    manager.load("good")
    assert manager.loaded_models() == ["good"]


# -- the report Verðandi reads -----------------------------------------------------


def test_report_shape_and_unmeasured_costs_are_null(tmp_path):
    manager, _, _ = make_lifecycle(tmp_path, {"alpha": 100, "beta": 200})
    manager.load("alpha")
    report = manager.verdandi_facts()
    assert report["loader_simulated"] is False  # TimedLoader says so itself
    assert report["budgets"] == {"vram_bytes": 0, "ram_bytes": 10_000, "max_slots": 4}
    assert report["used"] == {"vram_bytes": 0, "ram_bytes": 100, "slots": 1}
    facts = {m["name"]: m for m in report["known_models"]}
    assert facts["alpha"]["swap_in_cost_ms"] == 250.0
    assert facts["alpha"]["loaded"] is True
    assert facts["beta"]["swap_in_cost_ms"] is None  # never measured, not estimated
    assert facts["beta"]["last_load_ms"] is None
    assert facts["beta"]["loaded"] is False


def test_default_loader_is_labelled_simulated(tmp_path):
    store = make_store(tmp_path, {"alpha": 10})
    manager = ModelLifecycle(
        NODE_ID, store, ram_bytes=1000, now=lambda: FIXED, timer=Clock()
    )
    assert isinstance(manager._loader, SimulatedLoader)
    manager.load("alpha")
    report = manager.report()
    assert report["loader_simulated"] is True
    loaded_events = [e for e in manager.events if e["type"] == "model.loaded"]
    assert loaded_events[0]["payload"]["loader_simulated"] is True


# -- node agent integration ---------------------------------------------------------


def provision(store_dir: Path) -> LocalKeyStore:
    store = LocalKeyStore(store_dir)
    root_keypair, root_cert = create_mesh_root(now=FIXED)
    node_keypair = NodeKeypair.generate()
    cert = issue_node_certificate(
        root_keypair,
        root_cert,
        node_id=NODE_ID,
        node_keypair=node_keypair,
        device_class="vm",
        agent_version="0.0.1",
        now=FIXED,
    )
    store.save_certificate(root_cert)
    store.save_seed(NODE_ID, node_keypair.seed)
    store.save_certificate(cert)
    return store


def test_agent_health_lists_resident_models(tmp_path):
    key_dir = tmp_path / "keys"
    provision(key_dir)
    config = NodeConfig.from_dict(
        {
            "version": 1,
            "node_id": NODE_ID,
            "device_class": "vm",
            "key_store_dir": str(key_dir),
        }
    )
    clock = Clock()
    store = make_store(tmp_path, {"alpha": 100})
    lifecycle = ModelLifecycle(
        NODE_ID,
        store,
        ram_bytes=10_000,
        loader=TimedLoader(clock),
        now=lambda: FIXED,
        timer=clock,
    )
    agent = NodeAgent(
        config, now=lambda: FIXED, model_store=store, model_lifecycle=lifecycle
    )
    with pytest.raises(NodeAgentError, match="load_model"):
        agent.load_model("alpha")  # a created node loads nothing
    agent.start()
    agent.join()
    agent.load_model("alpha")
    assert agent.health()["models_loaded"] == ["alpha"]
    assert agent.status()["models_loaded"] == ["alpha"]
    agent.drain()
    agent.unload_model("alpha")  # shedding weight while draining is allowed
    assert agent.health()["models_loaded"] == []


def test_agent_without_lifecycle_still_claims_no_models(tmp_path):
    key_dir = tmp_path / "keys"
    provision(key_dir)
    config = NodeConfig.from_dict(
        {
            "version": 1,
            "node_id": NODE_ID,
            "device_class": "vm",
            "key_store_dir": str(key_dir),
        }
    )
    agent = NodeAgent(config, now=lambda: FIXED)
    agent.start()
    assert agent.health()["models_loaded"] == []
    with pytest.raises(NodeAgentError, match="no model lifecycle"):
        agent.load_model("alpha")


def test_readme_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
