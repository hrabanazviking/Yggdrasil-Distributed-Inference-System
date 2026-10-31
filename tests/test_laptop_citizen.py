"""Laptop citizen tests — Y-034's acceptance.

The citizen must prove: (a) the profile is data — it loads
from its registered file, validates against its schema
(with this slice's optional accelerator fields), and a
tampered profile (a governor policy that cannot hold, a
citizen that is not its device class, an unknown neighbour
key, a missing required accelerator or VRAM floor) is
refused by name; the manifest registers it (Y-016);
(b) evaluation is measured — a census shaped from
purplelongship's real measured facts (Ryzen 9 4900HS,
x86_64, 16 threads, 38.59 GiB, an RTX 2060 Max-Q detected
with 6.0 GiB VRAM measured, 40 °C, no Hailo device) with
the laptop's real neighbour states evaluates ready, with
the reserve computed, the Hailo claim ``absent``, and
every neighbour's action ``none``; (c) the gates bite — an
ARM census, a wrong device class, a small-RAM machine, a
GPU-less census, an unmeasured VRAM, and a too-small VRAM
all fail by name and the factory refuses to dress such a
machine as a laptop citizen; (d) the Aesir grade — the
citizen repeats exactly the grade the census's Aesir
engine entry carries (proven only through the adapter's
ledger feeding, Y-024, exercised here with the real
adapter over a fake transport), and claims ``absent``
when no Aesir engine was fed in; it never raises a grade;
(e) unknown neighbours never gate and never get invented
— missing observations read ``unknown`` and coexistence
holds by the reserve; (f) the factory wires the whole
agent — the profile's governor, a lifecycle whose VRAM
pool is the measured VRAM and whose RAM pool is the census
RAM scaled by the governor fraction, the Aesir engine seam
carried into the agent's own census, sandbox, queue, and
failure monitor all live: the agent starts on a
laptop-class identity, joins, runs a queued task end to
end, heartbeats, and carries the citizen report in its
status; (g) the founding README is untouched.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.aesir_adapter import AesirAdapter, AesirHttpError, AesirUnreachable, parse_ledger
from yggdrasil.census import ProbeSet, measure_census
from yggdrasil.data_discipline import validate_repo
from yggdrasil.identity import LocalKeyStore, NodeKeypair, create_mesh_root, issue_node_certificate
from yggdrasil.laptop_citizen import (
    LaptopCitizenError,
    aesir_grade_for,
    build_laptop_citizen_agent,
    clean_neighbours,
    cuda_entry_for,
    evaluate_laptop_citizen,
    load_profile,
    validate_profile,
    validate_report,
)
from yggdrasil.node_agent import NodeConfig

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
NODE_ID = "purplelongship-1"
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"

# purplelongship's measured facts, read off the machine on
# 2026-10-31 (see docs/LAPTOP_CITIZEN.md): AMD Ryzen 9 4900HS
# with Radeon Graphics, x86_64, 16 threads, MemTotal
# 40465868 kB = 38.59 GiB, /dev/nvidia0 present, the driver
# naming an NVIDIA GeForce RTX 2060 with Max-Q Design,
# nvidia-smi reading 6144 MiB VRAM and 37 °C GPU, thermal
# zones 37/40 °C, ~1.1 TB disk free, no /dev/hailo0, and no
# Aesir service active. Injected here as probes, so these
# runs are SIMULATED — the live run is recorded in the doc.
LAPTOP_NEIGHBOURS = {
    "desktop_session": "active",
    "cuda_device": "present",
}
GPU_NAME = "NVIDIA GeForce RTX 2060 with Max-Q Design"


def laptop_probes(**overrides) -> ProbeSet:
    values = {
        "cpu": lambda: {
            "arch": "x86_64",
            "cores": 16,
            "model": "AMD Ryzen 9 4900HS with Radeon Graphics",
        },
        "ram_gb": lambda: 40465868 / (1024 * 1024),
        "accelerators": lambda: [{"kind": "cuda", "name": GPU_NAME}],
        "disk_free_gb": lambda: 1100.0,
        "thermal_c": lambda: 40.0,
        "vram_gb": lambda: 6144 / 1024,
    }
    values.update(overrides)
    return ProbeSet(**values)


def laptop_census(engines=None, **probe_overrides) -> dict:
    return measure_census(
        NODE_ID, probes=laptop_probes(**probe_overrides), engines=engines, at=FIXED
    )


# -- the profile is data ------------------------------------------------------


def test_the_profile_loads_and_is_registered():
    profile = load_profile()
    assert profile["profile"] == "yggdrasil-laptop-citizen-v1"
    assert profile["citizen"] == profile["device_class"] == "laptop"
    assert profile["required_accelerator_kind"] == "cuda"
    assert profile["min_vram_gb"] == 4.0
    assert profile["hailo_rule"] == "never_beyond_aesir_ledger"
    reports = {r.id: r for r in validate_repo()}
    entry = reports["laptop_citizen_profile"]
    assert entry.kind == "profile"
    assert entry.version == "yggdrasil-laptop-citizen-v1"
    assert entry.path == "profiles/laptop_citizen.json"


def test_a_tampered_profile_is_refused_by_name():
    profile = load_profile()
    bad_policy = json.loads(json.dumps(profile))
    bad_policy["governor_policy"]["thermal_resume_c"] = 99.0  # resume above shed
    with pytest.raises(LaptopCitizenError, match="governor policy"):
        validate_profile(bad_policy)
    renamed = json.loads(json.dumps(profile))
    renamed["citizen"] = "pi"
    with pytest.raises(LaptopCitizenError, match="does not match"):
        validate_profile(renamed)
    unknown_key = json.loads(json.dumps(profile))
    unknown_key["neighbours"][0]["observation_key"] = "moon_phase"
    with pytest.raises(LaptopCitizenError, match="unknown neighbour"):
        validate_profile(unknown_key)
    no_accelerator = json.loads(json.dumps(profile))
    del no_accelerator["required_accelerator_kind"]
    with pytest.raises(LaptopCitizenError, match="required_accelerator_kind"):
        validate_profile(no_accelerator)
    no_vram = json.loads(json.dumps(profile))
    del no_vram["min_vram_gb"]
    with pytest.raises(LaptopCitizenError, match="min_vram_gb"):
        validate_profile(no_vram)


# -- evaluation is measured -----------------------------------------------------


def test_purplelongships_measured_shape_evaluates_ready():
    report = evaluate_laptop_citizen(
        laptop_census(),
        device_class="laptop",
        neighbours=LAPTOP_NEIGHBOURS,
        simulated=True,
        now=lambda: FIXED,
    )
    validate_report(report)
    assert report["ready"] is True
    assert report["simulated"] is True
    assert report["node_id"] == NODE_ID
    assert report["citizen"] == "laptop"
    gates = {c["name"]: c["status"] for c in report["checks"]}
    assert gates["device_class"] == "pass"
    assert gates["x86_architecture"] == "pass"
    assert gates["cores"] == "pass"
    assert gates["ram"] == "pass"
    assert gates["cuda_accelerator"] == "pass"
    assert gates["vram"] == "pass"
    assert gates["user_memory_reserve"] == "pass"
    # 38.59 GiB measured, ceiling 0.80 -> a fifth left for the user.
    assert report["memory_reserve_gb"] == pytest.approx(38.59 * 0.20, abs=0.01)
    # purplelongship's real reading: no Hailo device on this machine.
    assert report["hailo_claim"] == "absent"
    # No Aesir engine was fed into this census: claimed absent, plainly.
    assert aesir_grade_for(report["census"]) == "absent"
    observed = {c["name"]: c["detail"] for c in report["checks"] if c["status"] == "observed"}
    assert "no Aesir engine was fed" in observed["aesir_engine"]
    assert {n["name"]: n["state"] for n in report["neighbours"]} == {
        "user_desktop": "active",
        "nvidia_device": "present",
    }
    assert all(n["action_taken"] == "none" for n in report["neighbours"])


def test_missing_observations_are_unknown_and_do_not_gate():
    report = evaluate_laptop_citizen(
        laptop_census(), device_class="laptop", simulated=True, now=lambda: FIXED
    )
    assert report["ready"] is True
    assert all(n["state"] == "unknown" for n in report["neighbours"])
    assert all(n["action_taken"] == "none" for n in report["neighbours"])


def test_unshaped_observations_are_refused():
    profile = load_profile()
    with pytest.raises(LaptopCitizenError, match="unknown neighbour"):
        clean_neighbours({"weather": "sunny"}, profile)
    with pytest.raises(LaptopCitizenError, match="not one of"):
        clean_neighbours({"desktop_session": "loud"}, profile)


# -- the gates bite ---------------------------------------------------------------


def test_an_arm_machine_is_not_a_laptop_citizen():
    census = laptop_census(
        cpu=lambda: {"arch": "aarch64", "cores": 4, "model": "Raspberry Pi 5"},
        accelerators=lambda: [{"kind": "none"}],
    )
    report = evaluate_laptop_citizen(
        census, device_class="laptop", simulated=True, now=lambda: FIXED
    )
    assert report["ready"] is False
    gates = {c["name"]: c["status"] for c in report["checks"]}
    assert gates["x86_architecture"] == "fail"
    assert gates["cuda_accelerator"] == "fail"


def test_a_wrong_device_class_and_a_small_machine_fail():
    report = evaluate_laptop_citizen(
        laptop_census(), device_class="pi", simulated=True, now=lambda: FIXED
    )
    assert report["ready"] is False
    small = laptop_census(ram_gb=lambda: 8.0)
    report = evaluate_laptop_citizen(
        small, device_class="laptop", simulated=True, now=lambda: FIXED
    )
    gates = {c["name"]: c["status"] for c in report["checks"]}
    assert gates["ram"] == "fail"
    # 8 GiB at a 0.80 ceiling leaves 1.6 GiB — under the 4 GiB reserve.
    assert gates["user_memory_reserve"] == "fail"
    assert report["ready"] is False


def test_a_gpu_less_machine_and_unmeasured_vram_fail_by_name():
    no_gpu = laptop_census(accelerators=lambda: [{"kind": "none"}])
    report = evaluate_laptop_citizen(
        no_gpu, device_class="laptop", simulated=True, now=lambda: FIXED
    )
    gates = {c["name"]: c["status"] for c in report["checks"]}
    assert gates["cuda_accelerator"] == "fail"
    assert report["ready"] is False

    # Y-022's system probes honestly read no VRAM: a census
    # carrying no vram_gb must fail the gate, never pass on
    # an absence rounded up.
    unmeasured = laptop_census(vram_gb=lambda: None)
    assert "vram_gb" not in unmeasured
    report = evaluate_laptop_citizen(
        unmeasured, device_class="laptop", simulated=True, now=lambda: FIXED
    )
    gates = {c["name"]: c["status"] for c in report["checks"]}
    assert gates["vram"] == "fail"
    assert report["ready"] is False

    small_vram = laptop_census(vram_gb=lambda: 2.0)
    report = evaluate_laptop_citizen(
        small_vram, device_class="laptop", simulated=True, now=lambda: FIXED
    )
    gates = {c["name"]: c["status"] for c in report["checks"]}
    assert gates["vram"] == "fail"
    assert report["ready"] is False


# -- the Aesir grade -----------------------------------------------------------------


def test_cuda_detection_is_presence_not_proof():
    entry = cuda_entry_for(laptop_census())
    assert entry["kind"] == "cuda"
    assert entry["name"] == GPU_NAME
    # The system-shaped detection carries proven: false (Y-022);
    # presence is measured, proof is the engine ledger's word.
    assert entry["proven"] is False


# A ledger in Aesir's own markdown shape (the Y-024 test's
# fixture discipline): AES-SRV-010 verified governs native
# generation + streaming.
LEDGER_TEXT = """# Project A.E.S.I.R. Canonical Capability Ledger

