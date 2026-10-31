"""Graceful degradation tests — Y-040's acceptance.

The tracker and the agent must prove: (a) the baseline is
every accelerator kind ever measured and the lost kinds are
named exactly — total loss declares ``degraded_cpu_only``
with ``serving_on_cpu_only`` true, partial loss declares
``degraded_partial``; (b) a node that never had an
accelerator is ``cpu_only_by_design`` and is never called
degraded, and a tracker that observed nothing says so with
``observations: 0``; (c) gaining an accelerator grows the
baseline and is never a degradation; losing and regaining
one is a degradation and a recovery, counted, historied,
and emitted as validated Y-013 events; (d) VRAM is tracked
as baseline vs present with absence read as absence; (e) a
census naming another node is refused by name; (f)
resident impact is judged by pinned format against the
Y-038 policy table — gguf falls back to CPU, an accelerator
format whose kind is gone is stranded, a still-present
kind is unaffected, an unnamed format is stranded (never
assumed), system-pool residents are untouched, and a
stranded model in use is named separately; (g) the node
agent — a census refresh that measures a loss declares it
in the refresh result and in ``status()``, re-places the
CPU-fallback resident into the system pool, unloads the
stranded one, keeps serving (health still lists the
fallback model), and defers an in-use stranded model
instead of yanking it; recovery restores the declaration;
an unwired agent declares nothing and its refresh result
is exactly as before; (h) the citizen factories wire a
tracker over their factory-measured census (the Pi
citizen: by-design, observed once at build; the laptop
citizen: full, with its measured CUDA baseline); (i) the
founding README is untouched.

The losses here are probe fixtures shaped like losses —
SIMULATED. No real accelerator has died for these tests,
and none is claimed to have.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.census import ProbeSet, measure_census
from yggdrasil.degradation import (
    DegradationError,
    DegradationTracker,
    accelerator_kinds,
    validate_report,
    validate_state,
)
from yggdrasil.identity import (
    LocalKeyStore,
    NodeKeypair,
    create_mesh_root,
    issue_node_certificate,
)
from yggdrasil.laptop_citizen import build_laptop_citizen_agent
from yggdrasil.model_lifecycle import ModelLifecycle
from yggdrasil.model_store import ModelStore
from yggdrasil.node_agent import NodeAgent, NodeConfig
from yggdrasil.observability import validate_event
from yggdrasil.pi_citizen import build_pi_citizen_agent

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
NODE_ID = "degrade-node-1"
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
GIB = 1024**3


def census_with(kinds, *, vram=None, node_id=NODE_ID) -> dict:
    accelerators = [{"kind": kind} for kind in kinds] or [{"kind": "none"}]
    probes = ProbeSet(
        cpu=lambda: {"arch": "x86_64", "cores": 8, "model": "Fixture CPU"},
        ram_gb=lambda: 16.0,
        accelerators=lambda: list(accelerators),
        disk_free_gb=lambda: 100.0,
        thermal_c=lambda: 50.0,
        vram_gb=lambda: vram,
    )
    return measure_census(node_id, probes=probes, at=FIXED)


# -- baseline, loss, and the four honest statuses -----------------------------


def test_an_unobserved_tracker_claims_nothing():
    tracker = DegradationTracker(NODE_ID, now=lambda: FIXED)
    state = validate_state(tracker.state())
    assert state["observations"] == 0
    assert state["status"] == "cpu_only_by_design"
    assert state["baseline_accelerators"] == []
    assert tracker.degraded is False


def test_a_cpu_only_node_is_by_design_never_degraded():
    tracker = DegradationTracker(NODE_ID, now=lambda: FIXED)
    tracker.observe_census(census_with([]))
    state = tracker.observe_census(census_with([]))
    validate_state(state)
    assert state["status"] == "cpu_only_by_design"
    assert state["lost_accelerators"] == []
    assert state["serving_on_cpu_only"] is False
    assert state["observations"] == 2
    report = validate_report(tracker.report())
    assert report["degradations"] == 0
    assert report["transitions"] == 0


def test_total_loss_declares_cpu_only_service():
    tracker = DegradationTracker(NODE_ID, now=lambda: FIXED)
    first = tracker.observe_census(census_with(["cuda"], vram=6.0))
    assert first["status"] == "full"
    assert first["baseline_accelerators"] == ["cuda"]
    state = tracker.observe_census(census_with([]))
    validate_state(state)
    assert state["status"] == "degraded_cpu_only"
    assert state["lost_accelerators"] == ["cuda"]
    assert state["current_accelerators"] == []
    assert state["serving_on_cpu_only"] is True
    assert state["degraded_since"] == "2026-06-01T12:00:00Z"
    assert state["baseline_vram_gb"] == 6.0
    assert state["current_vram_gb"] is None  # absence read as absence
    assert tracker.degraded is True


def test_partial_loss_declares_partial():
    tracker = DegradationTracker(NODE_ID, now=lambda: FIXED)
    tracker.observe_census(census_with(["cuda", "hailo"]))
    state = tracker.observe_census(census_with(["hailo"]))
    assert state["status"] == "degraded_partial"
    assert state["lost_accelerators"] == ["cuda"]
    assert state["current_accelerators"] == ["hailo"]
    assert state["serving_on_cpu_only"] is False


def test_gaining_an_accelerator_is_an_upgrade_not_a_degradation():
    tracker = DegradationTracker(NODE_ID, now=lambda: FIXED)
    tracker.observe_census(census_with([]))
    state = tracker.observe_census(census_with(["cuda"], vram=6.0))
    assert state["status"] == "full"
    assert state["baseline_accelerators"] == ["cuda"]
    report = tracker.report()
    assert report["degradations"] == 0
    assert [e["type"] for e in tracker.events] == []


def test_loss_and_recovery_are_counted_historied_and_emitted():
    tracker = DegradationTracker(NODE_ID, now=lambda: FIXED)
    tracker.observe_census(census_with(["cuda"], vram=6.0))
    tracker.observe_census(census_with([]))
    state = tracker.observe_census(census_with(["cuda"], vram=6.0))
    assert state["status"] == "full"
    assert state["degraded_since"] is None
    report = validate_report(tracker.report())
    assert report["degradations"] == 1
    assert report["recoveries"] == 1
    assert report["transitions"] == 2
    assert report["history"][0]["from_status"] == "full"
    assert report["history"][0]["to_status"] == "degraded_cpu_only"
    assert report["history"][0]["lost_accelerators"] == ["cuda"]
    assert report["history"][1]["regained_accelerators"] == ["cuda"]
    assert [e["type"] for e in tracker.events] == [
        "degradation.entered",
        "degradation.recovered",
    ]
    for event in tracker.events:
        validate_event(event)
        assert event["source"]["component"] == "node_agent"
    assert tracker.events[0]["payload"]["lost_accelerators"] == ["cuda"]


def test_a_foreign_census_is_refused_by_name():
    tracker = DegradationTracker(NODE_ID, now=lambda: FIXED)
    with pytest.raises(DegradationError, match="another|belongs to"):
        tracker.observe_census(census_with(["cuda"], node_id="someone-else"))
    assert tracker.state()["observations"] == 0


def test_accelerator_kinds_ignores_the_honest_none():
    assert accelerator_kinds(census_with([])) == ()
    assert accelerator_kinds(census_with(["cuda", "hailo"])) == ("cuda", "hailo")


# -- resident impact, judged by format -----------------------------------------

GGUF_ENTRY = {"name": "gguf-model", "format": "gguf"}
AWQ_ENTRY = {"name": "awq-model", "format": "awq"}
MLX_ENTRY = {"name": "mlx-model", "format": "mlx"}
ODD_ENTRY = {"name": "odd-model", "format": "onnx"}


def _resident(name, *, placement="accelerator", in_use=0):
    return {"name": name, "placement": placement, "in_use": in_use}


def test_residents_are_sorted_by_format_against_the_loss():
    tracker = DegradationTracker(NODE_ID, now=lambda: FIXED)
    tracker.observe_census(census_with(["cuda", "metal"], vram=6.0))
    state = tracker.observe_census(
        census_with(["metal"]),
        residents=[
            _resident("gguf-model"),
            _resident("awq-model"),
            _resident("mlx-model"),
            _resident("odd-model"),
            _resident("gguf-model", placement="system"),
        ],
        store_entries=[GGUF_ENTRY, AWQ_ENTRY, MLX_ENTRY, ODD_ENTRY],
    )
    impact = state["residents"]
    # cuda is lost, metal remains: gguf falls back, awq (cuda)
    # and the unnamed onnx are stranded (never assumed), mlx
    # (metal) is unaffected, the system resident is untouched.
    assert impact["cpu_fallback"] == ["gguf-model"]
    assert impact["stranded"] == ["awq-model", "odd-model"]
    assert impact["accelerator"] == ["mlx-model"]
    assert impact["stranded_in_use"] == []


def test_a_stranded_model_in_use_is_named_separately():
    tracker = DegradationTracker(NODE_ID, now=lambda: FIXED)
    tracker.observe_census(census_with(["cuda"], vram=6.0))
    state = tracker.observe_census(
        census_with([]),
        residents=[_resident("awq-model", in_use=2)],
        store_entries=[AWQ_ENTRY],
    )
    assert state["residents"]["stranded"] == ["awq-model"]
    assert state["residents"]["stranded_in_use"] == ["awq-model"]


# -- the node agent: declaration + acts -----------------------------------------


def _provisioned_agent(tmp_path: Path, **agent_kwargs) -> NodeAgent:
    store_dir = tmp_path / "keys"
    store = LocalKeyStore(store_dir)
    root_keypair, root_cert = create_mesh_root(now=FIXED)
    node_keypair = NodeKeypair.generate()
    cert = issue_node_certificate(
        root_keypair, root_cert, node_id=NODE_ID, node_keypair=node_keypair,
        device_class="laptop", agent_version="0.0.1", now=FIXED,
    )
    store.save_certificate(root_cert)
    store.save_seed(NODE_ID, node_keypair.seed)
    store.save_certificate(cert)
    config = NodeConfig.from_dict({
        "version": 1, "node_id": NODE_ID, "device_class": "laptop",
        "key_store_dir": str(store_dir),
    })
    return NodeAgent(config, now=lambda: FIXED, **agent_kwargs)


class _Machine:
    """A mutable probe machine: the accelerator can 'die'."""

    def __init__(self):
        self.kinds = [{"kind": "cuda", "name": "Fixture GPU"}]
        self.vram = 6.0

    def probes(self) -> ProbeSet:
        return ProbeSet(
            cpu=lambda: {"arch": "x86_64", "cores": 8, "model": "Fixture CPU"},
            ram_gb=lambda: 16.0,
            accelerators=lambda: list(self.kinds),
            disk_free_gb=lambda: 100.0,
            thermal_c=lambda: 50.0,
            vram_gb=lambda: self.vram,
        )

    def lose_the_gpu(self):
        self.kinds = [{"kind": "none"}]
        self.vram = None

    def regain_the_gpu(self):
        self.kinds = [{"kind": "cuda", "name": "Fixture GPU"}]
        self.vram = 6.0


def _stocked_agent(tmp_path: Path):
    """An agent over a stocked store + lifecycle + tracker,
    with both fixture models resident on the accelerator."""
    machine = _Machine()
    store = ModelStore(tmp_path / "store", now=lambda: FIXED)
    for name, fmt, quant in (
        ("gguf-model", "gguf", "Q4_K_M"),
        ("awq-model", "awq", "AWQ-4bit"),
    ):
        source = tmp_path / f"{name}.bin"
        source.write_bytes(f"SIMULATED fixture bytes for {name}".encode() * 64)
        store.add(source, name=name, family="fixture", quant=quant, format=fmt)
    lifecycle = ModelLifecycle(
        NODE_ID, store, vram_bytes=8 * GIB, ram_bytes=8 * GIB,
        max_slots=4, now=lambda: FIXED,
    )
    tracker = DegradationTracker(NODE_ID, now=lambda: FIXED)
    agent = _provisioned_agent(
        tmp_path,
        census_probes=machine.probes(),
        model_store=store,
        model_lifecycle=lifecycle,
        degradation_tracker=tracker,
    )
    agent.start()
    agent.join()
    agent.refresh_census()  # the baseline: cuda present, full
    agent.load_model("gguf-model")
    agent.load_model("awq-model")
    assert lifecycle.resident("gguf-model")["placement"] == "accelerator"
    assert lifecycle.resident("awq-model")["placement"] == "accelerator"
    return agent, machine, lifecycle, tracker


def test_the_agent_declares_the_loss_and_acts_on_it(tmp_path):
    agent, machine, lifecycle, _tracker = _stocked_agent(tmp_path)
    machine.lose_the_gpu()
    result = agent.refresh_census()
    state = validate_state(result["degradation"])
    assert state["status"] == "degraded_cpu_only"
    assert state["lost_accelerators"] == ["cuda"]
    assert state["serving_on_cpu_only"] is True
    acts = result["degradation_acts"]
    assert acts["replaced_to_cpu"] == ["gguf-model"]
    assert acts["unloaded_stranded"] == ["awq-model"]
    assert acts["failures"] == []
    # The node keeps serving on CPU: the fallback model is
    # resident in the system pool and health says so; the
    # stranded model is gone and nowhere claimed.
    assert lifecycle.resident("gguf-model")["placement"] == "system"
    assert lifecycle.is_loaded("awq-model") is False
    assert agent.health()["models_loaded"] == ["gguf-model"]
    assert agent.status()["degradation"]["status"] == "degraded_cpu_only"
    assert agent.degradation_report()["degradations"] == 1


def test_the_agent_defers_an_in_use_stranded_model(tmp_path):
    agent, machine, lifecycle, _tracker = _stocked_agent(tmp_path)
    lifecycle.acquire("awq-model")
    machine.lose_the_gpu()
    result = agent.refresh_census()
    assert result["degradation"]["residents"]["stranded_in_use"] == ["awq-model"]
    assert result["degradation_acts"]["deferred_in_use"] == ["awq-model"]
    assert result["degradation_acts"]["unloaded_stranded"] == []
    assert lifecycle.is_loaded("awq-model") is True  # never yanked mid-task
    lifecycle.release("awq-model")


def test_recovery_restores_the_declaration(tmp_path):
    agent, machine, _lifecycle, _tracker = _stocked_agent(tmp_path)
    machine.lose_the_gpu()
    agent.refresh_census()
    machine.regain_the_gpu()
    result = agent.refresh_census()
    assert result["degradation"]["status"] == "full"
    assert result["degradation"]["lost_accelerators"] == []
    report = agent.degradation_report()
    assert report["degradations"] == 1
    assert report["recoveries"] == 1


def test_an_unwired_agent_declares_nothing(tmp_path):
    machine = _Machine()
    agent = _provisioned_agent(tmp_path, census_probes=machine.probes())
    agent.start()
    result = agent.refresh_census()
    assert "degradation" not in result
    assert agent.status()["degradation"] is None
    assert agent.degradation_state() is None
    assert agent.degradation_report() is None


# -- the citizen factories wire a tracker ---------------------------------------

MIMIR_NEIGHBOURS = {
    "samtal_service": "active",
    "aurelia_port": "listening",
    "kiosk_port": "not_listening",
    "hailo_device": "absent",
}


def _citizen_config(tmp_path: Path, node_id: str, device_class: str) -> NodeConfig:
    store_dir = tmp_path / f"keys-{device_class}"
    store = LocalKeyStore(store_dir)
    root_keypair, root_cert = create_mesh_root(now=FIXED)
    node_keypair = NodeKeypair.generate()
    cert = issue_node_certificate(
        root_keypair, root_cert, node_id=node_id, node_keypair=node_keypair,
        device_class=device_class, agent_version="0.0.1", now=FIXED,
    )
    store.save_certificate(root_cert)
    store.save_seed(node_id, node_keypair.seed)
    store.save_certificate(cert)
    return NodeConfig.from_dict({
        "version": 1, "node_id": node_id, "device_class": device_class,
        "key_store_dir": str(store_dir),
    })


def test_the_pi_factory_wires_a_by_design_tracker(tmp_path):
    config = _citizen_config(tmp_path, "mimir-1", "pi")
    mimir = ProbeSet(
        cpu=lambda: {
            "arch": "aarch64", "cores": 4,
            "model": "Raspberry Pi 5 Model B Rev 1.1",
        },
        ram_gb=lambda: 16598576 / (1024 * 1024),
        accelerators=lambda: [{"kind": "none"}],
        disk_free_gb=lambda: 202.0,
        thermal_c=lambda: 41.9,
        vram_gb=lambda: None,
    )
    agent, _report = build_pi_citizen_agent(
        config,
        store_root=tmp_path / "store",
        probes=mimir,
        neighbours=MIMIR_NEIGHBOURS,
        simulated=True,
        now=lambda: FIXED,
    )
    state = agent.status()["degradation"]
    assert state is not None
    assert state["status"] == "cpu_only_by_design"  # Mimir's design, not a loss
    assert state["observations"] >= 1  # the factory census is the baseline
    assert agent.degradation_tracker is not None


def test_the_laptop_factory_wires_a_full_tracker(tmp_path):
    config = _citizen_config(tmp_path, "purplelongship-1", "laptop")
    laptop = ProbeSet(
        cpu=lambda: {
            "arch": "x86_64", "cores": 16,
            "model": "AMD Ryzen 9 4900HS with Radeon Graphics",
        },
        ram_gb=lambda: 40465868 / (1024 * 1024),
        accelerators=lambda: [
            {"kind": "cuda", "name": "NVIDIA GeForce RTX 2060 with Max-Q Design"}
        ],
        disk_free_gb=lambda: 1100.0,
        thermal_c=lambda: 40.0,
        vram_gb=lambda: 6144 / 1024,
    )
    agent, _report = build_laptop_citizen_agent(
        config,
        store_root=tmp_path / "store",
        probes=laptop,
        neighbours={"desktop_session": "active", "cuda_device": "present"},
        simulated=True,
        now=lambda: FIXED,
    )
    state = agent.status()["degradation"]
    assert state["status"] == "full"
    assert state["baseline_accelerators"] == ["cuda"]
    assert state["baseline_vram_gb"] == 6.0


def test_the_founding_readme_is_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
    assert json.loads((ROOT / "data" / "data_manifest.json").read_text())
