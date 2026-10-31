"""Fake engine tests — Y-015's acceptance.

The stub engines must (a) answer deterministically — same task,
same answer, on any fresh engine; (b) shape latency by an exact
declared formula, and have that shape honoured by the simulator
(timings + virtual clock); (c) fail only where their declared
rule says, with named Y-007 error codes, replaying identically;
(d) describe themselves as validated, honestly-SIMULATED data;
and (e) never bypass the tunnel's capability discipline.
Everything here is SIMULATED fixture behaviour; nothing proves
any real engine or device behaviour.
"""

import itertools

import pytest

from yggdrasil.fake_engines import (
    EMBEDDING_DIM,
    FAKE_ENGINE_KINDS,
    EchoEngine,
    FailureEngine,
    FakeEngineError,
    LatencyEngine,
    add_engine_node,
    make_fake_engine,
)
from yggdrasil.simulator import SimulatedTaskFailure, Simulator

_ids = itertools.count(1)


def task(**overrides):
    envelope = {
        "id": f"3f6f1b2c-9d4e-4c5a-8b7d-{next(_ids):012d}",
        "kind": "chat.generate",
        "payload": {"prompt": "hello"},
        "caller": "fake-engine-test",
        "criticality": "standard",
    }
    envelope.update(overrides)
    return envelope


# -- echo -----------------------------------------------------------------


def test_echo_answers_are_determined_by_the_payload_alone():
    first, second = EchoEngine(), EchoEngine()
    envelope = task()
    assert first.handle(envelope) == second.handle(envelope) == {
        "text": "SIMULATED echo: hello",
        "tokens": 3,
        "simulated": True,
        "engine_kind": "echo",
    }
    assert first.handle(task(payload={"b": 1, "a": 2}))["text"] == (
        'SIMULATED echo: {"a": 2, "b": 1}'
    )


def test_echo_embeddings_are_deterministic_fixture_vectors():
    engine = EchoEngine(capabilities=("embed",))
    envelope = task(kind="embed.corpus", payload={"texts": ["alpha", "beta", "alpha"]})
    result = engine.handle(envelope)
    assert result["dim"] == EMBEDDING_DIM
    assert len(result["embeddings"]) == 3
    assert all(len(vector) == EMBEDDING_DIM for vector in result["embeddings"])
    assert all(-1.0 <= value < 1.0 for vector in result["embeddings"] for value in vector)
    assert result["embeddings"][0] == result["embeddings"][2]  # same text, same vector
    assert result["embeddings"][0] != result["embeddings"][1]
    assert EchoEngine().handle(envelope) == result  # fresh engine, same vectors


def test_vision_echo_never_claims_to_have_seen_an_image():
    engine = EchoEngine(capabilities=("vision",))
    result = engine.handle(task(kind="vision.describe", payload={"prompt": "a cat"}))
    assert result["text"] == "SIMULATED vision: no image was seen; echo: a cat"


# -- latency ----------------------------------------------------------------


def test_latency_is_an_exact_pure_function_of_task_size():
    engine = LatencyEngine(base_ms=10.0, per_char_ms=0.5)
    assert engine.latency_ms_for(task(payload={"prompt": ""})) == 10.0
    assert engine.latency_ms_for(task(payload={"prompt": "abcd"})) == 12.0
    assert engine.latency_ms_for(task(payload={"prompt": "abcd"})) == 12.0  # again: pure
    batch = task(kind="embed.corpus", payload={"texts": ["ab", "cde"]})
    assert engine.latency_ms_for(batch) == 10.0 + 0.5 * len("ab\ncde")
    # Latency shaping never changes the answer's shape.
    assert engine.handle(task())["text"] == "SIMULATED echo: hello"


def test_simulator_honours_the_engines_per_task_latency():
    sim = Simulator()
    engine = LatencyEngine(base_ms=10.0, per_char_ms=1.0)
    add_engine_node(sim, "sim-a", engine, models=("m-1",))
    small, _ = sim.submit(task(payload={"prompt": "ab"}))
    large, _ = sim.submit(task(payload={"prompt": "abcdefgh"}))
    assert small["timings_ms"] == {"queue": 0.0, "run": 12.0, "total": 12.0}
    assert large["timings_ms"] == {"queue": 0.0, "run": 18.0, "total": 18.0}
    assert sim.clock.elapsed_ms == 30.0

    def replay():
        fresh = Simulator()
        add_engine_node(fresh, "sim-a", LatencyEngine(base_ms=10.0, per_char_ms=1.0))
        fixed = task(
            id="3f6f1b2c-9d4e-4c5a-8b7d-0000000000aa", payload={"prompt": "ab"}
        )
        return fresh.run_many([fixed])

    assert replay() == replay()  # shaped runs replay identically


