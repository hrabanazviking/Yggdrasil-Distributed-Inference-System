"""Simulator tests — Y-014's acceptance.

The wind tunnel must (a) hold many fake nodes on one machine,
(b) keep every shape it speaks inside the Y-006/Y-007 contracts,
(c) deliver a task end to end — stream events plus exactly one
terminal result with provenance, (d) refuse and fail honestly in
the Y-007 taxonomy, (e) age a stopped node to ``gone`` in every
view, and (f) be deterministic: the same workload on a fresh
tunnel gives the same placement, timings, and timestamps.
Everything here is SIMULATED fixture data; nothing proves any
hardware behaviour.
"""

import itertools

import pytest

from yggdrasil.simulator import (
    TRANSPORT,
    SimulatedNode,
    SimulatedTaskFailure,
    Simulator,
    SimulatorError,
    make_fake_census,
    make_fake_identity,
)

_ids = itertools.count(1)


def task(**overrides):
    envelope = {
        "id": f"3f6f1b2c-9d4e-4c5a-8b7d-{next(_ids):012d}",
        "kind": "chat.generate",
        "payload": {"prompt": "hello"},
        "caller": "simulator-test",
        "criticality": "standard",
    }
    envelope.update(overrides)
    return envelope


def test_transport_is_loopback_and_labelled_simulated():
    sim = Simulator()
    assert sim.transport == TRANSPORT == "loopback-simulated"
    assert sim.is_simulated is True
    assert sim.network_access is False


def test_fake_fixtures_validate_and_census_must_match_identity():
    identity = make_fake_identity("sim-a", device_class="pi", roles=["svartalfheim"])
    census = make_fake_census("sim-a", models=("m-1",))
    node = SimulatedNode(identity, census)
    assert node.node_id == "sim-a"
    assert node.capabilities() == frozenset({"generate"})
    other = make_fake_census("sim-b")
    with pytest.raises(SimulatorError):
        SimulatedNode(identity, other)


def test_many_fake_nodes_on_one_machine():
    sim = Simulator()
    nodes = sim.spawn(30)
    assert len(nodes) == 30
    assert len(sim.node_ids()) == 30
    snapshot = sim.registry_snapshot()
    assert len(snapshot) == 30
    assert snapshot["sim-node-00"]["health"]["liveness"] == "present"
    assert sim.peers("sim-node-00") == tuple(
        sorted(n for n in sim.node_ids() if n != "sim-node-00")
    )


def test_duplicate_and_unknown_nodes_are_refused():
    sim = Simulator()
    sim.add_fake_node("sim-a")
    with pytest.raises(SimulatorError):
        sim.add_fake_node("sim-a")
    with pytest.raises(SimulatorError):
        sim.health("sim-ghost")


def test_submit_end_to_end_stream_result_and_clock():
    sim = Simulator()
    sim.add_fake_node("sim-b", models=("m-1",), latency_ms=7)
    sim.add_fake_node("sim-a", models=("m-1",), latency_ms=5)
    envelope = task(requirements={"model": "m-1", "capabilities": ["generate"]})
    result, stream = sim.submit(envelope)
    # Deterministic stand-in placement: first capable node by id.
    assert result["provenance"]["node_id"] == "sim-a"
    assert result["status"] == "completed"
    assert result["result"] == {"text": "SIMULATED echo: hello", "simulated": True}
    assert result["timings_ms"] == {"queue": 0.0, "run": 5.0, "total": 5.0}
    assert [event["event"] for event in stream] == ["accepted", "started", "completed"]
    assert [event["seq"] for event in stream] == [0, 1, 2]
    assert sim.clock.elapsed_ms == 5.0
    assert sim.results == [result]
    assert any(event["type"] == "task.completed" for event in sim.events)


def test_auto_placement_skips_incapable_and_stopped_nodes():
    sim = Simulator()
    sim.add_fake_node("sim-a", models=("other-model",))
    sim.add_fake_node("sim-b", models=("m-1",))
    sim.add_fake_node("sim-c", models=("m-1",), capabilities=("embed",))
    sim.stop_node("sim-b")
    envelope = task(requirements={"model": "m-1", "capabilities": ["generate"]})
    with pytest.raises(SimulatorError) as caught:
        sim.submit(envelope)
    assert caught.value.error_code == "no_capable_node"
    sim.start_node("sim-b")
    result, _stream = sim.submit(envelope)
    assert result["provenance"]["node_id"] == "sim-b"


def test_targeted_incapable_node_refuses_with_named_error():
    sim = Simulator()
    sim.add_fake_node("sim-a", models=("other-model",))
    envelope = task(requirements={"model": "m-1"})
    result, stream = sim.submit(envelope, to_node="sim-a")
    assert result["status"] == "refused"
    assert result["error"]["code"] == "model_not_present"
    assert result["result"] == {"simulated": True}
    assert [event["event"] for event in stream] == ["accepted", "failed"]


def test_targeted_stopped_node_is_unreachable_and_health_is_gone():
    sim = Simulator()
    sim.add_fake_node("sim-a")
    sim.stop_node("sim-a")
    assert sim.health("sim-a")["liveness"] == "gone"
    assert sim.health("sim-a")["models_loaded"] == []
    assert sim.peers("sim-a") == ()
    with pytest.raises(SimulatorError) as caught:
        sim.submit(task(), to_node="sim-a")
    assert caught.value.error_code == "partition_unreachable"


def test_handler_failure_becomes_a_failed_result_not_a_crash():
    def broken(_task, _node):
        raise SimulatedTaskFailure("engine_error", "SIMULATED engine blew up")

    sim = Simulator()
    sim.add_fake_node("sim-a", handler=broken)
    result, stream = sim.submit(task())
    assert result["status"] == "failed"
    assert result["error"] == {"code": "engine_error", "detail": "SIMULATED engine blew up"}
    assert [event["event"] for event in stream] == ["accepted", "started", "failed"]
    with pytest.raises(SimulatorError):
        SimulatedTaskFailure("not_a_real_code")


def test_malformed_task_is_rejected_before_any_node_sees_it():
    sim = Simulator()
    sim.add_fake_node("sim-a")
    with pytest.raises(SimulatorError):
        sim.submit({"id": "not-a-uuid", "kind": "Chat Generate", "payload": {}})
    assert sim.results == []
    assert sim.node("sim-a").tasks_run == 0


def test_injected_handler_is_the_y015_plug_point():
    def upper(task_envelope, _node):
        return {"text": task_envelope["payload"]["prompt"].upper(), "simulated": True}

    sim = Simulator()
    sim.add_fake_node("sim-a", handler=upper)
    result, _stream = sim.submit(task())
    assert result["result"]["text"] == "HELLO"


def test_runs_are_deterministic_across_fresh_simulators():
    def run():
        sim = Simulator()
        sim.add_fake_node("sim-a", models=("m-1",), latency_ms=5)
        sim.add_fake_node("sim-b", models=("m-1",), latency_ms=9)
        workload = [
            task(id=f"3f6f1b2c-9d4e-4c5a-8b7d-{i:012d}", requirements={"model": "m-1"})
            for i in range(3)
        ]
        results = sim.run_many(workload)
        return results, sim.health("sim-a")["at"], sim.clock.elapsed_ms

    first, second = run(), run()
    assert first == second


def test_virtual_clock_refuses_to_run_backwards():
    sim = Simulator()
    sim.clock.advance(10)
    assert sim.clock.elapsed_ms == 10.0
    with pytest.raises(SimulatorError):
        sim.clock.advance(-1)
