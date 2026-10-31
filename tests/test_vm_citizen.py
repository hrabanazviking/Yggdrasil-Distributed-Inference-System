"""VM citizen tests — Y-035's acceptance.

The citizen must prove: (a) the profile is data — it loads
from its registered file, validates against its schema, and
a tampered profile (a governor policy that cannot hold, a
citizen that is not its device class, an unknown neighbour
key, and — this citizen's own cross-field rule — a required
accelerator or a VRAM floor named on a CPU-only profile) is
refused by name; the manifest registers it (Y-016);
(b) evaluation is measured — a census shaped from this VM's
real measured facts (AMD EPYC 9D64, x86_64, 4 cores,
7.749 GiB, exactly one accelerator entry of kind ``none``,
no VRAM, no thermal reading, 7.346 GiB disk free) with the
VM's real neighbour states evaluates ready, with the
reserve computed, the Hailo claim ``absent``, the thermal
absence reported as absence, and every neighbour's action
``none``; (c) the gates bite — an ARM census, a wrong
device class, a small machine, an accelerator-bearing
census, and a VRAM figure with no accelerator all fail by
name (the cpu_only gate is the slice's defining gate) and
the factory refuses to dress such a machine as a VM
citizen; (d) honest speeds — the report claims no
tokens-per-second figure anywhere, and its honest_speeds
check says a vm speed is only ever a VM-measured one;
(e) unknown neighbours never gate and never get invented —
missing observations read ``unknown`` and coexistence holds
by the reserve; (f) the factory wires the whole agent — the
profile's governor, a lifecycle whose VRAM pool is zero
(absence read as absence) and whose RAM pool is the census
RAM scaled by the governor fraction, sandbox, queue, and
failure monitor all live: the agent starts on a vm-class
identity, joins, runs a queued task end to end, heartbeats,
and carries the citizen report in its status; (g) the
founding README is untouched.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.census import ProbeSet, measure_census
from yggdrasil.data_discipline import validate_repo
from yggdrasil.identity import LocalKeyStore, NodeKeypair, create_mesh_root, issue_node_certificate
from yggdrasil.node_agent import NodeConfig
from yggdrasil.vm_citizen import (
    VmCitizenError,
    build_vm_citizen_agent,
    clean_neighbours,
    evaluate_vm_citizen,
    is_cpu_only,
    load_profile,
    validate_profile,
    validate_report,
)

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
NODE_ID = "htch-runtime-1"
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"

# This VM's measured facts, read off the machine on
# 2026-10-31 (see docs/VM_CITIZEN.md): host htch-runtime,
# AMD EPYC 9D64 88-Core Processor, x86_64, 4 cores,
# MemTotal 8124908 kB = 7.749 GiB, no /dev/nvidia0 and no
# /dev/hailo0 (the census reports exactly one accelerator,
# kind "none"), no thermal zone (no thermal_c field), no
# VRAM reading, 7.346 GiB free on the root filesystem the
# census probe measures, the hatch daemon active, Python
# 3.12.3. Injected here as probes, so these runs are
# SIMULATED — the live run is recorded in the doc.
VM_NEIGHBOURS = {
    "host_daemon": "active",
    "thermal_sensor": "absent",
}


def vm_probes(**overrides) -> ProbeSet:
    values = {
        "cpu": lambda: {
            "arch": "x86_64",
            "cores": 4,
            "model": "AMD EPYC 9D64 88-Core Processor",
        },
        "ram_gb": lambda: 8124908 / (1024 * 1024),
        "accelerators": lambda: [{"kind": "none"}],
        "disk_free_gb": lambda: 7.346,
        "thermal_c": lambda: None,
        "vram_gb": lambda: None,
    }
    values.update(overrides)
    return ProbeSet(**values)


def vm_census(**probe_overrides) -> dict:
    return measure_census(NODE_ID, probes=vm_probes(**probe_overrides), at=FIXED)


# -- the profile is data ------------------------------------------------------


def test_the_profile_loads_and_is_registered():
    profile = load_profile()
    assert profile["profile"] == "yggdrasil-vm-citizen-v1"
    assert profile["citizen"] == profile["device_class"] == "vm"
    # A CPU-only profile names no accelerator and no VRAM floor.
    assert "required_accelerator_kind" not in profile
    assert "min_vram_gb" not in profile
    assert profile["hailo_rule"] == "never_beyond_aesir_ledger"
    reports = {r.id: r for r in validate_repo()}
    entry = reports["vm_citizen_profile"]
    assert entry.kind == "profile"
    assert entry.version == "yggdrasil-vm-citizen-v1"
    assert entry.path == "profiles/vm_citizen.json"


def test_a_tampered_profile_is_refused_by_name():
    profile = load_profile()
    bad_policy = json.loads(json.dumps(profile))
    bad_policy["governor_policy"]["thermal_resume_c"] = 99.0  # resume above shed
    with pytest.raises(VmCitizenError, match="governor policy"):
        validate_profile(bad_policy)
    renamed = json.loads(json.dumps(profile))
    renamed["citizen"] = "laptop"
    with pytest.raises(VmCitizenError, match="does not match"):
        validate_profile(renamed)
    unknown_key = json.loads(json.dumps(profile))
    unknown_key["neighbours"][0]["observation_key"] = "moon_phase"
    with pytest.raises(VmCitizenError, match="unknown neighbour"):
        validate_profile(unknown_key)
    # The inverse of the laptop citizen's rule (Y-034): an
    # accelerator requirement or a VRAM floor on a CPU-only
    # profile contradicts the citizen and is refused by name.
    with_accelerator = json.loads(json.dumps(profile))
    with_accelerator["required_accelerator_kind"] = "cuda"
    with pytest.raises(VmCitizenError, match="required_accelerator_kind"):
        validate_profile(with_accelerator)
    with_vram = json.loads(json.dumps(profile))
    with_vram["min_vram_gb"] = 4.0
    with pytest.raises(VmCitizenError, match="min_vram_gb"):
        validate_profile(with_vram)


# -- evaluation is measured -----------------------------------------------------


def test_this_vms_measured_shape_evaluates_ready():
    report = evaluate_vm_citizen(
        vm_census(),
        device_class="vm",
        neighbours=VM_NEIGHBOURS,
        simulated=True,
        now=lambda: FIXED,
    )
    validate_report(report)
    assert report["ready"] is True
    assert report["simulated"] is True
    assert report["node_id"] == NODE_ID
    assert report["citizen"] == "vm"
    gates = {c["name"]: c["status"] for c in report["checks"]}
    assert gates["device_class"] == "pass"
    assert gates["x86_architecture"] == "pass"
    assert gates["cores"] == "pass"
    assert gates["ram"] == "pass"
    assert gates["cpu_only"] == "pass"
    assert gates["host_memory_reserve"] == "pass"
    # 7.749 GiB measured, ceiling 0.75 -> a quarter left for the host.
    assert report["memory_reserve_gb"] == pytest.approx(7.749 * 0.25, abs=0.01)
    # This VM's real readings: no Hailo device, no thermal sensor.
    assert report["hailo_claim"] == "absent"
    assert "thermal_c" not in report["census"]
    assert "vram_gb" not in report["census"]
    observed = {c["name"]: c["detail"] for c in report["checks"] if c["status"] == "observed"}
    assert "no thermal_c" in observed["thermal_reading"]
    assert "never read as a cool machine" in observed["thermal_reading"]
    assert {n["name"]: n["state"] for n in report["neighbours"]} == {
        "host_daemon": "active",
        "thermal_sensor": "absent",
    }
    assert all(n["action_taken"] == "none" for n in report["neighbours"])


def test_honest_speeds_no_speed_figure_is_claimed_anywhere():
    report = evaluate_vm_citizen(
        vm_census(), device_class="vm", simulated=True, now=lambda: FIXED
    )
    # No measured-speeds vocabulary appears anywhere in the
    # report: this citizen has run no model and claims no rate.
    assert "tokens_per_sec" not in json.dumps(report)
    assert "tokens_per_second" not in json.dumps(report)
    observed = {c["name"]: c["detail"] for c in report["checks"] if c["status"] == "observed"}
    assert "claims no tokens-per-second figure" in observed["honest_speeds"]
    assert "only when a run on a VM measured it" in observed["honest_speeds"]


def test_missing_observations_are_unknown_and_do_not_gate():
    report = evaluate_vm_citizen(
        vm_census(), device_class="vm", simulated=True, now=lambda: FIXED
    )
    assert report["ready"] is True
    assert all(n["state"] == "unknown" for n in report["neighbours"])
    assert all(n["action_taken"] == "none" for n in report["neighbours"])


def test_unshaped_observations_are_refused():
    profile = load_profile()
    with pytest.raises(VmCitizenError, match="unknown neighbour"):
        clean_neighbours({"weather": "sunny"}, profile)
    with pytest.raises(VmCitizenError, match="not one of"):
        clean_neighbours({"host_daemon": "loud"}, profile)


# -- the gates bite ---------------------------------------------------------------


def test_an_arm_machine_is_not_a_vm_citizen():
    census = vm_census(
        cpu=lambda: {"arch": "aarch64", "cores": 4, "model": "Raspberry Pi 5"}
    )
    report = evaluate_vm_citizen(census, device_class="vm", simulated=True, now=lambda: FIXED)
    assert report["ready"] is False
    gates = {c["name"]: c["status"] for c in report["checks"]}
    assert gates["x86_architecture"] == "fail"


def test_a_wrong_device_class_and_a_small_machine_fail():
    report = evaluate_vm_citizen(
        vm_census(), device_class="laptop", simulated=True, now=lambda: FIXED
    )
    assert report["ready"] is False
    small = vm_census(ram_gb=lambda: 2.0)
    report = evaluate_vm_citizen(small, device_class="vm", simulated=True, now=lambda: FIXED)
    gates = {c["name"]: c["status"] for c in report["checks"]}
    assert gates["ram"] == "fail"
    # 2 GiB at a 0.75 ceiling leaves 0.5 GiB — under the 1.5 GiB reserve.
    assert gates["host_memory_reserve"] == "fail"
    assert report["ready"] is False


def test_an_accelerator_bearing_machine_fails_cpu_only_by_name():
    gpu = vm_census(
        accelerators=lambda: [{"kind": "cuda", "name": "Some GPU"}],
        vram_gb=lambda: 6.0,
    )
    assert is_cpu_only(gpu) is False
    report = evaluate_vm_citizen(gpu, device_class="vm", simulated=True, now=lambda: FIXED)
    gates = {c["name"]: c["status"] for c in report["checks"]}
    assert gates["cpu_only"] == "fail"
    assert report["ready"] is False

    # A VRAM figure with no accelerator to hold it is a
    # contradiction, refused by the same gate — never smoothed.
    phantom = vm_census(vram_gb=lambda: 6.0)
    assert is_cpu_only(phantom) is False
    report = evaluate_vm_citizen(phantom, device_class="vm", simulated=True, now=lambda: FIXED)
    gates = {c["name"]: c["status"] for c in report["checks"]}
    assert gates["cpu_only"] == "fail"
    assert report["ready"] is False


def test_a_thermal_reading_when_present_is_reported_as_measured():
    census = vm_census(thermal_c=lambda: 55.0)
    report = evaluate_vm_citizen(census, device_class="vm", simulated=True, now=lambda: FIXED)
    assert report["ready"] is True
    observed = {c["name"]: c["detail"] for c in report["checks"] if c["status"] == "observed"}
    assert "55.0" in observed["thermal_reading"]


# -- the factory wires the whole agent ------------------------------------------------


def _provisioned_config(tmp_path: Path) -> NodeConfig:
    store_dir = tmp_path / "keys"
    store = LocalKeyStore(store_dir)
    root_keypair, root_cert = create_mesh_root(now=FIXED)
    node_keypair = NodeKeypair.generate()
    cert = issue_node_certificate(
        root_keypair, root_cert, node_id=NODE_ID, node_keypair=node_keypair,
        device_class="vm", agent_version="0.0.1", now=FIXED,
    )
    store.save_certificate(root_cert)
    store.save_seed(NODE_ID, node_keypair.seed)
    store.save_certificate(cert)
    return NodeConfig.from_dict({
        "version": 1, "node_id": NODE_ID, "device_class": "vm",
        "key_store_dir": str(store_dir),
    })


def test_the_factory_refuses_a_machine_that_fails_the_profile(tmp_path):
    config = _provisioned_config(tmp_path)
    with pytest.raises(VmCitizenError, match="does not meet the VM citizen profile"):
        build_vm_citizen_agent(
            config,
            store_root=tmp_path / "store",
            probes=vm_probes(accelerators=lambda: [{"kind": "cuda", "name": "Some GPU"}]),
            simulated=True,
            now=lambda: FIXED,
        )


def test_the_factory_builds_the_full_citizen_agent(tmp_path):
    config = _provisioned_config(tmp_path)
    agent, report = build_vm_citizen_agent(
        config,
        store_root=tmp_path / "store",
        probes=vm_probes(),
        readings=lambda: {
            "thermal_c": None,
            "memory_used_bytes": 4 * 1024**3,
            "memory_total_bytes": 8 * 1024**3,
            "cpu_share_used": 0.25,
        },
        neighbours=VM_NEIGHBOURS,
        simulated=True,
        now=lambda: FIXED,
    )
    assert report["ready"] is True
    assert agent.citizen_report == report
    # The profile's governor governs, and the lifecycle's pools
    # are the measured pools: VRAM is zero — a CPU-only citizen
    # has no accelerator pool, absence read as absence — and
    # RAM is the measured RAM inside the governor's fraction,
    # so the reserve binds the budgets, not only the report.
    assert agent.governor.policy["thermal_shed_c"] == 80.0
    assert agent.governor.policy["max_memory_fraction"] == 0.75
    assert agent.governor.policy["max_cpu_share"] == 0.75
    assert agent.model_lifecycle._budgets["vram_bytes"] == 0
    expected_ram = int(report["census"]["ram_gb"] * 0.75 * (1024**3))
    assert agent.model_lifecycle._budgets["ram_bytes"] == expected_ram
    assert agent.model_lifecycle._budgets["max_slots"] == 2

    agent.start()
    agent.join()
    assert agent.status()["citizen"] == report
    assert agent.census()["cpu"]["arch"] == "x86_64"
    assert agent.census()["accelerators"] == [{"kind": "none", "proven": False}]

    # A hash-pinned fixture model, loaded through the lifecycle
    # (SIMULATED loader — residency bookkeeping, no weights move).
    fixture = tmp_path / "fixture-model.gguf"
    fixture.write_bytes(b"SIMULATED fixture model bytes for the VM citizen test")
    agent.model_store.add(
        fixture, name="fixture-model", family="fixture", quant="Q4_K_M", format="gguf"
    )
    agent.load_model("fixture-model")
    assert agent.health()["models_loaded"] == ["fixture-model"]

    task = {
        "id": "33333333-3333-4333-8333-333333333333",
        "kind": "chat.generate",
        "payload": {"prompt": "citizen check"},
        "caller": "vm-citizen-test",
        "criticality": "standard",
        "requirements": {"model": "fixture-model", "capabilities": ["generate"]},
    }
    agent.queue_task(task)
    assert agent.health()["queue_depth"] == 1

    def runner(task, control):
        yield "vm citizen answer"

    outcome = agent.execute_next_queued(runner, engine="ollama", model="fixture-model")
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
