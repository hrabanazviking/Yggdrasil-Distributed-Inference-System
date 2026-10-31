"""Node health endpoint tests — Y-030's acceptance.

The endpoint must prove: (a) the heartbeat is the validated
node_health v1 shape — liveness, lifecycle, the Y-006 health
report, counted load, models, the governor's full state,
census measured/fingerprint, uptime, and the honesty fields
(fabric_connected False, local-only transport); (b) honesty —
a node never reports itself suspect (a peer's verdict), an
unwired governor reads null readings rather than a calm zero,
an unmeasured census and an unmeasured model presence are
reported as such, and a heartbeat never forces a census
measurement; (c) the sequence — beats are numbered
monotonically per endpoint, a refused path/method consumes
no number, and snapshot/component disagreements (wrong node,
liveness vs lifecycle, queue or models vs the health report)
are refused by name; (d) the local handler — GET /health and
/health/heartbeat answer 200 while present and 503 while
gone, unknown paths 404, non-GET 405, no socket opened;
(e) the node agent integration — heartbeats ride the agent's
own snapshot (leases counted, governor readings carried,
store presence re-hashed at ask time, lifecycle residency,
sandbox executions), every beat lands as a node.heartbeat
Y-013 event, status counts the beats, --check prints one;
(f) the founding README is untouched.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from yggdrasil.census import ProbeSet, census_fingerprint
from yggdrasil.governor import ResourceGovernor
from yggdrasil.health_endpoint import (
    HealthError,
    NodeHealthEndpoint,
    build_node_health,
    validate_node_health,
)
from yggdrasil.identity import (
    LocalKeyStore,
    NodeKeypair,
    create_mesh_root,
    issue_node_certificate,
)
from yggdrasil.model_lifecycle import ModelLifecycle
from yggdrasil.model_store import ModelStore
from yggdrasil.node_agent import NodeAgent, NodeAgentError, NodeConfig, main
from yggdrasil.observability import validate_event
from yggdrasil.sandbox import TaskSandbox

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
NODE_ID = "test-node-1"
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"

CALM = {
    "thermal_c": 50.0,
    "memory_used_bytes": 100,
    "memory_total_bytes": 1000,
    "cpu_share_used": 0.1,
}
POLICY = {
    "version": 1,
    "thermal_shed_c": 80.0,
    "thermal_resume_c": 72.0,
    "thermal_hard_c": 90.0,
    "max_memory_fraction": 0.8,
    "max_cpu_share": 0.75,
}


def health_report(**overrides):
    report = {
        "node_id": NODE_ID,
        "at": "2026-06-01T12:00:00Z",
        "liveness": "present",
        "queue_depth": 0,
        "models_loaded": [],
    }
    report.update(overrides)
    return report


def snapshot(**overrides):
    snap = {
        "lifecycle_state": "joined",
        "health": health_report(),
        "load": {
            "in_flight": 0,
            "queue_depth": 0,
            "active_tasks": 0,
            "executions": 0,
            "thermal_c": None,
            "memory_fraction": None,
            "cpu_share_used": None,
        },
        "models": {"loaded": [], "present": None, "loader_simulated": None},
        "governor_state": None,
        "census": {"measured": False, "measured_at": None, "fingerprint": None},
        "uptime_seconds": 12.5,
    }
    snap.update(overrides)
    return snap


def build(**kwargs):
    args = {
        "node_id": NODE_ID,
        "seq": 0,
        "lifecycle_state": "joined",
        "health": health_report(),
        "load": snapshot()["load"],
        "models": {"loaded": [], "present": None, "loader_simulated": None},
        "governor_state": None,
        "census": {"measured": False, "measured_at": None, "fingerprint": None},
        "uptime_seconds": 12.5,
    }
    args.update(kwargs)
    return build_node_health(**args)


# -- the shape -----------------------------------------------------------------


def test_a_heartbeat_is_the_validated_shape_with_honesty_fields():
    beat = build()
    validate_node_health(beat)
    assert beat["version"] == 1
    assert beat["at"] == "2026-06-01T12:00:00Z"  # the health report's own moment
    assert beat["liveness"] == "present"
    assert beat["fabric_connected"] is False
    assert beat["transport"] == "local-only"
    assert beat["uptime_seconds"] == 12.5
    # Unknown is null, never an invented calm or empty claim.
    assert beat["load"]["thermal_c"] is None
    assert beat["models"]["present"] is None
    assert beat["census"]["measured"] is False
    assert beat["governor_state"] is None


def test_a_node_never_reports_itself_suspect():
    with pytest.raises(HealthError, match="never reports itself suspect"):
        build(health=health_report(liveness="suspect"))


def test_liveness_must_agree_with_the_lifecycle():
    with pytest.raises(HealthError, match="disagrees with lifecycle_state"):
        build(lifecycle_state="stopped")  # health still claims present
    gone = build(
        lifecycle_state="stopped",
        health=health_report(liveness="gone"),
        uptime_seconds=None,
    )
    assert gone["liveness"] == "gone"


def test_component_records_must_agree_with_the_node_and_the_report():
    with pytest.raises(HealthError, match="health report names node"):
        build(health=health_report(node_id="other-node-1"))
    with pytest.raises(HealthError, match="queue_depth disagrees"):
        build(load={**snapshot()["load"], "queue_depth": 3})
    with pytest.raises(HealthError, match="models loaded disagrees"):
        build(models={"loaded": ["ghost"], "present": None, "loader_simulated": None})
    governor = ResourceGovernor(
        "other-node-1", POLICY, readings=lambda: dict(CALM), now=lambda: FIXED
    )
    with pytest.raises(HealthError, match="governor state names node"):
        build(governor_state=governor.evaluate())
    with pytest.raises(HealthError, match="seq"):
        build(seq=-1)
    with pytest.raises(HealthError, match="uptime_seconds"):
        build(uptime_seconds=-0.5)


# -- the endpoint ----------------------------------------------------------------


def test_beats_are_numbered_and_a_refusal_consumes_no_number():
    endpoint = NodeHealthEndpoint(NODE_ID, lambda: snapshot())
    assert endpoint.beats == 0 and endpoint.last is None
    status, body = endpoint.handle("GET", "/health")
    assert status == 200 and body["seq"] == 0
    assert endpoint.handle("GET", "/nope")[0] == 404
    assert endpoint.handle("POST", "/health")[0] == 405
    assert endpoint.beats == 1  # the refusals served no beat
    status, body = endpoint.handle("get", "/health/heartbeat")
    assert status == 200 and body["seq"] == 1
    assert endpoint.beats == 2 and endpoint.last == body


def test_a_gone_node_answers_503_with_a_full_beat():
    gone = snapshot(
        lifecycle_state="stopped",
        health=health_report(liveness="gone"),
        uptime_seconds=None,
    )
    endpoint = NodeHealthEndpoint(NODE_ID, lambda: gone)
    status, body = endpoint.handle("GET", "/health")
    assert status == 503
    validate_node_health(body)
    assert body["liveness"] == "gone"


def test_refusal_bodies_name_themselves():
    endpoint = NodeHealthEndpoint(NODE_ID, lambda: snapshot())
    status, body = endpoint.handle("GET", "/health/ready")
    assert status == 404 and body["error"] == "not_found"
    assert body["known_paths"] == ["/health", "/health/heartbeat"]
    status, body = endpoint.handle("DELETE", "/health")
    assert status == 405 and body["error"] == "method_not_allowed"


def test_a_broken_snapshot_is_a_health_error_not_a_beat():
    def boom():
        raise RuntimeError("probe exploded")

    endpoint = NodeHealthEndpoint(NODE_ID, boom)
    with pytest.raises(HealthError, match="health snapshot failed"):
        endpoint.heartbeat()
    assert endpoint.beats == 0
    endpoint = NodeHealthEndpoint(NODE_ID, lambda: {"health": health_report()})
    with pytest.raises(HealthError, match="missing"):
        endpoint.heartbeat()


# -- node agent integration -------------------------------------------------------


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


class Clock:
    def __init__(self):
        self.moment = FIXED

    def __call__(self):
        return self.moment


def make_agent(tmp_path: Path, clock=None, **kwargs) -> NodeAgent:
    store_dir = tmp_path / "keys"
    provision(store_dir)
    config = NodeConfig.from_dict(
        {"version": 1, "node_id": NODE_ID, "device_class": "vm",
         "key_store_dir": str(store_dir)}
    )
    return NodeAgent(config, now=clock or (lambda: FIXED), **kwargs)


def test_agent_heartbeat_tells_lifecycle_truth(tmp_path):
    clock = Clock()
    agent = make_agent(tmp_path, clock)
    gone = agent.heartbeat()
    assert gone["liveness"] == "gone" and gone["lifecycle_state"] == "created"
    assert gone["uptime_seconds"] is None
    agent.start()
    clock.moment = FIXED + timedelta(seconds=90)
    agent.join()
    agent.begin_task("task-1")
    beat = agent.heartbeat()
    validate_node_health(beat)
    assert beat["seq"] == 1
    assert beat["liveness"] == "present"
    assert beat["uptime_seconds"] == 90.0
    assert beat["load"]["in_flight"] == 1
    assert beat["load"]["active_tasks"] == 1
    assert beat["health"]["liveness"] == "present"
    agent.end_task("task-1")
    agent.drain()
    draining = agent.heartbeat()
    assert draining["lifecycle_state"] == "draining"
    assert draining["health"]["governor"] == {"shedding": True, "reason": "draining"}
    agent.stop()
    stopped = agent.heartbeat()
    assert stopped["liveness"] == "gone" and stopped["uptime_seconds"] is None


def test_every_beat_lands_as_a_node_heartbeat_event(tmp_path):
    agent = make_agent(tmp_path)
    agent.start()
    agent.join()
    agent.heartbeat()
    agent.handle_health("GET", "/health")
    heartbeats = [e for e in agent.events if e["type"] == "node.heartbeat"]
    assert len(heartbeats) == 2
    for event in heartbeats:
        validate_event(event)
        assert event["source"] == {"node_id": NODE_ID, "component": "node_agent"}
        assert event["payload"]["fabric_connected"] is False
    status = agent.status()
    assert status["health_endpoint"] == {"beats": 2, "last_seq": 1}


def test_unwired_readings_and_presence_stay_unknown(tmp_path):
    agent = make_agent(tmp_path)
    agent.start()
    beat = agent.heartbeat()
    assert beat["governor_state"] is None
    assert beat["load"]["thermal_c"] is None
    assert beat["load"]["memory_fraction"] is None
    assert beat["models"]["present"] is None
    assert beat["models"]["loader_simulated"] is None
    assert beat["census"] == {"measured": False, "measured_at": None, "fingerprint": None}


def test_governor_state_and_readings_ride_the_beat(tmp_path):
    box = {"readings": dict(CALM)}
    governor = ResourceGovernor(
        NODE_ID, POLICY, readings=lambda: box["readings"], now=lambda: FIXED
    )
    agent = make_agent(tmp_path, governor=governor)
    agent.start()
    agent.join()
    beat = agent.heartbeat()
    assert beat["governor_state"]["state"] == "within_ceilings"
    assert beat["load"]["thermal_c"] == 50.0
    assert beat["load"]["memory_fraction"] == 0.1
    assert beat["load"]["cpu_share_used"] == 0.1
    box["readings"] = {**CALM, "thermal_c": 95.0}
    hot = agent.heartbeat()
    assert hot["governor_state"]["state"] == "critical"
    assert hot["governor_state"]["shed_resident_models_recommended"] is True


def test_models_present_is_rehashed_and_loaded_is_residency(tmp_path):
    store = ModelStore(tmp_path / "models", now=lambda: FIXED)
    source = tmp_path / "m.gguf"
    source.write_bytes(b"SIMULATED weights")
    store.add(source, name="m", family="test", quant="Q4", format="gguf")
    lifecycle = ModelLifecycle(NODE_ID, store, ram_bytes=10**9, now=lambda: FIXED)
    agent = make_agent(tmp_path, model_store=store, model_lifecycle=lifecycle)
    agent.start()
    agent.join()
    beat = agent.heartbeat()
    assert beat["models"] == {"loaded": [], "present": ["m"], "loader_simulated": True}
    agent.load_model("m")
    loaded = agent.heartbeat()
    assert loaded["models"]["loaded"] == ["m"]
    assert loaded["health"]["models_loaded"] == ["m"]
    # Tamper with the pinned bytes: presence falls off the next
    # beat, because the store re-hashes at ask time (Y-026).
    pinned = tmp_path / "models" / store.entries()[0]["path"]
    pinned.write_bytes(b"tampered")
    tampered = agent.heartbeat()
    assert tampered["models"]["present"] == []


def test_a_measured_census_rides_by_fingerprint_without_forcing_one(tmp_path):
    probes = ProbeSet(
        cpu=lambda: {"arch": "aarch64", "cores": 4},
        ram_gb=lambda: 8.0,
        accelerators=lambda: [{"kind": "none"}],
        disk_free_gb=lambda: 42.5,
        thermal_c=lambda: 51.24,
        vram_gb=lambda: None,
    )
    agent = make_agent(tmp_path, census_probes=probes)
    agent.start()
    beat = agent.heartbeat()
    assert beat["census"]["measured"] is False  # asking did not measure
    census = agent.census()
    beat = agent.heartbeat()
    assert beat["census"]["measured"] is True
    assert beat["census"]["measured_at"] == census["measured_at"]
    assert beat["census"]["fingerprint"] == census_fingerprint(census)
    # No store wired: presence comes from the measured census.
    assert beat["models"]["present"] == census["models_present"]


def test_sandbox_executions_are_counted(tmp_path):
    sandbox = TaskSandbox(NODE_ID, now=lambda: FIXED)
    agent = make_agent(tmp_path, sandbox=sandbox)
    agent.start()
    agent.join()
    task = {
        "id": "12345678-1234-4234-9234-1234567890ab",
        "kind": "generate.text",
        "caller": "test",
        "payload": {"prompt": "hi"},
    }
    agent.execute_task(
        task, lambda t, c: iter(["done"]), engine="fake", model="m"
    )
    beat = agent.heartbeat()
    assert beat["load"]["executions"] == 1
    assert beat["load"]["active_tasks"] == 0


def test_main_check_prints_a_heartbeat(tmp_path, capsys):
    store_dir = tmp_path / "keys"
    provision(store_dir)
    config_path = tmp_path / "node.json"
    config_path.write_text(json.dumps({
        "version": 1,
        "node_id": NODE_ID,
        "device_class": "vm",
        "key_store_dir": str(store_dir),
    }))
    assert main(["--config", str(config_path), "--check"]) == 0
    out = json.loads(capsys.readouterr().out)
    validate_node_health(out["heartbeat"])
    assert out["heartbeat"]["liveness"] == "present"


def test_agent_errors_stay_node_agent_errors(tmp_path):
    agent = make_agent(tmp_path)
    # A snapshot component that breaks mid-beat surfaces in the
    # agent's own error vocabulary, not the endpoint's.
    def broken_snapshot():
        raise HealthError("snapshot is unshaped")

    agent._health_snapshot = broken_snapshot
    with pytest.raises(NodeAgentError, match="snapshot is unshaped"):
        agent.heartbeat()


# -- the founding document -------------------------------------------------------


def test_readme_unchanged():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
