"""Idle generosity tests — Y-041's acceptance.

The ledger and the agent must prove: (a) the policy is data —
it validates, the defaults are the stated thresholds, and an
unshaped policy or load is refused by name; (b) a joined,
unloaded node with measured spare memory advertises
``generous`` with its spare (memory bytes/fraction, CPU
share, free queue and model slots) and its measured
capacity (accelerator kinds, loaded models) in validated
``spare_capacity`` v1 data; (c) every busyness withdraws it
with its reason named — work in flight, a held queue, a
shedding governor, a draining or unjoined lifecycle, tight
memory; (d) unmeasured memory withdraws as
``memory_unmeasured`` (never a guessed plenty) while an
unmeasured CPU share is simply reported null and blocks
nothing; (e) flips are transitions — historied in the
``generosity_report`` v1, counted, emitted as validated
Y-013 events, with ``generous_since`` set while generous
and cleared on withdrawal, and a steady state records no
new transition; (f) the node agent evaluates from its own
present facts — joined and idle it advertises, a task lease
in flight withdraws it naming ``in_flight``, the lease's
end restores it, draining withdraws it, an unwired agent
advertises nothing and its status is exactly as before;
(g) the VM citizen factory wires a ledger the built agent
carries; (h) the founding README is untouched.

The loads here are injected fixtures — SIMULATED. No peer
has seen an advertisement (there is no fabric yet, Part
III), and none is claimed to have.
"""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.census import ProbeSet
from yggdrasil.governor import ResourceGovernor
from yggdrasil.identity import (
    LocalKeyStore,
    NodeKeypair,
    create_mesh_root,
    issue_node_certificate,
)
from yggdrasil.idle_generosity import (
    GenerosityError,
    IdleGenerosity,
    default_policy,
    validate_advertisement,
    validate_policy,
    validate_report,
)
from yggdrasil.local_queue import LocalQueue
from yggdrasil.node_agent import NodeAgent, NodeConfig
from yggdrasil.observability import validate_event
from yggdrasil.vm_citizen import build_vm_citizen_agent

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def vm_probes() -> ProbeSet:
    """Probe set shaped from this VM's measured facts (the
    same fixture Y-035's tests use, duplicated here — the
    Y-018 provenance check rightly refuses cross-test
    imports of an undeclared ``tests`` package)."""
    return ProbeSet(
        cpu=lambda: {
            "arch": "x86_64",
            "cores": 4,
            "model": "AMD EPYC 9D64 88-Core Processor",
        },
        ram_gb=lambda: 8124908 / (1024 * 1024),
        accelerators=lambda: [{"kind": "none"}],
        disk_free_gb=lambda: 7.346,
        thermal_c=lambda: None,
        vram_gb=lambda: None,
    )
NODE_ID = "generous-node-1"
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
GIB = 1024**3


def idle_load(**overrides) -> dict:
    load = {
        "lifecycle_state": "joined",
        "in_flight": 0,
        "queue_depth": 0,
        "queue_free_slots": 64,
        "model_slots_free": 3,
        "governor_shedding": False,
        "memory_total_bytes": 16 * GIB,
        "memory_used_bytes": 4 * GIB,
        "cpu_share_used": 0.25,
        "accelerators": ["cuda"],
        "models_loaded": ["fixture-model"],
    }
    load.update(overrides)
    return load


# -- policy as data -----------------------------------------------------------


def test_default_policy_is_valid_and_stated():
    policy = validate_policy(default_policy())
    assert policy["max_in_flight"] == 0
    assert policy["max_queue_depth"] == 0
    assert policy["min_spare_memory_fraction"] == 0.25
    assert policy["withdraw_when_shedding"] is True
    assert policy["withdraw_when_draining"] is True


def test_unshaped_policy_and_load_are_refused():
    with pytest.raises(GenerosityError):
        IdleGenerosity(NODE_ID, {"version": 1, "max_in_flight": -1})
    with pytest.raises(GenerosityError):
        IdleGenerosity("")
    ledger = IdleGenerosity(NODE_ID, now=lambda: FIXED)
    with pytest.raises(GenerosityError, match="in_flight"):
        ledger.evaluate(idle_load(in_flight=-1))
    with pytest.raises(GenerosityError, match="lifecycle_state"):
        ledger.evaluate(idle_load(lifecycle_state=""))
    assert ledger.current() is None
    assert ledger.report()["observations"] == 0


# -- the advertisement ----------------------------------------------------------


