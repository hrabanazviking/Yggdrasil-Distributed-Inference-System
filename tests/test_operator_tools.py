"""Node operator tools tests — Y-042's acceptance.

The slice must prove: (a) the agent's lifecycle grows the
operator's states — pause from joined (present, finishing
in-flight leases, taking nothing new: no leases, no held
work, no fetches), resume back to joined, drain and stop
from paused, and retire as a terminal state (refused with
leases in flight, refused from created, never restartable,
its moment recorded); health names the pause the way it
names a drain, and a heartbeat carries the paused state as
present under the grown lifecycle enum; (b) every verb
ordered through OperatorTools lands as a validated
operator_action v1 in the ledger — actor and stated reason
recorded, states named, refusals recorded with the node's
own words before they are raised — and the report counts
applied and refused apart and states retirement; every
action also lands as a Y-013 event from the operator_cli
component carrying names only; (c) inspect assembles one
validated node_inspection v1 from the agent's own records,
with unwired components null (never dressed as calm zeros)
and a fully wired citizen answering its real facts with the
citizen's own simulated flag; (d) the CLI's ``node``
subcommands drive the tools handed in, print the ledgered
action, refuse by name with no tools handed in, and turn a
node's refusal into exit 2; (e) the founding README is
untouched.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.census import ProbeSet
from yggdrasil.cli import main as cli_main
from yggdrasil.identity import (
    LocalKeyStore,
    NodeKeypair,
    create_mesh_root,
    issue_node_certificate,
)
from yggdrasil.node_agent import (
    JOINED,
    PAUSED,
    RETIRED,
    NodeAgent,
    NodeAgentError,
    NodeConfig,
)
from yggdrasil.observability import validate_event
from yggdrasil.operator_tools import (
    OperatorTools,
    OperatorToolsError,
    validate_action,
    validate_inspection,
    validate_report,
)
from yggdrasil.vm_citizen import build_vm_citizen_agent

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
NODE_ID = "test-node-1"
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"


def provision(store_dir: Path, *, node_id: str = NODE_ID, device_class: str = "vm"):
    """A real Y-011 provisioning in a tmp store (the fixture
    pattern the agent's own tests build, duplicated here —
    test modules never import each other)."""
    store = LocalKeyStore(store_dir)
    root_keypair, root_cert = create_mesh_root(now=FIXED)
    node_keypair = NodeKeypair.generate()
    cert = issue_node_certificate(
        root_keypair,
        root_cert,
        node_id=node_id,
        node_keypair=node_keypair,
        device_class=device_class,
        agent_version="0.0.1",
        now=FIXED,
    )
    store.save_certificate(root_cert)
    store.save_seed(node_id, node_keypair.seed)
    store.save_certificate(cert)
    return store


def make_agent(tmp_path: Path, **kwargs) -> NodeAgent:
    store_dir = tmp_path / "keys"
    provision(store_dir)
    config = NodeConfig.from_dict(
        {
            "version": 1,
            "node_id": NODE_ID,
            "device_class": "vm",
            "key_store_dir": str(store_dir),
        }
    )
    return NodeAgent(config, now=lambda: FIXED, **kwargs)


def joined_agent(tmp_path: Path, **kwargs) -> NodeAgent:
    agent = make_agent(tmp_path, **kwargs)
    agent.start()
    agent.join()
    return agent


# -- the lifecycle grows: pause / resume --------------------------------------


def test_pause_and_resume_round_trip(tmp_path):
    agent = joined_agent(tmp_path)
    status = agent.pause()
    assert status["lifecycle_state"] == PAUSED == agent.state
    assert agent.health()["liveness"] == "present"
    assert agent.health()["governor"] == {"shedding": True, "reason": "paused"}
    status = agent.resume()
    assert status["lifecycle_state"] == JOINED
    types = [event["type"] for event in agent.events]
    assert "node.paused" in types and "node.resumed" in types


def test_a_paused_node_takes_nothing_new_but_finishes_its_leases(tmp_path):
    agent = joined_agent(tmp_path)
    agent.begin_task("task-before-pause")
    agent.pause()
    with pytest.raises(NodeAgentError, match="cannot begin_task from state 'paused'"):
        agent.begin_task("task-after-pause")
    # The lease it already held may finish while paused.
    agent.end_task("task-before-pause")
    assert agent.in_flight == ()
    agent.resume()
    agent.begin_task("task-after-resume")
    agent.end_task("task-after-resume")


def test_pause_is_refused_from_states_that_are_not_joined(tmp_path):
    agent = make_agent(tmp_path)
    with pytest.raises(NodeAgentError, match="cannot pause from state 'created'"):
        agent.pause()
    agent.start()
    with pytest.raises(NodeAgentError, match="cannot pause from state 'started'"):
        agent.pause()
    agent.join()
    agent.pause()
    with pytest.raises(NodeAgentError, match="cannot resume from state 'joined'"):
        joined_agent(tmp_path / "other").resume()


def test_drain_and_stop_from_paused(tmp_path):
    agent = joined_agent(tmp_path)
    agent.pause()
    assert agent.drain()["lifecycle_state"] == "draining"
    other = joined_agent(tmp_path / "second")
    other.pause()
    assert other.stop()["lifecycle_state"] == "stopped"


def test_a_heartbeat_carries_the_paused_state_as_present(tmp_path):
    agent = joined_agent(tmp_path)
    agent.pause()
    beat = agent.heartbeat()
    assert beat["lifecycle_state"] == "paused"
    assert beat["liveness"] == "present"


# -- the lifecycle grows: retire ----------------------------------------------


def test_retire_is_terminal_and_its_moment_is_recorded(tmp_path):
    agent = joined_agent(tmp_path)
    status = agent.retire()
    assert status["lifecycle_state"] == RETIRED == agent.state
    assert status["retired_at"] == "2026-06-01T12:00:00Z"
    assert agent.health()["liveness"] == "gone"
    with pytest.raises(NodeAgentError, match="cannot start from state 'retired'"):
        agent.start()
    with pytest.raises(NodeAgentError, match="cannot retire from state 'retired'"):
        agent.retire()
    assert "node.retired" in [event["type"] for event in agent.events]


def test_retire_refuses_leases_in_flight_and_a_node_that_never_started(tmp_path):
    agent = joined_agent(tmp_path)
    agent.begin_task("task-in-flight")
    with pytest.raises(NodeAgentError, match="retire refused: 1 task"):
        agent.retire()
    agent.end_task("task-in-flight")
    agent.retire()

    fresh = make_agent(tmp_path / "fresh")
    with pytest.raises(NodeAgentError, match="cannot retire from state 'created'"):
        fresh.retire()


# -- the ledger -----------------------------------------------------------------


def test_every_verb_is_ledgered_as_a_validated_action(tmp_path):
    agent = joined_agent(tmp_path)
    tools = OperatorTools(agent, actor="volmarr", now=lambda: FIXED)
    drain = tools.drain(reason="evening maintenance")
    validate_action(drain)
    assert drain["action"] == "drain" and drain["outcome"] == "applied"
    assert drain["from_state"] == "joined" and drain["to_state"] == "draining"
    assert drain["actor"] == "volmarr" and drain["reason"] == "evening maintenance"
    assert drain["seq"] == 0 and drain["simulated"] is False

    other = joined_agent(tmp_path / "other")
    tools = OperatorTools(other, now=lambda: FIXED)
    paused = tools.pause()
    resumed = tools.resume()
    retired = tools.retire(reason="the board is replaced")
    assert [row["seq"] for row in tools.actions()] == [0, 1, 2]
    assert (paused["to_state"], resumed["to_state"], retired["to_state"]) == (
        "paused",
        "joined",
        "retired",
    )
    report = tools.report()
    validate_report(report)
    assert report["actions_total"] == 3 and report["applied"] == 3
    assert report["refused"] == 0 and report["retired"] is True
    assert report["by_action"] == {
        "drain": 0,
        "inspect": 0,
        "pause": 1,
        "resume": 1,
        "retire": 1,
    }
    assert report["last_action_at"] == "2026-06-01T12:00:00Z"


def test_a_refusal_is_recorded_before_it_is_raised(tmp_path):
    agent = make_agent(tmp_path)  # created: nothing may be paused
    tools = OperatorTools(agent, now=lambda: FIXED)
    with pytest.raises(OperatorToolsError, match="cannot pause from state 'created'"):
        tools.pause(reason="too early")
    actions = tools.actions()
    assert len(actions) == 1
    row = actions[0]
    validate_action(row)
    assert row["outcome"] == "refused" and row["to_state"] is None
    assert "cannot pause" in row["refusal"]
    report = tools.report()
    assert report["applied"] == 0 and report["refused"] == 1
    assert report["retired"] is False


def test_actions_land_as_operator_cli_events_with_names_only(tmp_path):
    agent = joined_agent(tmp_path)
    tools = OperatorTools(agent, now=lambda: FIXED)
    tools.pause(reason="a reason that must not ride the event")
    tools.resume()
    events = tools.events
    assert [event["type"] for event in events] == [
        "operator.paused",
        "operator.resumed",
    ]
    for event in events:
        validate_event(event)
        assert event["source"]["component"] == "operator_cli"
        assert "reason" not in json.dumps(event)


def test_an_empty_reason_is_refused_and_never_ledgered(tmp_path):
    tools = OperatorTools(joined_agent(tmp_path), now=lambda: FIXED)
    with pytest.raises(OperatorToolsError, match="reason"):
        tools.drain(reason="  ")
    assert tools.actions() == []


# -- inspect ----------------------------------------------------------------------


def test_inspect_over_a_bare_agent_reports_unwired_as_null(tmp_path):
    agent = make_agent(tmp_path)
    tools = OperatorTools(agent, now=lambda: FIXED)
    inspection = tools.inspect()
    validate_inspection(inspection)
    assert inspection["lifecycle_state"] == "created"
    assert inspection["liveness"] == "gone"
    assert inspection["identity_verified"] is False
    assert inspection["queue_depth"] is None
    assert inspection["journal_entries"] is None
    assert inspection["active_failure_incidents"] is None
    assert inspection["generous"] is None
    assert inspection["degradation_status"] is None
    assert inspection["citizen_ready"] is None
    assert inspection["census_measured"] is False
    assert inspection["fabric_connected"] is False
    # The asking itself is ledgered, and inspecting changed nothing.
    assert tools.report()["by_action"]["inspect"] == 1
    assert agent.state == "created"

    agent.start()
    agent.join()
    agent.pause()
    inspection = tools.inspect()
    assert inspection["lifecycle_state"] == "paused"
    assert inspection["liveness"] == "present"
    assert inspection["identity_verified"] is True
    assert inspection["governor_reason"] == "paused"


# This VM's measured facts (the Y-035 fixture, duplicated here):
# AMD EPYC 9D64, x86_64, 4 cores, 7.749 GiB, exactly one
# accelerator entry of kind "none", no VRAM, no thermal reading.
VM_NEIGHBOURS = {"host_daemon": "active", "thermal_sensor": "absent"}


def vm_probes() -> ProbeSet:
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


def test_inspect_over_a_citizen_answers_its_real_facts(tmp_path):
    node_id = "vm-node-1"
    store_dir = tmp_path / "keys"
    provision(store_dir, node_id=node_id)
    config = NodeConfig.from_dict(
        {
            "version": 1,
            "node_id": node_id,
            "device_class": "vm",
            "key_store_dir": str(store_dir),
        }
    )
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
    agent.start()
    agent.join()
    agent.refresh_census()  # the operator measures; inspect then reports it
    tools = OperatorTools(agent, now=lambda: FIXED)
    inspection = tools.inspect()
    validate_inspection(inspection)
    assert inspection["citizen_ready"] is True
    assert inspection["simulated"] is True  # the citizen's own flag
    assert inspection["queue_depth"] == 0
    assert inspection["journal_entries"] == 0
    assert inspection["active_failure_incidents"] == 0
    assert inspection["census_measured"] is True
    assert inspection["models_present"] == []
    record = tools.pause(reason="citizen check")
    assert record["simulated"] is True
    assert tools.inspect()["lifecycle_state"] == "paused"


# -- the CLI grows ------------------------------------------------------------------


def test_cli_node_verbs_drive_the_tools_handed_in(tmp_path, capsys):
    agent = joined_agent(tmp_path)
    tools = OperatorTools(agent, now=lambda: FIXED)
    assert cli_main(["node", "pause"], tools=tools) == 0
    assert agent.state == "paused"
    assert "pause applied" in capsys.readouterr().out
    assert cli_main(["node", "resume", "--json"], tools=tools) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"]["action"] == "resume"
    assert payload["action"]["outcome"] == "applied"
    assert payload["simulated"] is False
    assert cli_main(["node", "inspect"], tools=tools) == 0
    assert "joined" in capsys.readouterr().out


def test_cli_node_verbs_refuse_by_name_with_no_tools_handed_in(capsys):
    assert cli_main(["node", "drain"]) == 2
    assert "need a node agent handed in" in capsys.readouterr().err


def test_cli_a_nodes_refusal_is_exit_2_not_a_traceback(tmp_path, capsys):
    tools = OperatorTools(joined_agent(tmp_path), now=lambda: FIXED)
    assert cli_main(["node", "resume"], tools=tools) == 2
    assert "cannot resume from state 'joined'" in capsys.readouterr().err
    # …and the refusal is in the ledger all the same.
    assert tools.report()["refused"] == 1


# -- the founding document ----------------------------------------------------------


def test_the_founding_readme_is_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
