"""Failure shapes tests — Y-032's acceptance.

The four shapes must prove: (a) distinctness — engine crash,
OOM, thermal throttle, and model corrupt are sorted apart,
each with its own Y-007 name, containment, and severity, and
codes outside the four (queue_full, deadline, cancellation)
are outcomes, not incidents; (b) detection — from a finished
result / sandbox report / raised exception, and by polling
the wired store (re-hash), governor (verdict), and readings;
(c) containment — a crashed engine is quarantined alone, a
corrupt model is quarantined and unloaded when resident, an
OOM and a throttle latch until a measured clearing; (d) no
spam — a known corruption / an ongoing throttle records one
incident, not one per check; (e) restoration — a model whose
pinned bytes return is un-quarantined with an event, an
engine's quarantine lifts only when cleared by name;
(f) the report — validated failure_report v1, totals by
shape, active quarantines/latches, uncontained 0, events
validated Y-013 with names/counts only; (g) the node agent —
execute_task feeds the monitor, check_failures polls through
it, status carries the report, an unwired agent reports None
exactly as before; (h) the founding README is untouched.
"""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.failure_shapes import (
    FailureError,
    FailureMonitor,
    classify_error_code,
    validate_incident,
    validate_report,
)
from yggdrasil.governor import ResourceGovernor
from yggdrasil.identity import (
    LocalKeyStore,
    NodeKeypair,
    create_mesh_root,
    issue_node_certificate,
)
from yggdrasil.model_lifecycle import ModelLifecycle
from yggdrasil.model_store import ModelStore
from yggdrasil.node_agent import NodeAgent, NodeAgentError, NodeConfig
from yggdrasil.observability import validate_event
from yggdrasil.sandbox import TaskSandbox
from yggdrasil.simulator import SimulatedTaskFailure

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
NODE_ID = "test-node-1"
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
TASK_ID = "11111111-1111-4111-8111-111111111111"

POLICY = {
    "version": 1,
    "thermal_shed_c": 80.0,
    "thermal_resume_c": 72.0,
    "thermal_hard_c": 90.0,
    "max_memory_fraction": 0.9,
    "max_cpu_share": 0.9,
}
COOL = {"thermal_c": 40.0, "memory_used_bytes": 100, "memory_total_bytes": 1000,
        "cpu_share_used": 0.1}
HOT = {"thermal_c": 85.0, "memory_used_bytes": 100, "memory_total_bytes": 1000,
       "cpu_share_used": 0.1}
HARD = {"thermal_c": 95.0, "memory_used_bytes": 100, "memory_total_bytes": 1000,
        "cpu_share_used": 0.1}


def result(code, *, engine="ollama", model="m.gguf", status="failed"):
    return {
        "task_id": TASK_ID,
        "status": status,
        "provenance": {"node_id": NODE_ID, "engine": engine, "model": model},
        "timings_ms": {"queue": 0.0, "run": 1.0, "total": 1.0},
        "partial": False,
        "error": {"code": code, "detail": f"it failed with {code}"},
    }


# -- classification -----------------------------------------------------------------


def test_classification_maps_only_the_four_shapes():
    assert classify_error_code("oom") == "oom"
    assert classify_error_code("model_hash_mismatch") == "model_corrupt"
    assert classify_error_code("thermal_shed") == "thermal_throttle"
    assert classify_error_code("engine_error") == "engine_crash"
    assert classify_error_code("engine_unreachable") == "engine_crash"
    for outcome in ("queue_full", "deadline_unmeetable", "cancelled_by_caller",
                    "model_not_present", "no_capable_node", None):
        assert classify_error_code(outcome) is None


def test_outcomes_and_successes_are_not_incidents():
    monitor = FailureMonitor(NODE_ID, now=lambda: FIXED)
    assert monitor.ingest_result(result("queue_full")) is None
    assert monitor.ingest_result(result("deadline_unmeetable")) is None
    done = result("engine_error", status="completed")
    del done["error"]
    assert monitor.ingest_result(done) is None
    assert monitor.report()["total_incidents"] == 0


# -- engine crash ---------------------------------------------------------------------


