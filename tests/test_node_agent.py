"""Node agent skeleton tests — Y-021's acceptance.

The skeleton must prove: (a) config is schema-shaped and loads
from a file with its paths read relative to that file; (b) the
agent starts only on a verified Y-011 identity — missing store,
expired or mismatched certificate, a stored key that is not the
certificate's key, and a config that renames the node or its
device class are all refused, with the agent staying down;
(c) the lifecycle walks created → started → joined → draining →
stopped in order, illegal moves are refused by name, a draining
node takes no new task leases, stop refuses while leases are in
flight, and a stopped node can start again on re-verified
identity; (d) health is the Y-006 shape told from lifecycle truth
(gone before start and after stop, governor shedding named while
draining, no models or queue claimed before their slices exist);
(e) every lifecycle move lands as a Y-013 event from the
node_agent component — in memory and in the local JSONL log when
one is configured — and join is honest in data: fabric_connected
stays False, because the fabric is Part III; (f) the systemd
unit's own command line (``-m yggdrasil.node_agent --config …
--check``) runs the lifecycle end to end as a real process.
"""

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from yggdrasil.identity import (
    LocalKeyStore,
    NodeKeypair,
    create_mesh_root,
    issue_node_certificate,
)
from yggdrasil.node_agent import (
    CREATED,
    DRAINING,
    JOINED,
    STARTED,
    STOPPED,
    NodeAgent,
    NodeAgentError,
    NodeConfig,
    main,
)
from yggdrasil.observability import validate_event

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
NODE_ID = "test-node-1"


def provision(store_dir: Path, *, device_class: str = "vm") -> LocalKeyStore:
    """A real Y-011 provisioning in a tmp store: mesh root, one
    node keypair, and a root-issued certificate for it."""
    store = LocalKeyStore(store_dir)
    root_keypair, root_cert = create_mesh_root(now=FIXED)
    node_keypair = NodeKeypair.generate()
    cert = issue_node_certificate(
        root_keypair,
        root_cert,
        node_id=NODE_ID,
        node_keypair=node_keypair,
        device_class=device_class,
        agent_version="0.0.1",
        now=FIXED,
    )
    store.save_certificate(root_cert)
    store.save_seed(NODE_ID, node_keypair.seed)
    store.save_certificate(cert)
    return store


def make_config(store_dir: Path, **overrides) -> NodeConfig:
    data = {
        "version": 1,
        "node_id": NODE_ID,
        "device_class": "vm",
        "key_store_dir": str(store_dir),
    }
    data.update(overrides)
    return NodeConfig.from_dict(data)


def make_agent(tmp_path: Path, **config_overrides) -> NodeAgent:
    store_dir = tmp_path / "keys"
    provision(store_dir)
    return NodeAgent(
        make_config(store_dir, **config_overrides), now=lambda: FIXED
    )


# -- config -----------------------------------------------------------


def test_config_from_dict_and_round_trip(tmp_path):
    config = make_config(tmp_path / "keys", display_name="Test Node")
    assert config.node_id == NODE_ID
    assert config.device_class == "vm"
    assert config.version == 1
    assert NodeConfig.from_dict(config.to_dict()) == config


@pytest.mark.parametrize(
    "data",
    [
        {"version": 2, "node_id": NODE_ID, "device_class": "vm",
         "key_store_dir": "keys"},  # wrong version
        {"version": 1, "node_id": "Bad Node", "device_class": "vm",
         "key_store_dir": "keys"},  # node_id breaks the pattern
        {"version": 1, "node_id": NODE_ID, "device_class": "toaster",
         "key_store_dir": "keys"},  # unknown device class
        {"version": 1, "node_id": NODE_ID, "device_class": "vm"},  # no store
        {"version": 1, "node_id": NODE_ID, "device_class": "vm",
         "key_store_dir": "keys", "surprise": True},  # unknown key
    ],
)
def test_config_refuses_unshaped_data(data):
    with pytest.raises(NodeAgentError, match="node config"):
        NodeConfig.from_dict(data)


def test_config_loads_from_file_with_relative_paths(tmp_path):
    store_dir = tmp_path / "keys"
    provision(store_dir)
    config_path = tmp_path / "node.json"
    config_path.write_text(json.dumps({
        "version": 1,
        "node_id": NODE_ID,
        "device_class": "vm",
        "key_store_dir": "keys",
        "log_file": "logs/agent.jsonl",
    }))
    config = NodeConfig.load(config_path)
    assert config.key_store_dir == store_dir.resolve()
    assert config.log_file == (tmp_path / "logs" / "agent.jsonl").resolve()