def test_an_idle_node_advertises_its_measured_spare():
    ledger = IdleGenerosity(NODE_ID, now=lambda: FIXED)
    advert = validate_advertisement(ledger.evaluate(idle_load()))
    assert advert["generous"] is True
    assert advert["status"] == "generous"
    assert advert["reasons"] == []
    assert advert["spare"]["memory_bytes"] == 12 * GIB
    assert advert["spare"]["memory_fraction"] == 0.75
    assert advert["spare"]["cpu_share"] == 0.75
    assert advert["load"] == {
        "in_flight": 0,
        "queue_depth": 0,
        "queue_free_slots": 64,
        "model_slots_free": 3,
    }
    assert advert["capacity"]["accelerators"] == ["cuda"]
    assert advert["capacity"]["models_loaded"] == ["fixture-model"]
    assert advert["generous_since"] == "2026-06-01T12:00:00Z"
    assert advert["observations"] == 1
    assert ledger.generous is True
    assert ledger.current() == advert


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"in_flight": 1}, "in_flight"),
        ({"queue_depth": 2}, "queue_held"),
        ({"governor_shedding": True}, "governor_shedding"),
        ({"lifecycle_state": "draining"}, "draining"),
        ({"lifecycle_state": "started"}, "not_joined"),
        ({"lifecycle_state": "created"}, "not_joined"),
        ({"memory_used_bytes": 15 * GIB}, "memory_tight"),
    ],
)
def test_every_busyness_withdraws_with_its_reason_named(overrides, reason):
    ledger = IdleGenerosity(NODE_ID, now=lambda: FIXED)
    advert = validate_advertisement(ledger.evaluate(idle_load(**overrides)))
    assert advert["generous"] is False
    assert advert["status"] == "withdrawn"
    assert reason in advert["reasons"]
    assert advert["generous_since"] is None
    assert ledger.generous is False


def test_unmeasured_memory_withdraws_but_unmeasured_cpu_is_null():
    ledger = IdleGenerosity(NODE_ID, now=lambda: FIXED)
    advert = ledger.evaluate(idle_load(memory_used_bytes=None, cpu_share_used=None))
    assert advert["generous"] is False
    assert advert["reasons"] == ["memory_unmeasured"]
    assert advert["spare"]["memory_bytes"] is None
    assert advert["spare"]["memory_fraction"] is None
    assert advert["spare"]["cpu_share"] is None
    # CPU alone unmeasured blocks nothing when memory is proven.
    advert = ledger.evaluate(idle_load(cpu_share_used=None))
    assert advert["generous"] is True
    assert advert["spare"]["cpu_share"] is None


def test_policy_thresholds_are_the_operators_call():
    policy = default_policy()
    policy["max_in_flight"] = 2
    policy["min_spare_memory_fraction"] = 0.5
    ledger = IdleGenerosity(NODE_ID, policy, now=lambda: FIXED)
    assert ledger.evaluate(idle_load(in_flight=2))["generous"] is True
    advert = ledger.evaluate(idle_load(in_flight=3))
    assert advert["reasons"] == ["in_flight"]
    advert = ledger.evaluate(idle_load(memory_used_bytes=10 * GIB))
    assert advert["reasons"] == ["memory_tight"]


# -- transitions, history, events ----------------------------------------------


def test_flips_are_historied_counted_and_emitted():
    ledger = IdleGenerosity(NODE_ID, now=lambda: FIXED)
    ledger.evaluate(idle_load())
    steady = ledger.evaluate(idle_load())
    assert steady["transitions"] == 1  # unevaluated -> generous only
    ledger.evaluate(idle_load(in_flight=1))
    back = ledger.evaluate(idle_load())
    assert back["generous_since"] == "2026-06-01T12:00:00Z"
    report = validate_report(ledger.report())
    assert report["current_status"] == "generous"
    assert report["observations"] == 4
    assert report["generous_observations"] == 3
    assert report["withdrawn_observations"] == 1
    assert report["transitions"] == 3
    assert [row["to_status"] for row in report["history"]] == [
        "generous",
        "withdrawn",
        "generous",
    ]
    assert report["history"][0]["from_status"] == "unevaluated"
    assert report["history"][1]["reasons"] == ["in_flight"]
    assert [e["type"] for e in ledger.events] == [
        "generosity.advertised",
        "generosity.withdrawn",
        "generosity.advertised",
    ]
    for event in ledger.events:
        validate_event(event)
        assert event["source"]["component"] == "node_agent"
    assert ledger.events[1]["payload"]["reasons"] == ["in_flight"]


