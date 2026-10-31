"""Resource governor tests — Y-028's acceptance.

The governor must prove: (a) ceilings are data — a policy is
validated, and a policy with no hysteresis gap or a hard line
under its shed line is refused; (b) thermal shedding latches —
it starts at the shed line, holds while the temperature dips
only under the shed line, and clears only below the resume
line, with start/clear events recorded once per transition;
(c) graceful refusal — while shedding, or when a task's own
footprint would push memory/CPU past its ceiling, admission
is refused BY NAME with projected numbers; work already
admitted is never reconsidered; (d) absence is honesty — an
unknown (None) reading disables only its own check and is
reported as null, never as calm; the system provider reads
this machine's real files and reports its thermal as None
exactly as the census does; (e) the hard temperature only
*recommends* resident shedding — the governor unloads and
cancels nothing; (f) the node agent integration — begin_task
is refused while shedding with no lease taken, health()
reports the verdict (combined with draining when both bite),
status() carries the last state; (g) the founding README is
untouched.
"""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.governor import (
    GovernorError,
    ResourceGovernor,
    default_policy,
    system_readings,
    validate_policy,
)
from yggdrasil.identity import (
    LocalKeyStore,
    NodeKeypair,
    create_mesh_root,
    issue_node_certificate,
)
from yggdrasil.node_agent import NodeAgent, NodeAgentError, NodeConfig

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
NODE_ID = "test-node-1"
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"

POLICY = {
    "version": 1,
    "thermal_shed_c": 80.0,
    "thermal_resume_c": 72.0,
    "thermal_hard_c": 90.0,
    "max_memory_fraction": 0.8,
    "max_cpu_share": 0.75,
}

CALM = {
    "thermal_c": 50.0,
    "memory_used_bytes": 100,
    "memory_total_bytes": 1000,
    "cpu_share_used": 0.1,
}


def make_governor(readings=None):
    box = {"readings": dict(CALM if readings is None else readings)}
    governor = ResourceGovernor(
        NODE_ID, POLICY, readings=lambda: box["readings"], now=lambda: FIXED
    )
    return governor, box


# -- policy as data ---------------------------------------------------------


def test_default_policy_is_valid_and_policy_ordering_is_enforced():
    policy = default_policy()
    assert policy["thermal_resume_c"] < policy["thermal_shed_c"]
    with pytest.raises(GovernorError, match="thermal_resume_c"):
        validate_policy({**POLICY, "thermal_resume_c": 80.0})
    with pytest.raises(GovernorError, match="thermal_hard_c"):
        validate_policy({**POLICY, "thermal_hard_c": 79.0})
    with pytest.raises(GovernorError, match="fails its schema"):
        validate_policy({**POLICY, "max_cpu_share": 1.5})
    with pytest.raises(GovernorError, match="fails its schema"):
        validate_policy({**POLICY, "surprise": True})


# -- thermal hysteresis ---------------------------------------------------------


def test_thermal_shedding_latches_until_the_resume_line():
    governor, box = make_governor()
    state = governor.evaluate()
    assert state["state"] == "within_ceilings"
    assert state["shedding"] is False

    box["readings"] = {**CALM, "thermal_c": 81.0}
    state = governor.evaluate()
    assert state["shedding"] is True
    assert state["reasons"] == ["thermal"]

    # A dip under the shed line but not the resume line: still shedding.
    box["readings"] = {**CALM, "thermal_c": 76.0}
    state = governor.evaluate()
    assert state["shedding"] is True

    # Below the resume line the latch clears — one event each way.
    box["readings"] = {**CALM, "thermal_c": 70.0}
    state = governor.evaluate()
    assert state["shedding"] is False
    types = [e["type"] for e in governor.events]
    assert types == ["governor.shedding_started", "governor.shedding_cleared"]


def test_hard_temperature_only_recommends_resident_shedding():
    governor, _box = make_governor({**CALM, "thermal_c": 95.0})
    state = governor.evaluate()
    assert state["state"] == "critical"
    assert state["reasons"] == ["thermal", "thermal_hard"]
    assert state["shed_resident_models_recommended"] is True
    # The governor itself takes no action against anything.
    assert governor.admissions == 0


# -- graceful refusal ---------------------------------------------------------