def test_an_engine_crash_quarantines_only_that_engine():
    monitor = FailureMonitor(NODE_ID, now=lambda: FIXED)
    incident = monitor.ingest_result(result("engine_error"))
    validate_incident(incident)
    assert incident["shape"] == "engine_crash"
    assert incident["error_code"] == "engine_error"
    assert incident["containment"] == "engine_quarantined"
    assert incident["severity"] == "critical"
    assert incident["subject"] == "ollama"
    assert incident["task_id"] == TASK_ID
    assert monitor.engine_available("ollama") is False
    assert monitor.engine_available("aesir") is True  # the neighbour is untouched
    assert monitor.report()["active"]["quarantined_engines"] == ["ollama"]
    # The quarantine lifts only when cleared by name.
    assert monitor.clear_engine("aesir") is False
    assert monitor.clear_engine("ollama") is True
    assert monitor.engine_available("ollama") is True
    assert monitor.events[-1]["type"] == "failure.engine_cleared"


def test_an_unreachable_engine_is_a_crash_keeping_its_own_code():
    monitor = FailureMonitor(NODE_ID, now=lambda: FIXED)
    incident = monitor.ingest_result(result("engine_unreachable", engine="aesir"))
    assert incident["shape"] == "engine_crash"
    assert incident["error_code"] == "engine_unreachable"


def test_exception_doors_sort_by_code_type_and_scope():
    monitor = FailureMonitor(NODE_ID, now=lambda: FIXED)
    oom = monitor.ingest_exception(MemoryError("blew up"), task_id=TASK_ID)
    assert oom["shape"] == "oom" and oom["error_code"] == "oom"
    assert monitor.oom_active is True
    coded = monitor.ingest_exception(
        SimulatedTaskFailure("thermal_shed", "too hot"), task_id=TASK_ID
    )
    assert coded["shape"] == "thermal_throttle"
    scoped = monitor.ingest_exception(RuntimeError("segfault"), engine="llamacpp")
    assert scoped["shape"] == "engine_crash" and scoped["subject"] == "llamacpp"
    assert monitor.ingest_exception(RuntimeError("a caller bug")) is None
    assert monitor.report()["total_incidents"] == 3


def test_a_sandbox_report_ingests_by_its_recorded_code():
    monitor = FailureMonitor(NODE_ID, now=lambda: FIXED)
    report = {"task_id": TASK_ID, "status": "failed", "error_code": "oom"}
    incident = monitor.ingest_sandbox_report(report, engine="ollama", model="m.gguf")
    assert incident["shape"] == "oom"
    assert incident["source"] == "sandbox"
    assert incident["containment"] == "task_terminated_and_load_shed"
    quiet = {"task_id": TASK_ID, "status": "expired", "error_code": "deadline_unmeetable"}
    assert monitor.ingest_sandbox_report(quiet) is None


# -- OOM from readings ------------------------------------------------------------------


def test_oom_from_readings_latches_and_clears_on_headroom():
    monitor = FailureMonitor(NODE_ID, now=lambda: FIXED)
    full = {"memory_used_bytes": 1000, "memory_total_bytes": 1000, "memory_fraction": 1.0}
    incident = monitor.check_memory(full)
    assert incident["shape"] == "oom" and incident["source"] == "readings"
    assert incident["evidence"]["memory_fraction"] == 1.0
    assert monitor.check_memory(full) is None  # latched: one incident, not one per check
    unknown = {"memory_used_bytes": None, "memory_total_bytes": None, "memory_fraction": None}
    assert monitor.check_memory(unknown) is None
    assert monitor.oom_active is True  # an unknown reading clears nothing
    roomy = {"memory_used_bytes": 100, "memory_total_bytes": 1000, "memory_fraction": 0.1}
    assert monitor.check_memory(roomy) is None
    assert monitor.oom_active is False
    assert monitor.events[-1]["type"] == "failure.oom_cleared"


# -- thermal throttle ---------------------------------------------------------------------


def governor_with(readings):
    box = {"readings": dict(readings)}
    gov = ResourceGovernor(NODE_ID, POLICY, readings=lambda: dict(box["readings"]),
                           now=lambda: FIXED)
    return gov, box


def test_thermal_throttle_from_the_governor_latches_and_clears():
    gov, box = governor_with(HOT)
    monitor = FailureMonitor(NODE_ID, governor=gov, now=lambda: FIXED)
    incident = monitor.check_thermal()
    assert incident["shape"] == "thermal_throttle"
    assert incident["severity"] == "warning"  # shed line, not the hard line
    assert incident["evidence"]["thermal_c"] == 85.0
    assert incident["containment"] == "load_shed"
    assert monitor.check_thermal() is None  # still hot, already recorded
    box["readings"] = dict(COOL)
    assert monitor.check_thermal() is None
    assert monitor.thermal_throttling is False
    assert monitor.events[-1]["type"] == "failure.thermal_cleared"


