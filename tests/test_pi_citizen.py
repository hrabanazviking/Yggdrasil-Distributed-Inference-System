"""Pi citizen tests — Y-033's acceptance.

The citizen must prove: (a) the profile is data — it loads
from its registered file, validates against its schema, and
a tampered profile (a governor policy that cannot hold, a
citizen that is not its device class, an unknown neighbour
key) is refused by name; the manifest registers it (Y-016);
(b) evaluation is measured — a census shaped from Mimir's
real measured facts (Raspberry Pi 5, aarch64, 4 cores,
15.828 GiB, 41.9 °C, no Hailo device) with Mimir's real
neighbour states evaluates ready, with the reserve computed,
the Hailo claim ``absent``, and every neighbour's action
``none``; (c) the gates bite — an x86 census, a wrong device
class, a small-RAM machine, and a governor ceiling that
leaves no reserve all fail by name and the factory refuses
to dress such a machine as a Pi citizen; (d) the Hailo rule —
detected is unproven, and only a census that itself carries
``proven: true`` (Aesir's ledger grade, Y-024) is repeated
as proven; the citizen never raises the grade; (e) unknown
neighbours never gate and never get invented — missing
observations read ``unknown`` and coexistence holds by the
reserve; (f) the factory wires the whole agent — the profile's
governor, a lifecycle whose RAM pool is the census RAM scaled
by the governor fraction, sandbox, queue, and failure monitor
all live: the agent starts on a Pi-class identity, joins,
runs a queued task end to end, heartbeats, and carries the
citizen report in its status; an unwired agent carries None;
(g) the founding README is untouched.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.census import ProbeSet, measure_census
from yggdrasil.data_discipline import validate_repo
from yggdrasil.identity import LocalKeyStore, NodeKeypair, create_mesh_root, issue_node_certificate
from yggdrasil.node_agent import NodeAgent, NodeConfig
from yggdrasil.pi_citizen import (
    PiCitizenError,
    build_pi_citizen_agent,
    clean_neighbours,
    evaluate_pi_citizen,
    hailo_claim_for,
    load_profile,
    validate_profile,
    validate_report,
)

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
NODE_ID = "mimir-1"
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"

# Mimir's measured facts, read off purpleviking1 on 2026-10-31
# (see docs/PI_CITIZEN.md): Raspberry Pi 5 Model B Rev 1.1,
# aarch64, 4 cores, MemTotal 16598576 kB = 15.828 GiB, thermal
# zone0 41.9 °C, no /dev/hailo0. Injected here as probes, so
# these runs are SIMULATED — the live run is recorded in the doc.
MIMIR_NEIGHBOURS = {
    "samtal_service": "active",
    "aurelia_port": "listening",
    "kiosk_port": "not_listening",
    "hailo_device": "absent",
}


def mimir_probes(**overrides) -> ProbeSet:
    values = {
        "cpu": lambda: {
            "arch": "aarch64",
            "cores": 4,
            "model": "Raspberry Pi 5 Model B Rev 1.1",
        },
        "ram_gb": lambda: 16598576 / (1024 * 1024),
        "accelerators": lambda: [{"kind": "none"}],
        "disk_free_gb": lambda: 202.0,
        "thermal_c": lambda: 41.9,
        "vram_gb": lambda: None,
    }
    values.update(overrides)
    return ProbeSet(**values)


def mimir_census(**probe_overrides) -> dict:
    return measure_census(
        NODE_ID, probes=mimir_probes(**probe_overrides), at=FIXED
    )


# -- the profile is data ------------------------------------------------------


def test_the_profile_loads_and_is_registered():
    profile = load_profile()
    assert profile["profile"] == "yggdrasil-pi-citizen-v1"
    assert profile["citizen"] == profile["device_class"] == "pi"
    assert profile["hailo_rule"] == "never_beyond_aesir_ledger"
    reports = {r.id: r for r in validate_repo()}
    entry = reports["pi_citizen_profile"]
    assert entry.kind == "profile"
    assert entry.version == "yggdrasil-pi-citizen-v1"
    assert entry.path == "profiles/pi_citizen.json"


def test_a_tampered_profile_is_refused_by_name():
    profile = load_profile()
    bad_policy = json.loads(json.dumps(profile))
    bad_policy["governor_policy"]["thermal_resume_c"] = 99.0  # resume above shed
    with pytest.raises(PiCitizenError, match="governor policy"):
        validate_profile(bad_policy)
    renamed = json.loads(json.dumps(profile))
    renamed["citizen"] = "laptop"
    with pytest.raises(PiCitizenError, match="does not match"):
        validate_profile(renamed)
    unknown_key = json.loads(json.dumps(profile))
    unknown_key["neighbours"][0]["observation_key"] = "moon_phase"
    with pytest.raises(PiCitizenError, match="unknown neighbour"):
        validate_profile(unknown_key)


# -- evaluation is measured -----------------------------------------------------


def test_mimirs_measured_shape_evaluates_ready():
    report = evaluate_pi_citizen(
        mimir_census(),
        device_class="pi",
        neighbours=MIMIR_NEIGHBOURS,
        simulated=True,
        now=lambda: FIXED,
    )
    validate_report(report)
    assert report["ready"] is True
    assert report["simulated"] is True
    assert report["node_id"] == NODE_ID
    assert report["citizen"] == "pi"
    gates = {c["name"]: c["status"] for c in report["checks"]}
    assert gates["device_class"] == "pass"
    assert gates["arm_architecture"] == "pass"
    assert gates["cores"] == "pass"
    assert gates["ram"] == "pass"
    assert gates["neighbour_memory_reserve"] == "pass"
    # 15.828 GiB measured, ceiling 0.75 -> a quarter left for the house.
    assert report["memory_reserve_gb"] == pytest.approx(15.828 * 0.25, abs=0.01)
    assert report["hailo_claim"] == "absent"  # Mimir's real reading: no /dev/hailo0
    assert {n["name"]: n["state"] for n in report["neighbours"]} == {
        "samtal": "active",
        "aurelia": "listening",
        "kiosk": "not_listening",
        "hailo": "absent",
    }
    assert all(n["action_taken"] == "none" for n in report["neighbours"])


def test_missing_observations_are_unknown_and_do_not_gate():
    report = evaluate_pi_citizen(
        mimir_census(), device_class="pi", simulated=True, now=lambda: FIXED
    )
    assert report["ready"] is True
    assert all(n["state"] == "unknown" for n in report["neighbours"])
    assert all(n["action_taken"] == "none" for n in report["neighbours"])


def test_unshaped_observations_are_refused():
    profile = load_profile()
    with pytest.raises(PiCitizenError, match="unknown neighbour"):
        clean_neighbours({"weather": "sunny"}, profile)
    with pytest.raises(PiCitizenError, match="not one of"):
        clean_neighbours({"samtal_service": "loud"}, profile)


# -- the gates bite ---------------------------------------------------------------


def test_an_x86_machine_is_not_a_pi_citizen():
    census = mimir_census(
        cpu=lambda: {"arch": "x86_64", "cores": 8, "model": "VM CPU"}
    )
    report = evaluate_pi_citizen(
        census, device_class="pi", simulated=True, now=lambda: FIXED
    )
    assert report["ready"] is False
    gates = {c["name"]: c["status"] for c in report["checks"]}
    assert gates["arm_architecture"] == "fail"


def test_a_wrong_device_class_and_a_small_machine_fail():
    report = evaluate_pi_citizen(
        mimir_census(), device_class="laptop", simulated=True, now=lambda: FIXED
    )
    assert report["ready"] is False
    small = mimir_census(ram_gb=lambda: 2.0)
    report = evaluate_pi_citizen(
        small, device_class="pi", simulated=True, now=lambda: FIXED
    )
    gates = {c["name"]: c["status"] for c in report["checks"]}
    assert gates["ram"] == "fail"
    # 2 GiB at a 0.75 ceiling leaves 0.5 GiB — under the 2 GiB reserve.
    assert gates["neighbour_memory_reserve"] == "fail"
    assert report["ready"] is False


# -- the Hailo rule -----------------------------------------------------------------


def test_hailo_detection_is_unproven_and_only_a_ledger_grade_is_proven():
    detected = mimir_census(accelerators=lambda: [{"kind": "hailo"}])
    assert hailo_claim_for(detected) == "detected_unproven"
    report = evaluate_pi_citizen(
        detected, device_class="pi", simulated=True, now=lambda: FIXED
    )
    assert report["ready"] is True
    assert report["hailo_claim"] == "detected_unproven"
    # The system probes never set proven (Y-022's fake-root proof:
    # a /dev/hailo0 in the tree reads back proven: false), so a
    # census carrying proven: true can only have arrived through
    # an adapter's census seam fed by Aesir's ledger (Y-024).
    # The citizen repeats that grade exactly; it never raises one.
    hand_built = dict(detected)
    hand_built["accelerators"] = [{"kind": "hailo", "proven": True}]
    assert hailo_claim_for(hand_built) == "proven_by_aesir_ledger"


# -- the factory wires the whole agent ------------------------------------------------


def _provisioned_config(tmp_path: Path) -> NodeConfig:
    store_dir = tmp_path / "keys"
    store = LocalKeyStore(store_dir)
    root_keypair, root_cert = create_mesh_root(now=FIXED)
    node_keypair = NodeKeypair.generate()
    cert = issue_node_certificate(
        root_keypair, root_cert, node_id=NODE_ID, node_keypair=node_keypair,
        device_class="pi", agent_version="0.0.1", now=FIXED,
    )
    store.save_certificate(root_cert)
    store.save_seed(NODE_ID, node_keypair.seed)
    store.save_certificate(cert)
    return NodeConfig.from_dict({
        "version": 1, "node_id": NODE_ID, "device_class": "pi",
        "key_store_dir": str(store_dir),
    })


def test_the_factory_refuses_a_machine_that_fails_the_profile(tmp_path):
    config = _provisioned_config(tmp_path)
    with pytest.raises(PiCitizenError, match="does not meet the Pi citizen profile"):
        build_pi_citizen_agent(
            config,
            store_root=tmp_path / "store",
            probes=mimir_probes(cpu=lambda: {"arch": "x86_64", "cores": 8}),
            simulated=True,
            now=lambda: FIXED,
        )


def test_the_factory_builds_the_full_citizen_agent(tmp_path):
    config = _provisioned_config(tmp_path)
    agent, report = build_pi_citizen_agent(
        config,
        store_root=tmp_path / "store",
        probes=mimir_probes(),
        readings=lambda: {
            "thermal_c": 41.9,
            "memory_used_bytes": 2 * 1024**3,
            "memory_total_bytes": 16 * 1024**3,
            "cpu_share_used": 0.25,
        },
        neighbours=MIMIR_NEIGHBOURS,
        simulated=True,
        now=lambda: FIXED,
    )
    assert report["ready"] is True
    assert agent.citizen_report == report
    # The profile's governor governs, and the lifecycle's RAM pool
    # is the measured RAM inside the governor's fraction — the
    # reserve binds the budgets, not only the report.
    assert agent.governor.policy["thermal_shed_c"] == 75.0
    assert agent.governor.policy["max_memory_fraction"] == 0.75
    expected_ram = int(report["census"]["ram_gb"] * 0.75 * (1024**3))
    assert agent.model_lifecycle._budgets["ram_bytes"] == expected_ram
    assert agent.model_lifecycle._budgets["max_slots"] == 2

    agent.start()
    agent.join()
    assert agent.status()["citizen"] == report
    assert agent.census()["cpu"]["arch"] == "aarch64"

    # A hash-pinned fixture model, loaded through the lifecycle
    # (SIMULATED loader — residency bookkeeping, no weights move).
    fixture = tmp_path / "fixture-model.gguf"
    fixture.write_bytes(b"SIMULATED fixture model bytes for the Pi citizen test")
    agent.model_store.add(
        fixture, name="fixture-model", family="fixture", quant="Q4_K_M", format="gguf"
    )
    agent.load_model("fixture-model")
    assert agent.health()["models_loaded"] == ["fixture-model"]

    task = {
        "id": "11111111-1111-4111-8111-111111111111",
        "kind": "chat.generate",
        "payload": {"prompt": "citizen check"},
        "caller": "pi-citizen-test",
        "criticality": "standard",
        "requirements": {"model": "fixture-model", "capabilities": ["generate"]},
    }
    agent.queue_task(task)
    assert agent.health()["queue_depth"] == 1

    def runner(task, control):
        yield "pi citizen answer"

    outcome = agent.execute_next_queued(
        runner, engine="ollama", model="fixture-model"
    )
    result, _events, _report = outcome
    assert result["status"] == "completed"
    assert agent.health()["queue_depth"] == 0
    beat = agent.heartbeat()
    assert beat["liveness"] == "present"
    assert agent.failure_report()["total_incidents"] == 0
    agent.drain()
    agent.stop()


def test_an_agent_without_a_citizen_carries_none(tmp_path):
    config = _provisioned_config(tmp_path)
    agent = NodeAgent(config, now=lambda: FIXED)
    assert agent.citizen_report is None
    assert agent.status()["citizen"] is None


def test_the_founding_readme_is_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