### AES-SRV-010 — Authenticated native loopback CUDA generation

- **Status:** `verified`
- **Owner:** CLI orchestration, server transport/protocol, facade session contract
"""
RESIDENT = "gemma4-e2b:latest"


class FakeAesirTransport:
    """Aesir's native wire shapes, canned — the adapter's
    transport seam (the bearer key lives here, never in the
    adapter or the citizen)."""

    def get_json(self, path: str) -> dict:
        if path == "/health/live":
            return {"schema_version": 1, "status": "ok", "scope": "process"}
        if path == "/health/ready":
            return {
                "schema_version": 1, "status": "ready", "ready": True,
                "model": RESIDENT, "backend": "cuda",
            }
        if path == "/v1/models":
            return {"object": "list", "data": [{"id": RESIDENT, "object": "model"}]}
        raise AesirHttpError(404, f"unknown route {path} (fake)")

    def post_json(self, path: str, body: dict) -> dict:
        raise AesirHttpError(404, f"unknown route {path} (fake)")

    def post_lines(self, path: str, body: dict):
        raise AesirUnreachable("no streaming in this fixture")
        yield  # pragma: no cover — a generator by shape


def _aesir_capability(ledger_text: str = LEDGER_TEXT) -> dict:
    adapter = AesirAdapter(FakeAesirTransport(), ledger=parse_ledger(ledger_text))
    return adapter.capability_for_census()


def test_the_aesir_grade_is_the_ledgers_grade_repeated_exactly():
    capability = _aesir_capability()
    assert capability["capabilities"]["provenance_grade"] == "proven"
    assert capability["capabilities"]["generate"] is True
    census = laptop_census(engines=[capability])
    assert aesir_grade_for(census) == "proven"
    report = evaluate_laptop_citizen(
        census, device_class="laptop", simulated=True, now=lambda: FIXED
    )
    assert report["ready"] is True
    observed = {c["name"]: c["detail"] for c in report["checks"] if c["status"] == "observed"}
    assert "graded proven" in observed["aesir_engine"]

    # A ledger that does not claim the entry grades unproven
    # through the same adapter — and the citizen repeats that
    # exactly, never proven.
    downgraded = _aesir_capability(LEDGER_TEXT.replace("verified", "planned"))
    census = laptop_census(engines=[downgraded])
    assert aesir_grade_for(census) == "unproven"
    report = evaluate_laptop_citizen(
        census, device_class="laptop", simulated=True, now=lambda: FIXED
    )
    observed = {c["name"]: c["detail"] for c in report["checks"] if c["status"] == "observed"}
    assert "no Aesir capability is claimed" in observed["aesir_engine"]


# -- the factory wires the whole agent ------------------------------------------------


def _provisioned_config(tmp_path: Path) -> NodeConfig:
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
    return NodeConfig.from_dict({
        "version": 1, "node_id": NODE_ID, "device_class": "laptop",
        "key_store_dir": str(store_dir),
    })


def test_the_factory_refuses_a_machine_that_fails_the_profile(tmp_path):
    config = _provisioned_config(tmp_path)
    with pytest.raises(LaptopCitizenError, match="does not meet the laptop citizen profile"):
        build_laptop_citizen_agent(
            config,
            store_root=tmp_path / "store",
            probes=laptop_probes(accelerators=lambda: [{"kind": "none"}]),
            simulated=True,
            now=lambda: FIXED,
        )


def test_the_factory_builds_the_full_citizen_agent(tmp_path):
    config = _provisioned_config(tmp_path)
    capability = _aesir_capability()
    agent, report = build_laptop_citizen_agent(
        config,
        store_root=tmp_path / "store",
        probes=laptop_probes(),
        readings=lambda: {
            "thermal_c": 40.0,
            "memory_used_bytes": 8 * 1024**3,
            "memory_total_bytes": 38 * 1024**3,
            "cpu_share_used": 0.25,
        },
        neighbours=LAPTOP_NEIGHBOURS,
        census_engines=lambda: [capability],
        simulated=True,
        now=lambda: FIXED,
    )
    assert report["ready"] is True
    assert agent.citizen_report == report
    # The profile's governor governs, and the lifecycle's pools
    # are the measured pools: VRAM is the card's own 6 GiB, RAM
    # is the measured RAM inside the governor's fraction — the
    # reserve binds the budgets, not only the report.
    assert agent.governor.policy["thermal_shed_c"] == 85.0
    assert agent.governor.policy["max_memory_fraction"] == 0.8
    assert agent.model_lifecycle._budgets["vram_bytes"] == 6 * 1024**3
    expected_ram = int(report["census"]["ram_gb"] * 0.8 * (1024**3))
    assert agent.model_lifecycle._budgets["ram_bytes"] == expected_ram
    assert agent.model_lifecycle._budgets["max_slots"] == 3

    agent.start()
    agent.join()
    assert agent.status()["citizen"] == report
    assert agent.census()["cpu"]["arch"] == "x86_64"
    # The Aesir seam reaches the agent's own census refresh,
    # with the ledger's grade intact.
    engines = {e["engine"]: e for e in agent.census()["engines"]}
    assert engines["aesir"]["capabilities"]["provenance_grade"] == "proven"

    # A hash-pinned fixture model, loaded through the lifecycle
    # (SIMULATED loader — residency bookkeeping, no weights move).
    fixture = tmp_path / "fixture-model.gguf"
    fixture.write_bytes(b"SIMULATED fixture model bytes for the laptop citizen test")
    agent.model_store.add(
        fixture, name="fixture-model", family="fixture", quant="Q4_K_M", format="gguf"
    )
    agent.load_model("fixture-model")
    assert agent.health()["models_loaded"] == ["fixture-model"]

    task = {
        "id": "22222222-2222-4222-8222-222222222222",
        "kind": "chat.generate",
        "payload": {"prompt": "citizen check"},
        "caller": "laptop-citizen-test",
        "criticality": "standard",
        "requirements": {"model": "fixture-model", "capabilities": ["generate"]},
    }
    agent.queue_task(task)
    assert agent.health()["queue_depth"] == 1

    def runner(task, control):
        yield "laptop citizen answer"

    outcome = agent.execute_next_queued(
        runner, engine="aesir", model="fixture-model"
    )
    result, _events, _report = outcome
    assert result["status"] == "completed"
    assert agent.health()["queue_depth"] == 0
    beat = agent.heartbeat()
    assert beat["liveness"] == "present"
    assert agent.failure_report()["total_incidents"] == 0
    agent.drain()
    agent.stop()


def test_the_founding_readme_is_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