def test_thermal_at_the_hard_line_is_critical():
    gov, _box = governor_with(HARD)
    monitor = FailureMonitor(NODE_ID, governor=gov, now=lambda: FIXED)
    assert monitor.check_thermal()["severity"] == "critical"


# -- model corrupt -------------------------------------------------------------------------


def make_store(tmp_path: Path, body: bytes = b"SIMULATED weights v1") -> ModelStore:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source.gguf"
    source.write_bytes(body)
    store = ModelStore(tmp_path / "store", now=lambda: FIXED)
    store.add(source, name="m.gguf", family="fam", quant="Q4", format="gguf")
    return store


def test_a_corrupt_model_is_quarantined_once_and_restored_honestly(tmp_path):
    store = make_store(tmp_path)
    entry = store.entry("m.gguf")
    path = store.root / entry["path"]
    original = path.read_bytes()
    monitor = FailureMonitor(NODE_ID, model_store=store, now=lambda: FIXED)
    assert monitor.check_models() == []
    path.write_bytes(b"rotted bytes, not the pinned weights")
    fresh = monitor.check_models()
    assert len(fresh) == 1
    incident = fresh[0]
    validate_incident(incident)
    assert incident["shape"] == "model_corrupt"
    assert incident["error_code"] == "model_hash_mismatch"
    assert incident["evidence"]["model_status"] == "corrupt"
    assert incident["evidence"]["expected_sha256"] == entry["sha256"]
    assert incident["evidence"]["actual_sha256"] != entry["sha256"]
    assert monitor.model_available("m.gguf") is False
    assert monitor.check_models() == []  # the same rot, already contained
    path.write_bytes(original)  # the operator restores the pinned bytes
    assert monitor.check_models() == []
    assert monitor.model_available("m.gguf") is True
    assert monitor.events[-1]["type"] == "failure.model_restored"


def test_a_missing_model_is_corrupt_shape_with_its_true_status(tmp_path):
    store = make_store(tmp_path)
    entry = store.entry("m.gguf")
    (store.root / entry["path"]).unlink()
    monitor = FailureMonitor(NODE_ID, model_store=store, now=lambda: FIXED)
    incident = monitor.check_models()[0]
    assert incident["shape"] == "model_corrupt"
    assert incident["evidence"]["model_status"] == "missing"
    assert incident["evidence"]["actual_sha256"] is None


def test_a_resident_corrupt_model_is_unloaded_by_containment(tmp_path):
    store = make_store(tmp_path)
    lifecycle = ModelLifecycle(
        NODE_ID, store, vram_bytes=0, ram_bytes=10**9, max_slots=2, now=lambda: FIXED
    )
    lifecycle.load("m.gguf", placement="system")
    assert lifecycle.is_loaded("m.gguf")
    entry = store.entry("m.gguf")
    (store.root / entry["path"]).write_bytes(b"tampered after load")
    monitor = FailureMonitor(
        NODE_ID, model_store=store, model_lifecycle=lifecycle, now=lambda: FIXED
    )
    incident = monitor.check_models()[0]
    assert incident["containment"] == "model_quarantined_and_unloaded"
    assert incident["evidence"]["resident"] is True
    assert incident["evidence"]["unloaded"] is True
    assert lifecycle.is_loaded("m.gguf") is False


# -- the report, whole ------------------------------------------------------------------------


def test_the_report_counts_each_shape_distinctly_and_events_are_clean():
    gov, _box = governor_with(HOT)
    monitor = FailureMonitor(NODE_ID, governor=gov, now=lambda: FIXED)
    monitor.ingest_result(result("engine_error"))
    monitor.ingest_exception(MemoryError("oom"), task_id=TASK_ID)
    monitor.check_thermal()
    monitor._record(  # a corrupt model named directly, as check_models records it
        "model_corrupt", error_code="model_hash_mismatch", source="model_store",
        subject="m.gguf", task_id=None, severity="critical",
        containment="model_quarantined", detail="bytes do not match the pin",
        evidence={k: None for k in (
            "thermal_c", "memory_fraction", "memory_used_bytes", "memory_total_bytes",
            "expected_sha256", "actual_sha256", "model_status", "resident", "unloaded")},
    )
    report = monitor.report()
    validate_report(report)
    assert report["total_incidents"] == 4
    assert report["by_shape"] == {
        "engine_crash": 1, "oom": 1, "thermal_throttle": 1, "model_corrupt": 1,
    }
    assert report["uncontained"] == 0
    assert report["active"]["quarantined_engines"] == ["ollama"]
    assert report["active"]["thermal_throttling"] is True
    assert report["active"]["oom_active"] is True
    assert [i["seq"] for i in report["incidents"]] == [0, 1, 2, 3]
    for event in monitor.events:
        validate_event(event)
        assert event["source"] == {"node_id": NODE_ID, "component": "node_agent"}
    assert "secret" not in repr(monitor.events)


