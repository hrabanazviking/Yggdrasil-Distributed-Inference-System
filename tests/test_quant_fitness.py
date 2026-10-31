"""Quantisation fitness tests — Y-038's acceptance.

Fitness must prove: (a) the policy is data — it loads from
its registered file (Y-016 manifest, kind ``policy``),
validates against its schema, and a tampered policy is
refused; the class floors are read from the citizen profiles
themselves, never a second copy; (b) class scope — every
quant of a registry entry is judged against every profiled
class's floor: a small gguf quant fits everywhere, a 7B-class
Q4_K_M exceeds the Pi/VM class RAM share but fits the laptop
class, the phone profile's small-model ceiling binds, a
CUDA-format quant fits only the laptop class and only under
its VRAM floor, an MLX quant fits no profiled class, an
unknown format is never assumed runnable, and a class with
no citizen profile is reported not-evaluated; the verdicts
earn the entry's ``runnable_on`` and the store stamps it only
when a judge is handed in; (c) node scope — the same quant
judged against a real measured census: Mimir's actual RAM
fits what the Pi class floor refused (the class guarantee is
conservative, the node decides), a too-large quant is refused
against this VM's measured RAM, a CUDA format on a CUDA-less
node is refused by name, an unmeasured VRAM refuses an
accelerator quant, and the phone ceiling binds a phone node
whose RAM alone would fit; (d) enforcement at placement —
a lifecycle wired with the judge refuses an unfit load
before the loader is asked, records the refusal, and loads
a fit quant exactly as before; an unwired lifecycle claims
no fitness; (e) the citizens and the agent — a Pi citizen
factory agent carries the judge in its status, answers
pre-flight verdicts, loads its fit fixture and refuses an
unfit format at its own door; an unwired agent carries
None; (f) the founding README is untouched.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.census import ProbeSet
from yggdrasil.data_discipline import validate_repo
from yggdrasil.identity import (
    LocalKeyStore,
    NodeKeypair,
    create_mesh_root,
    issue_node_certificate,
)
from yggdrasil.model_lifecycle import ModelLifecycle, ModelLifecycleError
from yggdrasil.model_store import ModelStore
from yggdrasil.node_agent import NodeAgent, NodeAgentError, NodeConfig
from yggdrasil.pi_citizen import build_pi_citizen_agent
from yggdrasil.quant_fitness import (
    QuantFitness,
    QuantFitnessError,
    evaluate_entry,
    evaluate_quant_for_class,
    evaluate_quant_for_node,
    load_class_profiles,
    load_policy,
    validate_policy,
    validate_report,
    validate_verdict,
    with_runnable_on,
)

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
NODE_ID = "mimir-1"

POLICY = load_policy()
PROFILES = load_class_profiles()

GGUF_TINY = {"name": "IQ1_S", "format": "gguf", "size_gb": 1.0}
GGUF_7B_Q4 = {"name": "Q4_K_M", "format": "gguf", "size_gb": 4.4}
GGUF_7B_Q8 = {"name": "Q8_0", "format": "gguf", "size_gb": 8.0}
AWQ_3G = {"name": "AWQ", "format": "awq", "size_gb": 3.0}
AWQ_4G = {"name": "AWQ", "format": "awq", "size_gb": 4.0}
MLX_2G = {"name": "MLX", "format": "mlx", "size_gb": 2.0}
UNKNOWN = {"name": "X9", "format": "madeup", "size_gb": 1.0}


def entry(*quants):
    return {
        "name": "fixture-7b",
        "family": "fixture",
        "quants": list(quants),
        "capabilities": ["generate"],
        "runnable_on": [],
    }


def census(node_id, *, ram_gb, accelerators, vram_gb="absent"):
    record = {
        "node_id": node_id,
        "measured_at": "2026-06-01T12:00:00Z",
        "cpu": {"arch": "aarch64", "cores": 4},
        "ram_gb": ram_gb,
        "accelerators": accelerators,
        "engines": [],
    }
    if vram_gb != "absent":
        record["vram_gb"] = vram_gb
    return record


# Mimir's measured facts (Y-033) and this VM's (Y-035), as censuses.
MIMIR = census(NODE_ID, ram_gb=15.828, accelerators=[{"kind": "none"}])
VM = census(
    "vm-1", ram_gb=7.749, accelerators=[{"kind": "none"}]
)
LAPTOP = census(
    "laptop-1",
    ram_gb=38.591,
    accelerators=[{"kind": "cuda", "name": "RTX 2060 Max-Q"}],
    vram_gb=6.0,
)
LAPTOP_NO_VRAM = census(
    "laptop-1",
    ram_gb=38.591,
    accelerators=[{"kind": "cuda", "name": "RTX 2060 Max-Q"}],
)
PHONE = census("phone-1", ram_gb=6.0, accelerators=[{"kind": "none"}])


def codes(verdict):
    return [r["code"] for r in verdict["reasons"]]


# -- the policy is data -------------------------------------------------------


def test_the_policy_loads_and_is_registered():
    assert POLICY["policy"] == "yggdrasil-quant-fitness-v1"
    assert POLICY["headroom_factor"] == 1.25
    assert POLICY["on_unknown_format"] == "refuse"
    reports = {r.id: r for r in validate_repo()}
    registered = reports["quant_fitness_policy"]
    assert registered.kind == "policy"
    assert registered.version == "yggdrasil-quant-fitness-v1"
    assert registered.path == "policies/quant_fitness.json"
    manifest = json.loads((ROOT / "data" / "data_manifest.json").read_text())
    manifest_entry = next(
        e for e in manifest["entries"] if e["id"] == "quant_fitness_policy"
    )
    assert manifest_entry["roadmap_slice"] == "Y-038"


def test_a_tampered_policy_is_refused():
    bad = json.loads(json.dumps(POLICY))
    bad["headroom_factor"] = 0.5  # headroom below 1 is a lie about size
    with pytest.raises(QuantFitnessError, match="fails its schema"):
        validate_policy(bad)
    bad2 = json.loads(json.dumps(POLICY))
    bad2["format_requirements"] = [{"format": "gguf"}]  # missing the requirement
    with pytest.raises(QuantFitnessError, match="fails its schema"):
        validate_policy(bad2)


def test_class_floors_come_from_the_profiles_themselves():
    assert sorted(PROFILES) == ["laptop", "phone", "pi", "vm"]
    # The phone ceiling and the laptop VRAM floor are the
    # profiles' own numbers (Y-036 / Y-034), read not copied.
    assert PROFILES["phone"]["max_model_size_gb"] == 1.5
    assert PROFILES["laptop"]["min_vram_gb"] == 4.0
    assert PROFILES["laptop"]["required_accelerator_kind"] == "cuda"


# -- class scope ---------------------------------------------------------------


def test_a_small_gguf_quant_fits_every_profiled_class():
    for device_class in ("pi", "laptop", "vm", "phone"):
        verdict = evaluate_quant_for_class(
            "fixture-7b", GGUF_TINY, device_class, policy=POLICY, profiles=PROFILES
        )
        validate_verdict(verdict)
        assert verdict["fit"] is True, (device_class, verdict["reasons"])
        assert verdict["scope"] == "class" and verdict["node_id"] is None


def test_a_7b_q4_exceeds_the_pi_and_vm_class_floors_but_fits_the_laptop():
    for device_class in ("pi", "vm"):
        verdict = evaluate_quant_for_class(
            "fixture-7b", GGUF_7B_Q4, device_class, policy=POLICY, profiles=PROFILES
        )
        assert verdict["fit"] is False
        assert codes(verdict) == ["exceeds_class_ram_share"]
    laptop = evaluate_quant_for_class(
        "fixture-7b", GGUF_7B_Q4, "laptop", policy=POLICY, profiles=PROFILES
    )
    assert laptop["fit"] is True


def test_the_phone_ceiling_binds_at_class_scope():
    verdict = evaluate_quant_for_class(
        "fixture-7b", GGUF_7B_Q4, "phone", policy=POLICY, profiles=PROFILES
    )
    assert verdict["fit"] is False
    assert "exceeds_class_model_ceiling" in codes(verdict)


def test_a_cuda_format_fits_only_the_laptop_class_and_only_under_its_vram_floor():
    fit = evaluate_quant_for_class(
        "fixture-7b", AWQ_3G, "laptop", policy=POLICY, profiles=PROFILES
    )
    assert fit["fit"] is True  # 3.0 × 1.25 = 3.75 ≤ 4.0 GiB floor
    over = evaluate_quant_for_class(
        "fixture-7b", AWQ_4G, "laptop", policy=POLICY, profiles=PROFILES
    )
    assert over["fit"] is False
    assert codes(over) == ["exceeds_class_vram_floor"]
    for device_class in ("pi", "vm", "phone"):
        verdict = evaluate_quant_for_class(
            "fixture-7b", AWQ_3G, device_class, policy=POLICY, profiles=PROFILES
        )
        assert verdict["fit"] is False
        assert "accelerator_not_in_class" in codes(verdict)


def test_mlx_fits_no_profiled_class_and_unknown_formats_are_never_assumed():
    for device_class in ("pi", "laptop", "vm", "phone"):
        mlx = evaluate_quant_for_class(
            "fixture-7b", MLX_2G, device_class, policy=POLICY, profiles=PROFILES
        )
        assert mlx["fit"] is False
        assert "accelerator_not_in_class" in codes(mlx)
        unknown = evaluate_quant_for_class(
            "fixture-7b", UNKNOWN, device_class, policy=POLICY, profiles=PROFILES
        )
        assert unknown["fit"] is False
        assert codes(unknown) == ["format_unknown"]


def test_a_class_with_no_profile_is_reported_not_evaluated():
    verdict = evaluate_quant_for_class(
        "fixture-7b", GGUF_TINY, "server", policy=POLICY, profiles=PROFILES
    )
    assert verdict["fit"] is False
    assert codes(verdict) == ["no_class_profile"]


def test_the_report_earns_runnable_on_and_the_entry_is_stamped():
    report = evaluate_entry(
        entry(GGUF_TINY, GGUF_7B_Q4, AWQ_3G), policy=POLICY, profiles=PROFILES
    )
    validate_report(report)
    # The tiny quant alone earns every class; verdicts cover
    # every quant × every profiled class.
    assert report["runnable_on"] == ["laptop", "phone", "pi", "vm"]
    assert len(report["verdicts"]) == 3 * 4
    big_only = evaluate_entry(entry(GGUF_7B_Q4), policy=POLICY, profiles=PROFILES)
    assert big_only["runnable_on"] == ["laptop"]
    stamped = with_runnable_on(
        entry(GGUF_7B_Q4), policy=POLICY, profiles=PROFILES
    )
    assert stamped["runnable_on"] == ["laptop"]


# -- node scope ----------------------------------------------------------------


def test_node_scope_decides_what_the_class_floor_refused():
    # The Pi class floor refused the 7B Q4 (smallest certified
    # Pi); Mimir itself, measured at 15.828 GiB, fits it — the
    # class guarantee is conservative and the node decides.
    verdict = evaluate_quant_for_node(
        "fixture-7b", GGUF_7B_Q4, device_class="pi", census=MIMIR,
        policy=POLICY, profiles=PROFILES,
    )
    assert verdict["fit"] is True
    assert verdict["scope"] == "node" and verdict["node_id"] == NODE_ID


def test_node_scope_refuses_what_the_measured_machine_cannot_hold():
    verdict = evaluate_quant_for_node(
        "fixture-7b", GGUF_7B_Q8, device_class="vm", census=VM,
        policy=POLICY, profiles=PROFILES,
    )
    assert verdict["fit"] is False
    assert codes(verdict) == ["exceeds_node_ram"]  # 8.0 × 1.25 = 10 > 7.749


def test_node_scope_names_a_missing_accelerator_and_unmeasured_vram():
    missing = evaluate_quant_for_node(
        "fixture-7b", AWQ_3G, device_class="vm", census=VM,
        policy=POLICY, profiles=PROFILES,
    )
    assert codes(missing) == ["accelerator_missing_on_node"]
    unmeasured = evaluate_quant_for_node(
        "fixture-7b", AWQ_3G, device_class="laptop", census=LAPTOP_NO_VRAM,
        policy=POLICY, profiles=PROFILES,
    )
    assert codes(unmeasured) == ["vram_unmeasured_on_node"]
    fits = evaluate_quant_for_node(
        "fixture-7b", AWQ_4G, device_class="laptop", census=LAPTOP,
        policy=POLICY, profiles=PROFILES,
    )
    assert fits["fit"] is True  # 5.0 ≤ 6.0 GiB measured VRAM


def test_the_phone_ceiling_binds_a_phone_node_whose_ram_would_fit():
    verdict = evaluate_quant_for_node(
        "fixture-7b", GGUF_7B_Q4, device_class="phone", census=PHONE,
        policy=POLICY, profiles=PROFILES,
    )
    assert verdict["fit"] is False
    assert codes(verdict) == ["exceeds_class_model_ceiling"]


# -- enforcement at placement ---------------------------------------------------


def make_store(tmp_path, *, quant="Q4_K_M", format="gguf", data=b"weights"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = ModelStore(tmp_path / "store", now=lambda: FIXED)
    source = tmp_path / "model.bin"
    source.write_bytes(data)
    store.add(
        source, name="fixture-7b", family="fixture", quant=quant, format=format
    )
    return store


def test_a_wired_lifecycle_refuses_an_unfit_quant_before_the_loader(tmp_path):
    store = make_store(tmp_path, quant="AWQ", format="awq")
    judge = QuantFitness(NODE_ID, device_class="vm", census=VM)
    lifecycle = ModelLifecycle(
        NODE_ID, store, ram_bytes=10 * 1024**3, max_slots=2,
        fitness_checker=judge, now=lambda: FIXED,
    )
    with pytest.raises(ModelLifecycleError, match="accelerator_missing_on_node"):
        lifecycle.load("fixture-7b")
    assert lifecycle.loaded_models() == []
    refusals = [e for e in lifecycle.events if e["type"] == "model.load_refused"]
    assert len(refusals) == 1


def test_a_wired_lifecycle_loads_a_fit_quant_and_an_unwired_one_judges_nothing(tmp_path):
    store = make_store(tmp_path)
    judge = QuantFitness(NODE_ID, device_class="vm", census=VM)
    wired = ModelLifecycle(
        NODE_ID, store, ram_bytes=10 * 1024**3, max_slots=2,
        fitness_checker=judge, now=lambda: FIXED,
    )
    record = wired.load("fixture-7b")
    assert record["name"] == "fixture-7b"
    # The same AWQ store entry loads under an unwired lifecycle:
    # no gate wired, no fitness claimed — enforcement is the
    # wiring's act, stated in the doc.
    store2 = make_store(tmp_path / "second", quant="AWQ", format="awq")
    unwired = ModelLifecycle(
        NODE_ID, store2, ram_bytes=10 * 1024**3, max_slots=2, now=lambda: FIXED
    )
    assert unwired.load("fixture-7b")["name"] == "fixture-7b"


def test_check_store_entry_judges_the_pinned_quant(tmp_path):
    store = make_store(tmp_path, quant="AWQ", format="awq")
    judge = QuantFitness(NODE_ID, device_class="laptop", census=LAPTOP)
    verdict = judge.check_store_entry(store.entry("fixture-7b"))
    assert verdict["fit"] is True
    assert verdict["quant"]["format"] == "awq"
    vm_judge = QuantFitness(NODE_ID, device_class="vm", census=VM)
    assert vm_judge(store.entry("fixture-7b"))["fit"] is False


def test_the_store_stamps_runnable_on_only_when_a_judge_is_handed_in(tmp_path):
    store = make_store(tmp_path)
    plain = store.model_entries()
    assert plain[0]["runnable_on"] == []
    judge = QuantFitness(NODE_ID, device_class="vm", census=VM)
    stamped = store.model_entries(quant_fitness=judge)
    # The fixture file is a few bytes: its quant fits every
    # profiled class floor, so the stamp names them all.
    assert stamped[0]["runnable_on"] == ["laptop", "phone", "pi", "vm"]


# -- the citizens and the agent ---------------------------------------------------


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


def _mimir_probes() -> ProbeSet:
    return ProbeSet(
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


def test_the_pi_citizen_agent_carries_and_enforces_the_judge(tmp_path):
    config = _provisioned_config(tmp_path)
    agent, report = build_pi_citizen_agent(
        config,
        store_root=tmp_path / "store",
        probes=_mimir_probes(),
        neighbours={
            "samtal_service": "active", "aurelia_port": "listening",
            "kiosk_port": "not_listening", "hailo_device": "absent",
        },
        simulated=True,
        now=lambda: FIXED,
    )
    assert report["ready"] is True
    summary = agent.status()["quant_fitness"]
    assert summary["policy"] == "yggdrasil-quant-fitness-v1"
    assert summary["device_class"] == "pi"
    verdicts = agent.quant_verdicts(entry(GGUF_7B_Q4, AWQ_3G))
    by_quant = {v["quant"]["name"]: v for v in verdicts}
    # Mimir's measured RAM fits the Q4 the Pi class floor refused.
    assert by_quant["Q4_K_M"]["fit"] is True
    assert by_quant["AWQ"]["fit"] is False
    assert codes(by_quant["AWQ"]) == ["accelerator_missing_on_node"]

    agent.start()
    agent.join()
    fixture = tmp_path / "fixture.gguf"
    fixture.write_bytes(b"SIMULATED fixture bytes")
    agent.model_store.add(
        fixture, name="fit-model", family="fixture", quant="Q4_K_M", format="gguf"
    )
    agent.load_model("fit-model")
    assert agent.health()["models_loaded"] == ["fit-model"]
    agent.model_store.add(
        fixture, name="unfit-model", family="fixture", quant="AWQ", format="awq"
    )
    with pytest.raises(NodeAgentError, match="accelerator_missing_on_node"):
        agent.load_model("unfit-model")
    assert agent.health()["models_loaded"] == ["fit-model"]
    agent.drain()
    agent.stop()


def test_an_unwired_agent_carries_no_fitness(tmp_path):
    config = _provisioned_config(tmp_path)
    agent = NodeAgent(config, now=lambda: FIXED)
    assert agent.quant_fitness is None
    assert agent.status()["quant_fitness"] is None
    with pytest.raises(NodeAgentError, match="no quant fitness judge"):
        agent.quant_verdicts(entry(GGUF_TINY))


def test_the_founding_readme_is_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
