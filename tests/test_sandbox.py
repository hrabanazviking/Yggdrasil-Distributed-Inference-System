"""Task execution sandbox tests — Y-029's acceptance.

The sandbox must prove: (a) bounds are data — a policy is
validated, and a task may only tighten them (deadline_ms,
payload caps), never loosen them; (b) a task runs end to end
streamed — accepted/started/token/completed StreamEvents in
order, the text assembled in the terminal ResultEnvelope, a
measured SandboxReport, and a Y-013 event per execution;
(c) output bounds bite mid-stream — the crossing chunk is
truncated to the room left (per-chunk and whitespace token
counting both), the run completes partial, and the report
names the bound; (d) the time bound ends a run ``expired``
with deadline_unmeetable, the late chunk dropped, partial
text kept — measured on an injected timer, no real waiting;
(e) cancellation — an on_event callback (or another holder of
the sandbox) cancels mid-run, the runner's generator is
closed so its cleanup runs, and the result is ``cancelled``
with cancelled_by_caller and the partial text; cancelling a
finished or unknown task returns False; (f) failures keep
their names — SimulatedTaskFailure and error_code-carrying
exceptions fail under their Y-007 code, a bare exception is
engine_error, text streamed before the failure kept partial;
(g) the runner bridges — a dict-returning handler (fake
engine) and an adapter-style generate() pair stream both run
through the same bounds; (h) the node agent integration —
execute_task runs behind the lease (in-flight during the run,
released after, whatever the outcome), holds the model
lifecycle's use lease so the running model cannot be
unloaded mid-run, and refuses when no sandbox is wired or
the model is not resident; (i) the founding README is
untouched.
"""

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.fake_engines import EchoEngine
from yggdrasil.identity import (
    LocalKeyStore,
    NodeKeypair,
    create_mesh_root,
    issue_node_certificate,
)
from yggdrasil.model_lifecycle import ModelLifecycle, ModelLifecycleError
from yggdrasil.model_store import ModelStore
from yggdrasil.node_agent import NodeAgent, NodeAgentError, NodeConfig
from yggdrasil.observability import validate_event
from yggdrasil.sandbox import (
    SandboxError,
    TaskSandbox,
    adapter_generate_runner,
    default_policy,
    result_handler_runner,
    validate_policy,
)
from yggdrasil.simulator import SimulatedTaskFailure

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
NODE_ID = "test-node-1"
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"

POLICY = {
    "version": 1,
    "max_run_ms": 60_000,
    "max_output_tokens": 100,
    "max_output_chars": 10_000,
    "token_counting": "per_chunk",
}


class FakeTimer:
    """A monotonic clock the test advances by hand."""

    def __init__(self):
        self.seconds = 0.0

    def __call__(self):
        return self.seconds

    def advance(self, seconds: float):
        self.seconds += seconds


def make_task(**overrides) -> dict:
    task = {
        "id": str(uuid.uuid4()),
        "kind": "generate.text",
        "caller": "test",
        "payload": {"prompt": "hello"},
    }
    task.update(overrides)
    return task


def make_sandbox(timer=None, **policy_overrides) -> TaskSandbox:
    policy = dict(POLICY)
    policy.update(policy_overrides)
    return TaskSandbox(NODE_ID, policy, timer=timer or FakeTimer(), now=lambda: FIXED)


def chunks_runner(chunks):
    def runner(task, control):
        yield from chunks

    return runner


# -- policy ---------------------------------------------------------------


def test_default_policy_is_valid_and_named():
    policy = default_policy()
    assert policy["token_counting"] == "per_chunk"
    assert validate_policy(policy) == policy


@pytest.mark.parametrize(
    "policy",
    [
        {"version": 2, "max_run_ms": 1, "max_output_tokens": 1, "max_output_chars": 1},
        {"version": 1, "max_run_ms": 0, "max_output_tokens": 1, "max_output_chars": 1},
        {"version": 1, "max_run_ms": 1, "max_output_tokens": -1, "max_output_chars": 1},
        {"version": 1, "max_run_ms": 1, "max_output_tokens": 1, "max_output_chars": 1,
         "token_counting": "real_tokeniser"},  # no such counter is claimed
        {"version": 1, "max_run_ms": 1, "max_output_tokens": 1},  # missing a bound
    ],
)
def test_bad_policies_are_refused(policy):
    with pytest.raises(SandboxError):
        validate_policy(policy)