def test_memory_and_cpu_ceilings_refuse_by_name_with_projections():
    governor, _box = make_governor({**CALM, "memory_used_bytes": 850})
    state = governor.evaluate()
    assert state["reasons"] == ["memory"]
    assert state["readings"]["memory_fraction"] == 0.85

    governor2, _ = make_governor()
    # Within ceilings now, but this task's footprint crosses both.
    decision = governor2.admit(task_id="t-1", memory_bytes=750, cpu_share=0.7)
    assert decision["admitted"] is False
    assert decision["reasons"] == ["memory", "cpu"]
    assert decision["projected_memory_fraction"] == 0.85
    assert decision["projected_cpu_share"] == 0.8
    assert governor2.refusals == 1
    refused = [e for e in governor2.events if e["type"] == "governor.task_refused"]
    assert refused[0]["payload"]["task_id"] == "t-1"

    ok = governor2.admit(task_id="t-2", memory_bytes=100, cpu_share=0.1)
    assert ok["admitted"] is True
    assert ok["reasons"] == []
    assert governor2.admissions == 1


def test_admission_refused_while_shedding_even_for_a_tiny_task():
    governor, _ = make_governor({**CALM, "cpu_share_used": 0.9})
    decision = governor.admit(task_id="t-1")
    assert decision["admitted"] is False
    assert decision["shedding"] is True
    assert decision["reasons"] == ["cpu"]


def test_bad_requests_and_readings_are_refused():
    governor, _ = make_governor()
    with pytest.raises(GovernorError, match="memory_bytes"):
        governor.admit(memory_bytes=-1)
    with pytest.raises(GovernorError, match="cpu_share"):
        governor.admit(cpu_share=-0.1)
    with pytest.raises(GovernorError, match="unknown reading"):
        governor.evaluate({**CALM, "fan_rpm": 900})


# -- absence is honesty ---------------------------------------------------------


def test_unknown_readings_disable_only_their_own_check():
    governor, _ = make_governor(
        {"thermal_c": None, "memory_used_bytes": None,
         "memory_total_bytes": None, "cpu_share_used": 0.95}
    )
    state = governor.evaluate()
    assert state["readings"]["thermal_c"] is None
    assert state["readings"]["memory_fraction"] is None
    # CPU is known and over its ceiling — that check still bites.
    assert state["reasons"] == ["cpu"]
    # And a memory footprint cannot be projected against an
    # unknown total — admitted, with the projection honestly null.
    governor2, _ = make_governor(
        {"thermal_c": None, "memory_used_bytes": None,
         "memory_total_bytes": None, "cpu_share_used": 0.1}
    )
    decision = governor2.admit(task_id="t-1", memory_bytes=10**9)
    assert decision["admitted"] is True
    assert decision["projected_memory_fraction"] is None


def test_system_readings_read_this_machine_honestly():
    readings = system_readings()
    # This VM exposes no thermal sensor (the census says the
    # same) — unknown, never a cool fiction.
    assert readings["thermal_c"] is None
    assert readings["memory_total_bytes"] > 0
    assert 0 <= readings["memory_used_bytes"] <= readings["memory_total_bytes"]
    assert readings["cpu_share_used"] >= 0


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


def make_agent(tmp_path, box):
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
    governor = ResourceGovernor(
        NODE_ID, POLICY, readings=lambda: box["readings"], now=lambda: FIXED
    )
    agent = NodeAgent(config, now=lambda: FIXED, governor=governor)
    agent.start()
    agent.join()
    return agent, governor


def test_agent_refuses_new_leases_while_shedding_and_keeps_in_flight(tmp_path):
    box = {"readings": dict(CALM)}
    agent, _ = make_agent(tmp_path, box)
    agent.begin_task("early")
    box["readings"] = {**CALM, "thermal_c": 85.0}
    with pytest.raises(NodeAgentError, match="governor refused task late: thermal"):
        agent.begin_task("late")
    # No lease was taken for the refused task; the in-flight
    # task is untouched by the shedding.
    assert agent.in_flight == ("early",)
    health = agent.health()
    assert health["governor"] == {"shedding": True, "reason": "thermal"}
    assert agent.status()["governor"]["state"] == "shedding"
    agent.end_task("early")  # finishing work is always allowed


def test_agent_health_combines_draining_and_governor_reasons(tmp_path):
    box = {"readings": {**CALM, "memory_used_bytes": 900}}
    agent, _ = make_agent(tmp_path, box)
    assert agent.health()["governor"] == {"shedding": True, "reason": "memory"}
    agent.drain()
    health = agent.health()
    assert health["governor"]["shedding"] is True
    assert health["governor"]["reason"] == "draining, memory"


def test_agent_without_governor_keeps_the_old_health_shape(tmp_path):
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
    assert "governor" not in agent.health()
    assert agent.status()["governor"] is None


def test_readme_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