# -- failure ------------------------------------------------------------------


def test_failure_always_fails_with_its_named_code_through_the_tunnel():
    sim = Simulator()
    engine = FailureEngine(mode="always", error_code="oom", detail="SIMULATED oom")
    add_engine_node(sim, "sim-a", engine)
    result, stream = sim.submit(task())
    assert result["status"] == "failed"
    assert result["error"] == {"code": "oom", "detail": "SIMULATED oom"}
    assert result["timings_ms"]["run"] == engine.base_ms  # failing still takes its time
    assert [event["event"] for event in stream] == ["accepted", "started", "failed"]
    with pytest.raises(SimulatedTaskFailure):
        engine.handle(task())


def test_failure_on_trigger_only_fires_on_the_trigger():
    engine = FailureEngine(mode="on_trigger", trigger="boom", error_code="engine_error")
    assert engine.handle(task(payload={"prompt": "calm"}))["simulated"] is True
    with pytest.raises(SimulatedTaskFailure) as caught:
        engine.handle(task(payload={"prompt": "say boom now"}))
    assert caught.value.error_code == "engine_error"


@pytest.mark.parametrize(
    "engine, expected",
    [
        (FailureEngine(mode="every_nth", n=3), [True, True, False, True, True, False]),
        (FailureEngine(mode="first_n", n=2), [False, False, True, True]),
    ],
)
def test_counted_failure_modes_replay_identically(engine, expected):
    def run(eng):
        outcomes = []
        for _ in expected:
            try:
                eng.handle(task())
                outcomes.append(True)
            except SimulatedTaskFailure:
                outcomes.append(False)
        return outcomes

    assert run(engine) == expected
    fresh = FailureEngine(mode=engine.mode, n=engine.n)
    assert run(fresh) == expected


def test_failure_configuration_is_validated_at_construction():
    with pytest.raises(FakeEngineError):
        FailureEngine(error_code="not_a_real_code")
    with pytest.raises(FakeEngineError):
        FailureEngine(mode="sometimes")
    with pytest.raises(FakeEngineError):
        FailureEngine(mode="on_trigger")  # no trigger
    with pytest.raises(FakeEngineError):
        FailureEngine(mode="every_nth", n=1)
    with pytest.raises(FakeEngineError):
        FailureEngine(mode="first_n", n=0)
    with pytest.raises(FakeEngineError):
        EchoEngine(base_ms=-1)
    with pytest.raises(FakeEngineError):
        EchoEngine(capabilities=("teleport",))
    with pytest.raises(FakeEngineError):
        EchoEngine(capabilities=())


# -- descriptors, factory, tunnel discipline -----------------------------------


def test_descriptors_are_validated_and_honestly_simulated():
    for kind in FAKE_ENGINE_KINDS:
        descriptor = make_fake_engine(kind).describe()
        assert descriptor["kind"] == kind
        assert descriptor["simulated"] is True
        assert descriptor["deterministic"] is True
        assert descriptor["latency"]["base_ms"] >= 0
    failure = FailureEngine(mode="every_nth", n=2, error_code="queue_full").describe()
    assert failure["failure"] == {
        "mode": "every_nth",
        "error_code": "queue_full",
        "trigger": None,
        "n": 2,
    }
    with pytest.raises(FakeEngineError):
        make_fake_engine("quantum")


def test_engine_nodes_still_obey_the_tunnels_capability_discipline():
    sim = Simulator()
    add_engine_node(sim, "sim-a", EchoEngine(), models=("m-1",))
    refused, _stream = sim.submit(
        task(requirements={"model": "other-model"}), to_node="sim-a"
    )
    assert refused["status"] == "refused"
    assert refused["error"]["code"] == "model_not_present"
    embed_engine = EchoEngine(capabilities=("embed",))
    add_engine_node(sim, "sim-b", embed_engine, models=("m-1",))
    result, _ = sim.submit(
        task(kind="embed.corpus", payload={"texts": ["x"]},
             requirements={"capabilities": ["embed"]}),
        to_node="sim-b",
    )
    assert result["status"] == "completed"
    assert result["result"]["embeddings"] == [embed_engine.handle(
        task(kind="embed.corpus", payload={"texts": ["x"]}))["embeddings"][0]]