def test_a_task_may_only_tighten_bounds():
    sandbox = make_sandbox(max_output_tokens=10, max_run_ms=5000)
    task = make_task(deadline_ms=2000, payload={"prompt": "x", "max_tokens": 3})
    _result, _stream, report = sandbox.run(
        task, chunks_runner(["a"]), engine="fake", model="m"
    )
    assert report["bounds"] == {
        "max_run_ms": 2000,
        "max_output_tokens": 3,
        "max_output_chars": 10_000,
        "deadline_ms": 2000,
    }
    # Asking for more than the policy gives nothing.
    loose = make_task(deadline_ms=999_999, payload={"prompt": "x", "max_tokens": 999})
    _r, _s, report2 = sandbox.run(loose, chunks_runner(["a"]), engine="fake", model="m")
    assert report2["bounds"]["max_run_ms"] == 5000
    assert report2["bounds"]["max_output_tokens"] == 10


# -- a clean run ------------------------------------------------------------


def test_a_task_streams_to_completion():
    sandbox = make_sandbox()
    seen = []
    result, stream, report = sandbox.run(
        make_task(),
        chunks_runner(["Hello", ", ", "world"]),
        engine="fake",
        model="m",
        on_event=seen.append,
    )
    assert [e["event"] for e in stream] == [
        "accepted", "started", "token", "token", "token", "completed",
    ]
    assert seen == stream  # the caller holds each event as it happens
    assert result["status"] == "completed"
    assert result["partial"] is False
    assert result["result"]["text"] == "Hello, world"
    assert result["result"]["tokens_out"] == 3
    assert result["provenance"] == {"node_id": NODE_ID, "engine": "fake", "model": "m"}
    assert report["status"] == "completed"
    assert report["bound_hit"] is None
    assert report["error_code"] is None
    assert report["tokens_out"] == 3 and report["chars_out"] == len("Hello, world")
    events = sandbox.events
    assert [e["type"] for e in events] == ["sandbox.task_completed"]
    validate_event(events[0])
    assert sandbox.reports == [report]
    assert sandbox.active_task_ids == ()


# -- output bounds ------------------------------------------------------------


def test_the_token_bound_truncates_and_completes_partial():
    sandbox = make_sandbox(max_output_tokens=2)
    result, stream, report = sandbox.run(
        make_task(), chunks_runner(["a", "b", "c", "d"]), engine="fake", model="m"
    )
    assert result["status"] == "completed"
    assert result["partial"] is True
    assert result["result"]["text"] == "ab"
    assert report["bound_hit"] == "tokens"
    assert report["tokens_out"] == 2
    assert stream[-1]["event"] == "completed"


def test_the_char_bound_truncates_the_crossing_chunk():
    sandbox = make_sandbox(max_output_chars=5)
    result, _stream, report = sandbox.run(
        make_task(), chunks_runner(["hello world"]), engine="fake", model="m"
    )
    assert result["result"]["text"] == "hello"
    assert result["partial"] is True
    assert report["bound_hit"] == "output_chars"
    assert report["chars_out"] == 5


def test_whitespace_counting_truncates_by_words():
    sandbox = make_sandbox(token_counting="whitespace_words", max_output_tokens=3)
    result, _stream, report = sandbox.run(
        make_task(), chunks_runner(["one two", " three four"]), engine="fake", model="m"
    )
    assert result["result"]["text"] == "one twothree"
    assert report["tokens_out"] == 3
    assert report["bound_hit"] == "tokens"
    assert result["partial"] is True


# -- the time bound -------------------------------------------------------------


def test_the_time_bound_expires_a_run_and_drops_the_late_chunk():
    timer = FakeTimer()
    sandbox = make_sandbox(timer=timer, max_run_ms=1000)

    def slow(task, control):
        yield "early"
        timer.advance(2.0)  # two seconds pass before the next chunk
        yield "late"

    result, stream, report = sandbox.run(
        make_task(), slow, engine="fake", model="m"
    )
    assert result["status"] == "expired"
    assert result["error"]["code"] == "deadline_unmeetable"
    assert result["partial"] is True
    assert result["result"]["text"] == "early"  # the late chunk never streamed
    assert [e["event"] for e in stream] == ["accepted", "started", "token", "failed"]
    assert report["bound_hit"] == "time"
    assert report["run_ms"] == 2000.0


def test_a_task_deadline_is_the_time_bound():
    timer = FakeTimer()
    sandbox = make_sandbox(timer=timer)  # policy allows a minute

    def slow(task, control):
        yield "x"
        timer.advance(0.5)
        yield "y"

    result, _s, report = sandbox.run(
        make_task(deadline_ms=100), slow, engine="fake", model="m"
    )
    assert result["status"] == "expired"
    assert report["bounds"]["max_run_ms"] == 100


# -- cancellation ------------------------------------------------------------


