"""Operator's door tests — Y-017's acceptance.

Midgard v0 must (a) list nodes from the registry truth — hats,
device class, liveness, capabilities, models, (b) list tasks
from the recorded ResultEnvelopes in finish order, with error
codes named, (c) answer health for all nodes or one, and refuse
an unknown node as an error rather than a blank, (d) label
every output SIMULATED, in text and in JSON, and (e) be
deterministic: the built-in demo mesh is the same door, showing
the same tree, every time it is opened. The door is read-only:
opening it must not add tasks, nodes, or results to its source.
Everything viewed here is SIMULATED fixture data.
"""

import itertools
import json

import pytest

from yggdrasil.cli import CliError, OperatorDoor, build_demo_mesh, main
from yggdrasil.simulator import Simulator

_ids = itertools.count(1)


def task(**overrides):
    envelope = {
        "id": f"3f6f1b2c-9d4e-4c5a-8b7d-{next(_ids):012d}",
        "kind": "chat.generate",
        "payload": {"prompt": "hello"},
        "caller": "cli-test",
        "criticality": "standard",
    }
    envelope.update(overrides)
    return envelope


def small_door():
    sim = Simulator()
    sim.add_fake_node(
        "sim-b", device_class="laptop", roles=["jotunheim"], models=("m-1",), latency_ms=9
    )
    sim.add_fake_node("sim-a", device_class="pi", roles=["svartalfheim"], models=("m-1",))
    return sim, OperatorDoor(sim)


def test_nodes_view_reads_registry_truth_in_id_order():
    _sim, door = small_door()
    rows = door.list_nodes()
    assert [row["node_id"] for row in rows] == ["sim-a", "sim-b"]
    assert rows[0]["device_class"] == "pi"
    assert rows[0]["roles"] == ["svartalfheim"]
    assert rows[0]["liveness"] == "present"
    assert rows[0]["capabilities"] == ["generate"]
    assert rows[0]["models_present"] == ["m-1"]
    assert rows[1]["device_class"] == "laptop"


def test_nodes_view_shows_a_stopped_node_as_gone_with_no_models_loaded():
    sim, door = small_door()
    sim.stop_node("sim-a")
    row = door.list_nodes()[0]
    assert row["liveness"] == "gone"
    assert row["models_loaded"] == []
    # Census truth is kept separate: the model is still on disk.
    assert row["models_present"] == ["m-1"]


def test_tasks_view_is_empty_until_the_source_records_a_task():
    _sim, door = small_door()
    assert door.list_tasks() == []


def test_tasks_view_reports_results_in_finish_order_with_provenance():
    sim, door = small_door()
    first = task(requirements={"model": "m-1"})
    second = task(requirements={"model": "absent-model"})
    sim.submit(first)
    sim.submit(second, to_node="sim-a")  # refused there: the model is absent
    rows = door.list_tasks()
    assert [row["task_id"] for row in rows] == [first["id"], second["id"]]
    assert rows[0]["status"] == "completed"
    assert rows[0]["node_id"] == "sim-a"  # first capable node by id
    assert rows[0]["model"] == "m-1"
    assert rows[0]["error_code"] is None
    assert rows[0]["simulated"] is True


def test_tasks_view_names_the_error_code_of_a_refused_task():
    sim, door = small_door()
    refused = task(requirements={"model": "not-on-this-node"})
    sim.submit(refused, to_node="sim-a")
    (row,) = door.list_tasks()
    assert row["status"] == "refused"
    assert row["error_code"] == "model_not_present"
    assert row["total_ms"] == 0.0


def test_health_view_covers_all_nodes_or_one_and_refuses_ghosts():
    sim, door = small_door()
    reports = door.health()
    assert [report["node_id"] for report in reports] == ["sim-a", "sim-b"]
    (one,) = door.health("sim-b")
    assert one["liveness"] == "present"
    sim.stop_node("sim-b")
    (gone,) = door.health("sim-b")
    assert gone["liveness"] == "gone"
    with pytest.raises(CliError, match="sim-ghost"):
        door.health("sim-ghost")


def test_the_door_is_read_only_opening_it_changes_nothing():
    sim, door = small_door()
    sim.submit(task())
    before = (len(sim.results), len(sim.events), sim.node_ids())
    door.list_nodes()
    door.list_tasks()
    door.health()
    assert (len(sim.results), len(sim.events), sim.node_ids()) == before


def test_demo_mesh_is_deterministic_and_complete():
    first, second = OperatorDoor(build_demo_mesh()), OperatorDoor(build_demo_mesh())
    assert first.list_nodes() == second.list_nodes()
    assert first.list_tasks() == second.list_tasks()
    assert first.health() == second.health()
    assert [row["node_id"] for row in first.list_nodes()] == [
        "sim-alfheim",
        "sim-jotunheim",
        "sim-midgard",
    ]
    assert [row["status"] for row in first.list_tasks()] == ["completed", "completed"]
    liveness = {row["node_id"]: row["liveness"] for row in first.list_nodes()}
    assert liveness == {
        "sim-alfheim": "gone",
        "sim-jotunheim": "present",
        "sim-midgard": "present",
    }


def test_cli_nodes_text_is_labelled_simulated(capsys):
    assert main(["nodes"]) == 0
    out = capsys.readouterr().out
    assert "SIMULATED" in out
    assert "sim-midgard" in out
    assert "3 nodes: 2 present, 1 not" in out


def test_cli_tasks_and_health_text(capsys):
    assert main(["tasks"]) == 0
    out = capsys.readouterr().out
    assert "completed" in out
    assert main(["health", "sim-jotunheim"]) == 0
    out = capsys.readouterr().out
    assert "sim-jotunheim" in out and "present" in out
    assert "sim-alfheim" not in out


def test_cli_json_output_parses_and_carries_the_simulated_label(capsys):
    assert main(["nodes", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "loopback-simulated"
    assert payload["simulated"] is True
    assert payload["nodes"][0]["node_id"] == "sim-alfheim"
    assert main(["tasks", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["tasks"]) == 2
    assert all(row["simulated"] for row in payload["tasks"])


def test_cli_unknown_node_is_exit_2_with_a_named_error_not_a_traceback(capsys):
    assert main(["health", "sim-ghost"]) == 2
    captured = capsys.readouterr()
    assert "sim-ghost" in captured.err
    assert captured.out == ""


def test_cli_over_a_caller_supplied_door(capsys):
    _sim, door = small_door()
    assert main(["nodes"], door=door) == 0
    out = capsys.readouterr().out
    assert "2 nodes: 2 present, 0 not" in out