def test_config_load_failures_are_named(tmp_path):
    with pytest.raises(NodeAgentError, match="cannot read node config"):
        NodeConfig.load(tmp_path / "absent.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(NodeAgentError, match="not valid JSON"):
        NodeConfig.load(bad)


# -- identity at start --------------------------------------------------


def test_start_verifies_identity_and_reports_it(tmp_path):
    agent = make_agent(tmp_path)
    assert agent.state == CREATED
    assert agent.identity is None
    status = agent.start()
    assert agent.state == STARTED
    assert status["lifecycle_state"] == STARTED
    identity = agent.identity
    assert identity["node_id"] == NODE_ID
    assert identity["device_class"] == "vm"
    assert identity["agent_version"] == "0.0.1"
    assert len(status["key_fingerprint"]) == 64
    # Custody is Y-011's honest local store, and it says so.
    assert status["custody"]["backend"] == "local"


def test_start_refuses_an_empty_store_and_stays_down(tmp_path):
    agent = NodeAgent(make_config(tmp_path / "keys"), now=lambda: FIXED)
    with pytest.raises(NodeAgentError, match="identity does not verify"):
        agent.start()
    assert agent.state == CREATED
    assert agent.identity is None


def test_start_refuses_an_expired_certificate(tmp_path):
    store_dir = tmp_path / "keys"
    provision(store_dir)
    late = FIXED + timedelta(days=366)  # node certs live 365 days
    agent = NodeAgent(make_config(store_dir), now=lambda: late)
    with pytest.raises(NodeAgentError, match="expired"):
        agent.start()
    assert agent.state == CREATED


def test_start_refuses_a_stored_key_that_is_not_the_certificates(tmp_path):
    store_dir = tmp_path / "keys"
    store = provision(store_dir)
    # Replace the seed with a different keypair's (save refuses to
    # overwrite, so the file is swapped the way an attacker or a
    # careless copy would swap it).
    (store_dir / f"{NODE_ID}.key").write_bytes(NodeKeypair.generate().seed)
    agent = NodeAgent(make_config(store_dir), key_store=store, now=lambda: FIXED)
    with pytest.raises(NodeAgentError, match="does not match"):
        agent.start()


def test_start_refuses_a_config_that_disagrees_with_the_certificate(tmp_path):
    store_dir = tmp_path / "keys"
    provision(store_dir, device_class="vm")
    config = make_config(store_dir, device_class="laptop")
    agent = NodeAgent(config, now=lambda: FIXED)
    with pytest.raises(NodeAgentError, match="device_class"):
        agent.start()


def test_start_refuses_a_certificate_naming_another_node(tmp_path):
    store_dir = tmp_path / "keys"
    store = provision(store_dir)
    # The same keypair, validly certified — but under a different
    # node_id, and filed under this node's name. The signature
    # verifies and the key matches; the *name* does not, and the
    # filename is not the identity — the signed body is.
    keypair = store.load_keypair(NODE_ID)
    store2_dir = tmp_path / "keys2"
    store2 = LocalKeyStore(store2_dir)
    root_keypair, root_cert = create_mesh_root(now=FIXED)
    cert_other = issue_node_certificate(
        root_keypair, root_cert, node_id="test-node-2",
        node_keypair=keypair, device_class="vm",
        agent_version="0.0.1", now=FIXED,
    )
    store2.save_certificate(root_cert)
    store2.save_seed(NODE_ID, keypair.seed)
    (store2_dir / f"{NODE_ID}.cert.json").write_text(json.dumps(cert_other))
    agent = NodeAgent(make_config(store2_dir), now=lambda: FIXED)
    with pytest.raises(NodeAgentError, match="certificate names node"):
        agent.start()
    assert agent.state == CREATED


# -- lifecycle ------------------------------------------------------------


def test_the_full_lifecycle_in_order(tmp_path):
    agent = make_agent(tmp_path)
    agent.start()
    agent.join()
    assert agent.state == JOINED
    agent.begin_task("task-1")
    agent.drain()
    assert agent.state == DRAINING
    agent.end_task("task-1")
    status = agent.stop()
    assert agent.state == STOPPED
    assert status["in_flight"] == []


def test_illegal_moves_are_refused_by_name(tmp_path):
    agent = make_agent(tmp_path)
    with pytest.raises(NodeAgentError, match="cannot join from state 'created'"):
        agent.join()
    with pytest.raises(NodeAgentError, match="cannot drain from state 'created'"):
        agent.drain()
    with pytest.raises(NodeAgentError, match="cannot stop from state 'created'"):
        agent.stop()
    with pytest.raises(NodeAgentError, match="cannot begin_task from state"):
        agent.begin_task("task-1")
    agent.start()
    with pytest.raises(NodeAgentError, match="cannot start from state 'started'"):
        agent.start()
    with pytest.raises(NodeAgentError, match="cannot begin_task from state 'started'"):
        agent.begin_task("task-1")  # started but not joined takes no work


def test_task_leases_gate_on_drain_and_stop(tmp_path):
    agent = make_agent(tmp_path)
    agent.start()
    agent.join()
    agent.begin_task("task-1")
    with pytest.raises(NodeAgentError, match="already in flight"):
        agent.begin_task("task-1")
    with pytest.raises(NodeAgentError, match="still in flight"):
        agent.stop()
    agent.drain()
    with pytest.raises(NodeAgentError, match="cannot begin_task from state 'draining'"):
        agent.begin_task("task-2")  # a draining node sheds, never takes
    with pytest.raises(NodeAgentError, match="not in flight"):
        agent.end_task("task-2")
    agent.end_task("task-1")
    agent.stop()
    assert agent.state == STOPPED


def test_a_stopped_node_starts_again_on_reverified_identity(tmp_path):
    agent = make_agent(tmp_path)
    agent.start()
    agent.join()
    agent.stop()
    assert agent.state == STOPPED
    agent.start()
    assert agent.state == STARTED
    assert agent.identity["node_id"] == NODE_ID
    agent.join()
    assert agent.state == JOINED


# -- health & status -------------------------------------------------------


def test_health_is_lifecycle_truth_in_the_contract_shape(tmp_path):
    agent = make_agent(tmp_path)
    gone = agent.health()
    assert gone["liveness"] == "gone"
    assert gone["queue_depth"] == 0  # no queue exists until Y-031
    assert gone["models_loaded"] == []  # no engines exist until Y-023
    assert "governor" not in gone
    agent.start()
    agent.join()
    present = agent.health()
    assert present["liveness"] == "present"
    assert present["node_id"] == NODE_ID
    assert present["at"] == "2026-06-01T12:00:00Z"
    agent.drain()
    draining = agent.health()
    assert draining["liveness"] == "present"
    assert draining["governor"] == {"shedding": True, "reason": "draining"}
    agent.stop()
    assert agent.health()["liveness"] == "gone"


def test_status_never_claims_a_fabric(tmp_path):
    agent = make_agent(tmp_path)
    agent.start()
    status = agent.join()
    assert status["fabric_connected"] is False
    assert status["transport"] == "local-only"
    assert status["simulated"] is False
    assert agent.fabric_connected is False


# -- events ------------------------------------------------------------------


def test_lifecycle_events_are_y013_envelopes_with_sequence(tmp_path):
    agent = make_agent(tmp_path)
    agent.start()
    agent.join()
    agent.drain()
    agent.stop()
    events = agent.events
    assert [event["type"] for event in events] == [
        "node.started",
        "node.joined",
        "node.draining",
        "node.stopped",
    ]
    assert [event["seq"] for event in events] == [0, 1, 2, 3]
    for event in events:
        validate_event(event)
        assert event["source"] == {"node_id": NODE_ID, "component": "node_agent"}
        assert event["payload"]["fabric_connected"] is False
    joined = events[1]
    assert joined["payload"]["join_scope"] == "local-only"


def test_events_reach_the_local_jsonl_log_when_configured(tmp_path):
    log_file = tmp_path / "agent.jsonl"
    agent = make_agent(tmp_path, log_file=str(log_file))
    agent.start()
    agent.join()
    agent.stop()
    lines = log_file.read_text().splitlines()
    assert len(lines) == 3
    for line in lines:
        validate_event(json.loads(line))


# -- the service door ---------------------------------------------------------


def test_main_check_runs_the_whole_lifecycle(tmp_path, capsys):
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
    assert out["status"]["lifecycle_state"] == STOPPED
    assert out["status"]["identity"]["node_id"] == NODE_ID
    # Health is sampled mid-lifecycle, while the node is joined —
    # the live shape, proven present before drain and stop.
    assert out["health"]["liveness"] == "present"


def test_main_reports_a_missing_config_without_a_traceback(tmp_path, capsys):
    assert main(["--config", str(tmp_path / "absent.json"), "--check"]) == 2
    assert "cannot read node config" in capsys.readouterr().err


def test_the_systemd_unit_points_at_this_module():
    unit = (ROOT / "deploy" / "yggdrasil-node.service").read_text()
    assert "-m yggdrasil.node_agent" in unit
    assert "--config" in unit
    # A native service, and the unit says the standing law out loud.
    assert "no Docker" in unit
    assert not any(
        line.startswith(("FROM", "ExecStart=docker", "ExecStart=podman"))
        for line in unit.splitlines()
    )


def test_the_units_own_command_line_runs_as_a_process(tmp_path):
    store_dir = tmp_path / "keys"
    provision(store_dir)
    config_path = tmp_path / "node.json"
    config_path.write_text(json.dumps({
        "version": 1,
        "node_id": NODE_ID,
        "device_class": "vm",
        "key_store_dir": str(store_dir),
    }))
    proc = subprocess.run(
        [sys.executable, "-m", "yggdrasil.node_agent",
         "--config", str(config_path), "--check"],
        capture_output=True, text=True, timeout=60, cwd=ROOT, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["status"]["lifecycle_state"] == STOPPED