def test_an_unevaluated_ledger_reports_unevaluated():
    report = validate_report(IdleGenerosity(NODE_ID, now=lambda: FIXED).report())
    assert report["current_status"] == "unevaluated"
    assert report["observations"] == 0
    assert report["history"] == []


# -- the node agent ---------------------------------------------------------------


def _provisioned_agent(tmp_path: Path, **agent_kwargs) -> NodeAgent:
    store_dir = tmp_path / "keys"
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
    config = NodeConfig.from_dict(
        {
            "version": 1,
            "node_id": NODE_ID,
            "device_class": "vm",
            "key_store_dir": str(store_dir),
        }
    )
    return NodeAgent(config, now=lambda: FIXED, **agent_kwargs)


def _readings(memory_used: int = 4 * GIB) -> dict:
    return {
        "thermal_c": None,
        "memory_used_bytes": memory_used,
        "memory_total_bytes": 16 * GIB,
        "cpu_share_used": 0.25,
    }


def test_agent_generosity_end_to_end(tmp_path: Path):
    box = {"readings": _readings()}
    governor = ResourceGovernor(
        NODE_ID, readings=lambda: box["readings"], now=lambda: FIXED
    )
    queue = LocalQueue(NODE_ID, now=lambda: FIXED)
    ledger = IdleGenerosity(NODE_ID, now=lambda: FIXED)
    agent = _provisioned_agent(
        tmp_path,
        governor=governor,
        local_queue=queue,
        idle_generosity=ledger,
    )
    # Before joining, the advertisement is withdrawn: not_joined.
    advert = validate_advertisement(agent.generosity_advertisement())
    assert advert["generous"] is False
    assert "not_joined" in advert["reasons"]
    agent.start()
    agent.join()
    advert = agent.generosity_advertisement()
    assert advert["generous"] is True
    assert advert["spare"]["memory_bytes"] == 12 * GIB
    assert advert["load"]["queue_free_slots"] == 64
    assert agent.status()["generosity"] == advert
    # A lease in flight withdraws it; ending the lease restores it.
    agent.begin_task("11111111-1111-1111-1111-111111111111")
    advert = agent.generosity_advertisement()
    assert advert["generous"] is False
    assert "in_flight" in advert["reasons"]
    agent.end_task("11111111-1111-1111-1111-111111111111")
    assert agent.generosity_advertisement()["generous"] is True
    # Draining withdraws it by name.
    agent.drain()
    advert = agent.generosity_advertisement()
    assert advert["generous"] is False
    assert "draining" in advert["reasons"]
    report = validate_report(agent.generosity_report())
    assert report["transitions"] >= 3
    agent.stop()


def test_an_unwired_agent_advertises_nothing(tmp_path: Path):
    agent = _provisioned_agent(tmp_path)
    assert agent.generosity_advertisement() is None
    assert agent.generosity_report() is None
    assert agent.status()["generosity"] is None


# -- the citizen factories ---------------------------------------------------------


def test_the_vm_citizen_factory_wires_a_generosity_ledger(tmp_path: Path):
    store_dir = tmp_path / "keys"
    store = LocalKeyStore(store_dir)
    root_keypair, root_cert = create_mesh_root(now=FIXED)
    node_keypair = NodeKeypair.generate()
    cert = issue_node_certificate(
        root_keypair,
        root_cert,
        node_id="vm-citizen-1",
        node_keypair=node_keypair,
        device_class="vm",
        agent_version="0.0.1",
        now=FIXED,
    )
    store.save_certificate(root_cert)
    store.save_seed("vm-citizen-1", node_keypair.seed)
    store.save_certificate(cert)
    config = NodeConfig.from_dict(
        {
            "version": 1,
            "node_id": "vm-citizen-1",
            "device_class": "vm",
            "key_store_dir": str(store_dir),
        }
    )
    agent, _report = build_vm_citizen_agent(
        config,
        store_root=tmp_path / "models",
        probes=vm_probes(),
        readings=lambda: {
            "thermal_c": None,
            "memory_used_bytes": 2 * GIB,
            "memory_total_bytes": 8 * GIB,
            "cpu_share_used": 0.1,
        },
        simulated=True,
        now=lambda: FIXED,
        key_store=store,
    )
    assert agent.idle_generosity is not None
    agent.start()
    agent.join()
    advert = validate_advertisement(agent.generosity_advertisement())
    assert advert["node_id"] == "vm-citizen-1"
    agent.stop()


# -- the founding document ----------------------------------------------------------


def test_the_founding_readme_is_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
