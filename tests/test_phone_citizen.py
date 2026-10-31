"""Phone citizen tests — Y-036's acceptance.

**Honesty first: there is NO phone hardware run in this
slice.** Every run here is injected-probe SIMULATED against
a declared, representative phone shape — round numbers,
chosen (aarch64, 8 cores, 6.0 GiB RAM, 32 GiB disk free,
36 °C, no accelerator entry, no VRAM), *not* measured off
any real phone, and no measured fact about any real phone
is stated anywhere in this module. On-device proof is
NOT PROVEN until a real phone runs the citizen.

The citizen must prove: (a) the profile is data — it loads
from its registered file, validates against its schema
(with this slice's optional small-model/battery fields),
and a tampered profile (a governor policy that cannot
hold, a citizen that is not its device class, an unknown
neighbour key, a missing small-model ceiling, a missing or
inverted battery policy) is refused by name; the manifest
registers it (Y-016); (b) evaluation is measured by the
caller's seam — the representative phone shape with a
healthy battery evaluates ready, with the reserve
computed, the Hailo claim ``absent``, the NOT PROVEN check
recorded, and every neighbour's action ``none``; (c) the
gates bite — an x86 census, a wrong device class, and a
small machine fail by name, and the factory refuses to
dress such a machine as a phone citizen; (d) the battery
governor behaves — unplugged below the shed line sheds,
unplugged below the critical line is critical and gates
readiness (the factory refuses), charging relaxes the shed
line to the critical line and no lower, charging unknown
reads conservatively as unplugged, and an unknown battery
level is never read as full; (e) small models only — the
ceiling helper allows at/under the ceiling and refuses
above it, the ceiling gate fails on a machine whose RAM
pool cannot hold the largest allowed model, and an NPU in
the census does not gate (the citizen's gates are class,
shape, reserve, and battery — not accelerator dressing);
(f) unknown neighbours never gate and never get invented —
missing observations read ``unknown`` and coexistence
holds by the reserve; (g) the factory wires the whole
agent — the profile's governor, a lifecycle whose VRAM
pool is zero and whose RAM pool is the census RAM scaled
by the governor fraction, sandbox, queue, and failure
monitor all live: the agent starts on a phone-class
identity, joins, runs a queued task end to end, heartbeats,
and carries the citizen report in its status — all
SIMULATED; (h) the founding README is untouched.
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
from yggdrasil.phone_citizen import (
    PhoneCitizenError,
    battery_state_for,
    build_phone_citizen_agent,
    clean_battery,
    clean_neighbours,
    evaluate_phone_citizen,
    load_profile,
    model_size_allowed,
    validate_profile,
    validate_report,
)

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
NODE_ID = "phone-sim-1"
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"

# A declared, representative phone shape — SIMULATED.
# These are chosen round numbers for a plausible modern
# phone class, NOT readings from any real phone: no phone
# was measured for this slice, and on-device proof is
# NOT PROVEN until one runs the citizen.
PHONE_NEIGHBOURS = {
    "phone_os": "active",
    "cellular_radio": "present",
}
HEALTHY_BATTERY = {"level_percent": 80, "charging": False}


def phone_probes(**overrides) -> ProbeSet:
    values = {
        "cpu": lambda: {
            "arch": "aarch64",
            "cores": 8,
            "model": "Representative phone-class ARM SoC (SIMULATED shape)",
        },
        "ram_gb": lambda: 6.0,
        "accelerators": lambda: [{"kind": "none"}],
        "disk_free_gb": lambda: 32.0,
        "thermal_c": lambda: 36.0,
        "vram_gb": lambda: None,
    }
    values.update(overrides)
    return ProbeSet(**values)


def phone_census(**probe_overrides) -> dict:
    return measure_census(NODE_ID, probes=phone_probes(**probe_overrides), at=FIXED)


# -- the profile is data ------------------------------------------------------


def test_the_profile_loads_and_is_registered():
    profile = load_profile()
    assert profile["profile"] == "yggdrasil-phone-citizen-v1"
    assert profile["citizen"] == profile["device_class"] == "phone"
    assert profile["max_model_size_gb"] == 1.5
    assert profile["battery_policy"]["shed_below_percent"] == 20
    assert profile["battery_policy"]["critical_below_percent"] == 10
    assert profile["hailo_rule"] == "never_beyond_aesir_ledger"
    reports = {r.id: r for r in validate_repo()}
    entry = reports["phone_citizen_profile"]
    assert entry.kind == "profile"
    assert entry.version == "yggdrasil-phone-citizen-v1"
    assert entry.path == "profiles/phone_citizen.json"


def test_a_tampered_profile_is_refused_by_name():
    profile = load_profile()
    bad_policy = json.loads(json.dumps(profile))
    bad_policy["governor_policy"]["thermal_resume_c"] = 99.0  # resume above shed
    with pytest.raises(PhoneCitizenError, match="governor policy"):
        validate_profile(bad_policy)
    renamed = json.loads(json.dumps(profile))
    renamed["citizen"] = "laptop"
    with pytest.raises(PhoneCitizenError, match="does not match"):
        validate_profile(renamed)
    unknown_key = json.loads(json.dumps(profile))
    unknown_key["neighbours"][0]["observation_key"] = "moon_phase"
    with pytest.raises(PhoneCitizenError, match="unknown neighbour"):
        validate_profile(unknown_key)
    # This citizen's own cross-field rules (Y-036, the shape
    # of Y-034's): a phone profile without its small-model
    # ceiling or its battery policy cannot bite.
    no_ceiling = json.loads(json.dumps(profile))
    del no_ceiling["max_model_size_gb"]
    with pytest.raises(PhoneCitizenError, match="max_model_size_gb"):
        validate_profile(no_ceiling)
    no_battery = json.loads(json.dumps(profile))
    del no_battery["battery_policy"]
    with pytest.raises(PhoneCitizenError, match="battery_policy"):
        validate_profile(no_battery)
    inverted = json.loads(json.dumps(profile))
    inverted["battery_policy"]["critical_below_percent"] = 25  # above the shed line
    with pytest.raises(PhoneCitizenError, match="critical_below_percent"):
        validate_profile(inverted)


# -- evaluation is measured (by the caller's seam) ------------------------------


def test_the_representative_phone_shape_evaluates_ready():
    report = evaluate_phone_citizen(
        phone_census(),
        device_class="phone",
        neighbours=PHONE_NEIGHBOURS,
        battery=HEALTHY_BATTERY,
        simulated=True,
        now=lambda: FIXED,
    )
    validate_report(report)
    assert report["ready"] is True
    assert report["simulated"] is True
    assert report["node_id"] == NODE_ID
    assert report["citizen"] == "phone"
    gates = {c["name"]: c["status"] for c in report["checks"]}
    assert gates["device_class"] == "pass"
    assert gates["arm_architecture"] == "pass"
    assert gates["cores"] == "pass"
    assert gates["ram"] == "pass"
    assert gates["small_model_ceiling"] == "pass"
    assert gates["phone_memory_reserve"] == "pass"
    assert gates["battery_governor"] == "pass"
    # 6.0 GiB in the SIMULATED shape, ceiling 0.50 -> half left for the phone.
    assert report["memory_reserve_gb"] == pytest.approx(6.0 * 0.50, abs=0.01)
    assert report["hailo_claim"] == "absent"
    observed = {c["name"]: c["detail"] for c in report["checks"] if c["status"] == "observed"}
    assert "NOT PROVEN" in observed["on_device_proof"]
    assert "no phone has run this citizen" in observed["on_device_proof"]
    assert "within the battery policy's ceilings" in observed["battery_state"]
    assert {n["name"]: n["state"] for n in report["neighbours"]} == {
        "phone_os": "active",
        "cellular_radio": "present",
    }
    assert all(n["action_taken"] == "none" for n in report["neighbours"])


def test_missing_observations_are_unknown_and_do_not_gate():
    report = evaluate_phone_citizen(
        phone_census(), device_class="phone", simulated=True, now=lambda: FIXED
    )
    assert report["ready"] is True
    assert all(n["state"] == "unknown" for n in report["neighbours"])
    assert all(n["action_taken"] == "none" for n in report["neighbours"])
    # No battery handed in: unknown, recorded as an absence —
    # and the report nowhere dresses it up as a full battery.
    observed = {c["name"]: c["detail"] for c in report["checks"] if c["status"] == "observed"}
    assert "never read as full" in observed["battery_state"]


def test_unshaped_observations_are_refused():
    profile = load_profile()
    with pytest.raises(PhoneCitizenError, match="unknown neighbour"):
        clean_neighbours({"weather": "sunny"}, profile)
    with pytest.raises(PhoneCitizenError, match="not one of"):
        clean_neighbours({"phone_os": "loud"}, profile)
    with pytest.raises(PhoneCitizenError, match="unknown battery"):
        clean_battery({"voltage": 3900})
    with pytest.raises(PhoneCitizenError, match="level_percent"):
        clean_battery({"level_percent": 101})
    with pytest.raises(PhoneCitizenError, match="level_percent"):
        clean_battery({"level_percent": "full"})
    with pytest.raises(PhoneCitizenError, match="charging"):
        clean_battery({"charging": "yes"})


# -- the gates bite ---------------------------------------------------------------


def test_an_x86_machine_is_not_a_phone_citizen():
    census = phone_census(
        cpu=lambda: {"arch": "x86_64", "cores": 16, "model": "Some x86 CPU"}
    )
    report = evaluate_phone_citizen(
        census, device_class="phone", battery=HEALTHY_BATTERY,
        simulated=True, now=lambda: FIXED,
    )
    assert report["ready"] is False
    gates = {c["name"]: c["status"] for c in report["checks"]}
    assert gates["arm_architecture"] == "fail"


def test_a_wrong_device_class_and_a_small_machine_fail():
    report = evaluate_phone_citizen(
        phone_census(), device_class="laptop", battery=HEALTHY_BATTERY,
        simulated=True, now=lambda: FIXED,
    )
    assert report["ready"] is False
    small = phone_census(ram_gb=lambda: 2.0)
    report = evaluate_phone_citizen(
        small, device_class="phone", battery=HEALTHY_BATTERY,
        simulated=True, now=lambda: FIXED,
    )
    gates = {c["name"]: c["status"] for c in report["checks"]}
    assert gates["ram"] == "fail"
    # 2 GiB at a 0.50 ceiling leaves 1.0 GiB — under the 2.0 GiB reserve,
    # and its 1.0 GiB pool cannot hold the 1.5 GiB largest phone model.
    assert gates["phone_memory_reserve"] == "fail"
    assert gates["small_model_ceiling"] == "fail"
    assert report["ready"] is False


def test_an_npu_in_the_census_does_not_gate():
    census = phone_census(accelerators=lambda: [{"kind": "npu"}])
    report = evaluate_phone_citizen(
        census, device_class="phone", battery=HEALTHY_BATTERY,
        simulated=True, now=lambda: FIXED,
    )
    assert report["ready"] is True
    # Detection is presence, never proof — and no Hailo claim appears.
    assert report["hailo_claim"] == "absent"


# -- the battery governor behaves ---------------------------------------------------


def test_the_battery_governor_sheds_and_gates_by_its_policy():
    profile = load_profile()
    # Healthy unplugged: within ceilings.
    state = battery_state_for({"level_percent": 80, "charging": False}, profile)
    assert state["state"] == "within_ceilings"
    assert state["shedding"] is False
    # Unplugged below the 20% shed line: shedding, but the
    # citizen still evaluates ready — shedding is a door
    # closing, not a refusal to exist.
    state = battery_state_for({"level_percent": 15, "charging": False}, profile)
    assert state["state"] == "shedding"
    assert state["shedding"] is True
    report = evaluate_phone_citizen(
        phone_census(), device_class="phone",
        battery={"level_percent": 15, "charging": False},
        simulated=True, now=lambda: FIXED,
    )
    assert report["ready"] is True
    observed = {c["name"]: c["detail"] for c in report["checks"] if c["status"] == "observed"}
    assert "sheds" in observed["battery_state"]
    # Unplugged below the 10% critical line: critical, and
    # the battery gate fails — readiness is refused by name.
    state = battery_state_for({"level_percent": 5, "charging": False}, profile)
    assert state["state"] == "critical"
    report = evaluate_phone_citizen(
        phone_census(), device_class="phone",
        battery={"level_percent": 5, "charging": False},
        simulated=True, now=lambda: FIXED,
    )
    gates = {c["name"]: c["status"] for c in report["checks"]}
    assert gates["battery_governor"] == "fail"
    assert report["ready"] is False
    # Charging unknown reads conservatively as unplugged.
    state = battery_state_for({"level_percent": 5, "charging": None}, profile)
    assert state["state"] == "critical"


def test_charging_relaxes_the_shed_line_to_the_critical_line_only():
    profile = load_profile()
    # 15% on the charger: within ceilings (the shed line
    # relaxed to the critical line).
    state = battery_state_for({"level_percent": 15, "charging": True}, profile)
    assert state["state"] == "within_ceilings"
    assert state["shed_line_percent"] == 10
    # 5% on the charger: shedding, never critical — but the
    # relaxation stops at the critical line, it does not
    # abolish it.
    state = battery_state_for({"level_percent": 5, "charging": True}, profile)
    assert state["state"] == "shedding"
    report = evaluate_phone_citizen(
        phone_census(), device_class="phone",
        battery={"level_percent": 5, "charging": True},
        simulated=True, now=lambda: FIXED,
    )
    assert report["ready"] is True


def test_an_unknown_battery_is_never_read_as_full():
    profile = load_profile()
    state = battery_state_for({"level_percent": None, "charging": None}, profile)
    assert state["state"] == "unknown"
    assert state["reasons"] == ["battery_unknown"]
    assert clean_battery(None) == {"level_percent": None, "charging": None}
    assert clean_battery({}) == {"level_percent": None, "charging": None}


# -- small models only ---------------------------------------------------------------


def test_small_models_only_bites_by_name():
    assert model_size_allowed(1 * 1024**3) is True
    assert model_size_allowed(int(1.5 * 1024**3)) is True
    assert model_size_allowed(2 * 1024**3) is False
    assert model_size_allowed(0) is False


# -- the factory wires the whole agent ------------------------------------------------


def _provisioned_config(tmp_path: Path) -> NodeConfig:
    store_dir = tmp_path / "keys"
    store = LocalKeyStore(store_dir)
    root_keypair, root_cert = create_mesh_root(now=FIXED)
    node_keypair = NodeKeypair.generate()
    cert = issue_node_certificate(
        root_keypair, root_cert, node_id=NODE_ID, node_keypair=node_keypair,
        device_class="phone", agent_version="0.0.1", now=FIXED,
    )
    store.save_certificate(root_cert)
    store.save_seed(NODE_ID, node_keypair.seed)
    store.save_certificate(cert)
    return NodeConfig.from_dict({
        "version": 1, "node_id": NODE_ID, "device_class": "phone",
        "key_store_dir": str(store_dir),
    })


def test_the_factory_refuses_a_machine_that_fails_the_profile(tmp_path):
    config = _provisioned_config(tmp_path)
    with pytest.raises(PhoneCitizenError, match="does not meet the phone citizen profile"):
        build_phone_citizen_agent(
            config,
            store_root=tmp_path / "store",
            probes=phone_probes(
                cpu=lambda: {"arch": "x86_64", "cores": 16, "model": "Some x86 CPU"}
            ),
            battery=HEALTHY_BATTERY,
            simulated=True,
            now=lambda: FIXED,
        )
    # A critically low unplugged battery is refused too.
    with pytest.raises(PhoneCitizenError, match="battery_governor"):
        build_phone_citizen_agent(
            config,
            store_root=tmp_path / "store",
            probes=phone_probes(),
            battery={"level_percent": 5, "charging": False},
            simulated=True,
            now=lambda: FIXED,
        )


def test_the_factory_builds_the_full_citizen_agent(tmp_path):
    config = _provisioned_config(tmp_path)
    agent, report = build_phone_citizen_agent(
        config,
        store_root=tmp_path / "store",
        probes=phone_probes(),
        readings=lambda: {
            "thermal_c": 36.0,
            "memory_used_bytes": 2 * 1024**3,
            "memory_total_bytes": 6 * 1024**3,
            "cpu_share_used": 0.25,
        },
        neighbours=PHONE_NEIGHBOURS,
        battery=HEALTHY_BATTERY,
        simulated=True,
        now=lambda: FIXED,
    )
    assert report["ready"] is True
    assert report["simulated"] is True
    assert agent.citizen_report == report
    # The profile's governor governs, and the lifecycle's pools
    # are the measured pools: VRAM is zero — this citizen's
    # census carries none, absence read as absence — and RAM
    # is the measured RAM inside the governor's fraction, so
    # the reserve binds the budgets, not only the report.
    assert agent.governor.policy["thermal_shed_c"] == 45.0
    assert agent.governor.policy["max_memory_fraction"] == 0.5
    assert agent.governor.policy["max_cpu_share"] == 0.5
    assert agent.model_lifecycle._budgets["vram_bytes"] == 0
    expected_ram = int(report["census"]["ram_gb"] * 0.5 * (1024**3))
    assert agent.model_lifecycle._budgets["ram_bytes"] == expected_ram
    assert agent.model_lifecycle._budgets["max_slots"] == 1

    agent.start()
    agent.join()
    assert agent.status()["citizen"] == report
    assert agent.census()["cpu"]["arch"] == "aarch64"
    assert agent.census()["accelerators"] == [{"kind": "none", "proven": False}]

    # A hash-pinned fixture model under the small-model
    # ceiling, loaded through the lifecycle (SIMULATED
    # loader — residency bookkeeping, no weights move, and
    # no phone is involved anywhere in this run).
    fixture = tmp_path / "fixture-model.gguf"
    fixture.write_bytes(b"SIMULATED fixture model bytes for the phone citizen test")
    assert model_size_allowed(fixture.stat().st_size) is True
    agent.model_store.add(
        fixture, name="fixture-model", family="fixture", quant="Q4_K_M", format="gguf"
    )
    agent.load_model("fixture-model")
    assert agent.health()["models_loaded"] == ["fixture-model"]

    task = {
        "id": "44444444-4444-4444-8444-444444444444",
        "kind": "chat.generate",
        "payload": {"prompt": "citizen check"},
        "caller": "phone-citizen-test",
        "criticality": "standard",
        "requirements": {"model": "fixture-model", "capabilities": ["generate"]},
    }
    agent.queue_task(task)
    assert agent.health()["queue_depth"] == 1

    def runner(task, control):
        yield "phone citizen answer (SIMULATED)"

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