def test_cancellation_keeps_partial_text_and_closes_the_runner():
    sandbox = make_sandbox()
    closed = []

    def endless(task, control):
        try:
            yield "one"
            yield "two"
            yield "three"
        finally:
            closed.append(True)

    def cancel_on_first_token(event):
        if event["event"] == "token":
            assert sandbox.cancel(event["task_id"]) is True

    result, _stream, report = sandbox.run(
        make_task(), endless, engine="fake", model="m", on_event=cancel_on_first_token
    )
    assert result["status"] == "cancelled"
    assert result["error"]["code"] == "cancelled_by_caller"
    assert result["partial"] is True
    assert result["result"]["text"] == "one"
    assert closed == [True]  # the runner's cleanup ran: the close is guaranteed
    assert report["bound_hit"] == "cancelled"
    assert sandbox.events[-1]["type"] == "sandbox.task_cancelled"


def test_cancelling_a_finished_or_unknown_task_returns_false():
    sandbox = make_sandbox()
    task = make_task()
    sandbox.run(task, chunks_runner(["a"]), engine="fake", model="m")
    assert sandbox.cancel(task["id"]) is False
    assert sandbox.cancel(str(uuid.uuid4())) is False


def test_a_runner_may_poll_its_control():
    sandbox = make_sandbox()

    def polite(task, control):
        yield "start"
        sandbox.cancel(task["id"])
        if control.cancelled:
            return
        yield "never"  # pragma: no cover - the poll above returns first

    result, _s, _r = sandbox.run(make_task(), polite, engine="fake", model="m")
    assert result["status"] == "cancelled"
    assert result["result"]["text"] == "start"


def test_a_duplicate_execution_is_refused():
    sandbox = make_sandbox()
    task = make_task()

    def reentrant(task, control):
        with pytest.raises(SandboxError, match="already executing"):
            sandbox.run(task, chunks_runner(["x"]), engine="fake", model="m")
        yield "outer"

    result, _s, _r = sandbox.run(task, reentrant, engine="fake", model="m")
    assert result["status"] == "completed"


# -- failures -----------------------------------------------------------------


def test_a_simulated_failure_keeps_its_code_and_partial_text():
    sandbox = make_sandbox()

    def failing(task, control):
        yield "part"
        raise SimulatedTaskFailure("engine_error", "the engine fell over")

    result, stream, report = sandbox.run(
        make_task(), failing, engine="fake", model="m"
    )
    assert result["status"] == "failed"
    assert result["error"] == {"code": "engine_error", "detail": "the engine fell over"}
    assert result["partial"] is True
    assert result["result"]["text"] == "part"
    assert stream[-1]["event"] == "failed"
    assert report["error_code"] == "engine_error"


def test_a_coded_exception_and_a_bare_exception():
    sandbox = make_sandbox()

    class CodedBoom(Exception):
        error_code = "oom"

    def coded(task, control):
        raise CodedBoom("out of memory")
        yield  # pragma: no cover

    result, _s, _r = sandbox.run(make_task(), coded, engine="fake", model="m")
    assert result["status"] == "failed"
    assert result["error"]["code"] == "oom"

    def bare(task, control):
        raise RuntimeError("mystery")
        yield  # pragma: no cover

    result2, _s2, _r2 = sandbox.run(make_task(), bare, engine="fake", model="m")
    assert result2["error"] == {"code": "engine_error", "detail": "mystery"}
    assert result2["partial"] is False
    assert "result" not in result2


def test_a_non_text_chunk_is_a_sandbox_error_not_a_result():
    sandbox = make_sandbox()

    def weird(task, control):
        yield 42

    with pytest.raises(SandboxError, match="text chunks"):
        sandbox.run(make_task(), weird, engine="fake", model="m")
    assert sandbox.active_task_ids == ()  # the execution cleaned up after itself


def test_a_malformed_task_is_refused_before_anything_runs():
    sandbox = make_sandbox()
    with pytest.raises(SandboxError):
        sandbox.run({"id": "not-a-uuid"}, chunks_runner(["a"]), engine="fake", model="m")


# -- runner bridges -------------------------------------------------------------


def test_the_result_handler_bridge_runs_a_fake_engine():
    sandbox = make_sandbox()
    engine = EchoEngine()
    task = make_task(payload={"prompt": "ping"})
    result, _stream, report = sandbox.run(
        task, result_handler_runner(engine), engine="echo", model="m"
    )
    assert result["status"] == "completed"
    assert result["result"]["text"] == "SIMULATED echo: ping"
    assert result["result"]["simulated"] is True  # the handler's fields ride along
    assert report["tokens_out"] == 1