def test_check_all_reports_every_shape_it_finds(tmp_path):
    store = make_store(tmp_path)
    entry = store.entry("m.gguf")
    (store.root / entry["path"]).write_bytes(b"rot")
    gov, _box = governor_with(HARD)
    monitor = FailureMonitor(NODE_ID, model_store=store, governor=gov, now=lambda: FIXED)
    report = monitor.check_all(
        {"memory_used_bytes": 999, "memory_total_bytes": 1000, "memory_fraction": 0.999}
    )
    assert report["by_shape"]["model_corrupt"] == 1
    assert report["by_shape"]["thermal_throttle"] == 1
    assert report["by_shape"]["oom"] == 1


def test_an_unwired_monitor_claims_no_polled_shape():
    monitor = FailureMonitor(NODE_ID, now=lambda: FIXED)
    assert monitor.check_models() == []
    assert monitor.check_thermal() is None
    assert monitor.check_memory() is None
    with pytest.raises(FailureError, match="node_id"):
        FailureMonitor("")


# -- node agent integration ---------------------------------------------------------------------


def provision(store_dir: Path) -> LocalKeyStore:
    store = LocalKeyStore(store_dir)
    root_keypair, root_cert = create_mesh_root(now=FIXED)
    node_keypair = NodeKeypair.generate()
    cert = issue_node_certificate(
        root_keypair, root_cert, node_id=NODE_ID, node_keypair=node_keypair,
        device_class="vm", agent_version="0.0.1", now=FIXED,
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


def task():
    return {
        "id": TASK_ID, "kind": "generate.text", "caller": "test",
        "payload": {"prompt": "a secret prompt that must never leak"},
    }


def test_agent_execute_task_feeds_the_monitor_and_quarantines_the_engine(tmp_path):
    monitor = FailureMonitor(NODE_ID, now=lambda: FIXED)
    sandbox = TaskSandbox(NODE_ID, now=lambda: FIXED)
    agent = make_agent(tmp_path, sandbox=sandbox, failure_monitor=monitor)
    agent.start()
    agent.join()

    def crashing_runner(t, control):
        raise SimulatedTaskFailure("engine_error", "the engine fell over")
        yield  # pragma: no cover — makes this a generator like a real runner

    result_env, _stream, _report = agent.execute_task(
        task(), crashing_runner, engine="ollama", model="m.gguf"
    )
    assert result_env["status"] == "failed"  # the task's own verdict is unchanged
    report = agent.failure_report()
    assert report["by_shape"]["engine_crash"] == 1
    assert report["active"]["quarantined_engines"] == ["ollama"]
    assert agent.status()["failures"]["total_incidents"] == 1
    assert "secret prompt" not in repr(report)


def test_agent_check_failures_polls_through_the_monitor(tmp_path):
    store = make_store(tmp_path / "models")
    entry = store.entry("m.gguf")
    (store.root / entry["path"]).write_bytes(b"rot")
    monitor = FailureMonitor(NODE_ID, model_store=store, now=lambda: FIXED)
    agent = make_agent(tmp_path, failure_monitor=monitor)
    report = agent.check_failures()
    assert report["by_shape"]["model_corrupt"] == 1


def test_an_unwired_agent_reports_no_failures_and_refuses_the_check(tmp_path):
    agent = make_agent(tmp_path)
    agent.start()
    agent.join()
    assert agent.failure_report() is None
    assert agent.status()["failures"] is None
    with pytest.raises(NodeAgentError, match="no failure monitor"):
        agent.check_failures()


# -- the founding document ------------------------------------------------------------------------


def test_readme_unchanged():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