def test_the_adapter_bridge_streams_token_events():
    sandbox = make_sandbox()

    class StubAdapter:
        def generate(self, task, node_id):
            yield {"task_id": task["id"], "seq": 0, "event": "started"}, None
            yield {"task_id": task["id"], "seq": 1, "event": "token",
                   "data": {"text": "ab"}}, None
            yield {"task_id": task["id"], "seq": 2, "event": "token",
                   "data": {"text": "cd"}}, None
            yield {"task_id": task["id"], "seq": 3, "event": "completed"}, None
            yield None, {"task_id": task["id"], "status": "completed"}

    result, _stream, report = sandbox.run(
        make_task(),
        adapter_generate_runner(StubAdapter(), NODE_ID),
        engine="stub",
        model="m",
    )
    assert result["result"]["text"] == "abcd"
    assert report["tokens_out"] == 2


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


def make_agent(tmp_path: Path, **kwargs) -> NodeAgent:
    store_dir = tmp_path / "keys"
    provision(store_dir)
    config = NodeConfig.from_dict(
        {"version": 1, "node_id": NODE_ID, "device_class": "vm",
         "key_store_dir": str(store_dir)}
    )
    return NodeAgent(config, now=lambda: FIXED, **kwargs)


def test_execute_task_runs_behind_the_lease(tmp_path):
    sandbox = make_sandbox()
    agent = make_agent(tmp_path, sandbox=sandbox)
    agent.start()
    agent.join()
    in_flight_during = []

    def observe(event):
        if event["event"] == "token":
            in_flight_during.append(agent.in_flight)

    result, _stream, _report = agent.execute_task(
        make_task(),
        chunks_runner(["hi"]),
        engine="fake",
        model="m",
        on_event=observe,
    )
    assert result["status"] == "completed"
    assert in_flight_during == [(result["task_id"],)]  # the lease was held mid-run
    assert agent.in_flight == ()  # and released after
    status = agent.status()
    assert status["sandbox"]["executions"] == 1
    assert status["sandbox"]["active"] == []


def make_store(tmp_path: Path) -> ModelStore:
    store = ModelStore(tmp_path / "models", now=lambda: FIXED)
    source = tmp_path / "m.gguf"
    source.write_bytes(b"SIMULATED weights")
    store.add(source, name="m", family="test", quant="Q4", format="gguf")
    return store


def test_execute_task_holds_the_model_use_lease(tmp_path):
    store = make_store(tmp_path)
    lifecycle = ModelLifecycle(NODE_ID, store, ram_bytes=10**9, now=lambda: FIXED)
    lifecycle.load("m")
    sandbox = make_sandbox()
    agent = make_agent(tmp_path, sandbox=sandbox, model_lifecycle=lifecycle)
    agent.start()
    agent.join()
    unload_verdicts = []

    def try_unload_mid_run(event):
        if event["event"] == "token":
            try:
                lifecycle.unload("m")
                unload_verdicts.append("unloaded")
            except ModelLifecycleError:
                unload_verdicts.append("refused-in-use")

    result, _s, _r = agent.execute_task(
        make_task(),
        chunks_runner(["working"]),
        engine="fake",
        model="m",
        on_event=try_unload_mid_run,
    )
    assert result["status"] == "completed"
    assert unload_verdicts == ["refused-in-use"]  # Y-027's protection, held by Y-029
    lifecycle.unload("m")  # released with the run: it unloads now


def test_execute_task_refusals_leave_no_lease(tmp_path):
    # No sandbox wired at all.
    bare = make_agent(tmp_path / "a")
    bare.start()
    bare.join()
    with pytest.raises(NodeAgentError, match="no task sandbox"):
        bare.execute_task(make_task(), chunks_runner(["x"]), engine="fake", model="m")

    # A model the lifecycle does not hold resident.
    store = make_store(tmp_path)
    lifecycle = ModelLifecycle(NODE_ID, store, ram_bytes=10**9, now=lambda: FIXED)
    agent = make_agent(tmp_path / "b", sandbox=make_sandbox(), model_lifecycle=lifecycle)
    agent.start()
    agent.join()
    with pytest.raises(NodeAgentError, match="not loaded"):
        agent.execute_task(make_task(), chunks_runner(["x"]), engine="fake", model="m")
    assert agent.in_flight == ()  # the lease was given back


def test_an_unwired_agent_status_reports_no_sandbox(tmp_path):
    agent = make_agent(tmp_path)
    assert agent.status()["sandbox"] is None
    assert agent.sandbox is None


# -- the founding document -------------------------------------------------------


def test_readme_unchanged():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
