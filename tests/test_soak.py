"""Node soak tests — Y-047's acceptance.

The soak must prove: (a) the roadmap's run — one joined
node, 1,000 tasks, three fake engines in rotation under
three engine names — completes with the arithmetic tally:
the failure engine is dealt 333 of the tasks and fails
every fifth of its own calls, so 66 failed and 934
completed, counted apart by engine, and the report is a
validated soak_report v1 saying simulated: true; (b) leak
in numbers — after the run no lease is in flight, no
sandbox execution is active, the queue holds nothing, and
the retained records are exactly one sandbox report and
one journal entry per task that ran, so the verdict is
stable; (c) drift in numbers — ten windows of 100, each a
validated soak_window v1, first and last windows compared
with the drift the policy tolerates, and a policy whose
tolerances are tightened past the measured drift names
the same workload drifted; (d) a second run on a fresh
harness over the same node replays the identical tally
(fresh fake engines — one run's failure tally never leaks
into the next) and the retained counts grow by exactly
another run; (e) door refusals are counted, not fatal —
a governor that refuses everything yields 1,000 refused,
zero retained records expected, and no leak verdict from
the counting alone; (f) misuse is refused before any task
runs — no agent, a node with no sandbox, a node not
joined, a lifecycle that has not loaded the soak models,
and an unshaped policy each raise SoakError by name and
deal no tasks; (g) every run records one validated Y-013
soak event with counts only, and require_stable is the
gate in one call; (h) the founding README is untouched.

No live server is spoken to and no model runs: the
engines are Y-015's fake engines, declared simulated.
"""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.governor import ResourceGovernor
from yggdrasil.identity import (
    LocalKeyStore,
    NodeKeypair,
    create_mesh_root,
    issue_node_certificate,
)
from yggdrasil.local_queue import LocalQueue
from yggdrasil.model_lifecycle import ModelLifecycle
from yggdrasil.node_agent import NodeAgent, NodeConfig
from yggdrasil.node_journal import NodeJournal
from yggdrasil.observability import validate_event
from yggdrasil.sandbox import TaskSandbox
from yggdrasil.soak import (
    SoakError,
    SoakHarness,
    default_policy,
    require_stable,
    run_soak,
    validate_policy,
    validate_report,
    validate_window,
)

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
NODE_ID = "soak-node-1"
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"


def provision(store_dir: Path) -> None:
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


def make_agent(tmp_path: Path, *, join: bool = True, **kwargs) -> NodeAgent:
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
    agent = NodeAgent(config, now=lambda: FIXED, **kwargs)
    if join:
        agent.start()
        agent.join()
    return agent


def soaked_agent(tmp_path: Path) -> tuple[NodeAgent, TaskSandbox, NodeJournal]:
    sandbox = TaskSandbox(NODE_ID, now=lambda: FIXED)
    journal = NodeJournal(NODE_ID, now=lambda: FIXED)
    queue = LocalQueue(NODE_ID, now=lambda: FIXED)
    agent = make_agent(
        tmp_path, sandbox=sandbox, node_journal=journal, local_queue=queue
    )
    return agent, sandbox, journal


# -- the roadmap's run --------------------------------------------------------


def test_the_default_policy_is_the_roadmaps_soak():
    policy = validate_policy(default_policy())
    assert policy["task_count"] == 1000
    assert policy["window_size"] == 100


def test_one_node_a_thousand_tasks_mixed_engines(tmp_path):
    agent, _sandbox, _journal = soaked_agent(tmp_path)
    report = validate_report(run_soak(agent, now=lambda: FIXED))
    assert report["node_id"] == NODE_ID
    assert report["simulated"] is True
    assert report["at"] == "2026-06-01T12:00:00Z"
    totals = report["totals"]
    assert totals["tasks"] == 1000
    # The rotation deals 334 / 333 / 333 tasks; the failure
    # engine fails every fifth of its own 333 calls = 66.
    assert report["by_engine"]["ollama"] == {
        "tasks": 334,
        "completed": 334,
        "failed": 0,
        "refused": 0,
    }
    assert report["by_engine"]["aesir"] == {
        "tasks": 333,
        "completed": 333,
        "failed": 0,
        "refused": 0,
    }
    assert report["by_engine"]["llamacpp"] == {
        "tasks": 333,
        "completed": 267,
        "failed": 66,
        "refused": 0,
    }
    assert totals["completed"] == 934
    assert totals["failed"] == 66
    assert totals["refused"] == 0
    assert totals["cancelled"] == 0
    assert totals["expired"] == 0
    assert report["verdict"] == "stable"


def test_no_leak_after_a_thousand_tasks(tmp_path):
    agent, sandbox, journal = soaked_agent(tmp_path)
    report = run_soak(agent, now=lambda: FIXED)
    residuals = report["residuals"]
    assert residuals["in_flight"] == 0
    assert residuals["sandbox_active"] == 0
    assert residuals["queue_depth"] == 0
    assert residuals["sandbox_reports"] == 1000
    assert residuals["journal_entries"] == 1000
    assert agent.in_flight == ()
    assert sandbox.active_task_ids == ()
    assert len(journal) == 1000
    memory = report["memory"]
    assert memory["peak_rss_after_kb"] >= memory["peak_rss_before_kb"]
    assert (
        memory["peak_rss_growth_kb"]
        == memory["peak_rss_after_kb"] - memory["peak_rss_before_kb"]
    )


def test_windows_and_drift_in_numbers(tmp_path):
    agent, _sandbox, _journal = soaked_agent(tmp_path)
    report = run_soak(agent, now=lambda: FIXED)
    windows = report["windows"]
    assert len(windows) == 10
    for index, window in enumerate(windows):
        validate_window(window)
        assert window["index"] == index
        assert window["tasks"] == 100
        assert (
            window["completed"] + window["failed"] + window["refused"]
            + window["cancelled"] + window["expired"]
            == 100
        )
    assert sum(w["completed"] for w in windows) == report["totals"]["completed"]
    assert sum(w["failed"] for w in windows) == report["totals"]["failed"]
    drift = report["drift"]
    assert drift["completion_rate_drift"] <= 0.1
    assert drift["mean_chars_out_drift_fraction"] <= 0.25
    # The run-ms drift is reported, never hidden: a fraction
    # or an honest null over a zero first window.
    assert drift["mean_run_ms_drift_fraction"] is None or (
        drift["mean_run_ms_drift_fraction"] >= 0
    )


def test_a_tightened_policy_names_the_same_run_drifted(tmp_path):
    agent, _sandbox, _journal = soaked_agent(tmp_path)
    strict = default_policy()
    strict["max_completion_rate_drift"] = 0.0
    strict["max_output_drift_fraction"] = 0.0
    report = run_soak(agent, policy=strict, now=lambda: FIXED)
    # The workload's windows are not perfectly uniform (the
    # failure engine's tally lands unevenly across windows),
    # so a zero-tolerance policy must name it drifted.
    assert report["verdict"] == "drifted"
    with pytest.raises(SoakError, match="drifted"):
        require_stable(agent, policy=strict, now=lambda: FIXED)


def test_a_second_run_replays_identically(tmp_path):
    agent, _sandbox, journal = soaked_agent(tmp_path)
    first = run_soak(agent, now=lambda: FIXED)
    second = run_soak(agent, now=lambda: FIXED)
    assert second["totals"] == first["totals"]
    assert second["by_engine"] == first["by_engine"]
    assert second["residuals"]["sandbox_reports"] == 1000
    assert second["residuals"]["journal_entries"] == 1000
    assert len(journal) == 2000
    assert second["verdict"] == "stable"


def test_require_stable_is_the_gate_in_one_call(tmp_path):
    agent, _sandbox, _journal = soaked_agent(tmp_path)
    report = require_stable(agent, now=lambda: FIXED)
    assert report["verdict"] == "stable"


def test_every_run_records_one_event_with_counts_only(tmp_path):
    agent, _sandbox, _journal = soaked_agent(tmp_path)
    harness = SoakHarness(agent, now=lambda: FIXED)
    harness.run()
    (event,) = harness.events
    validate_event(event)
    assert event["type"] == "soak.stable"
    assert event["payload"]["tasks"] == 1000
    assert event["payload"]["completed"] == 934
    assert event["payload"]["simulated"] is True
    assert "prompt" not in str(event["payload"])


# -- door refusals, counted -----------------------------------------------------


def test_door_refusals_are_counted_not_fatal(tmp_path):
    sandbox = TaskSandbox(NODE_ID, now=lambda: FIXED)
    journal = NodeJournal(NODE_ID, now=lambda: FIXED)
    hot = {
        "thermal_c": 95.0,
        "memory_used_bytes": 100,
        "memory_total_bytes": 1000,
        "cpu_share_used": 0.1,
    }
    governor = ResourceGovernor(NODE_ID, readings=lambda: hot, now=lambda: FIXED)
    agent = make_agent(
        tmp_path, sandbox=sandbox, node_journal=journal, governor=governor
    )
    policy = default_policy()
    policy["task_count"] = 30
    policy["window_size"] = 10
    report = run_soak(agent, policy=policy, now=lambda: FIXED)
    assert report["totals"]["tasks"] == 30
    assert report["totals"]["refused"] == 30
    assert report["totals"]["completed"] == 0
    # Nothing reached the sandbox, so nothing is retained —
    # and the counting knows it: refused runs are not leaks.
    assert report["residuals"]["sandbox_reports"] == 0
    assert report["residuals"]["journal_entries"] == 0
    assert report["verdict"] != "leaked"
    assert len(journal) == 0


# -- misuse, refused before any task runs ---------------------------------------


def test_misuse_is_refused_by_name(tmp_path):
    with pytest.raises(SoakError, match="needs a node agent"):
        SoakHarness(None)
    with pytest.raises(SoakError, match="soak policy"):
        SoakHarness(make_agent(tmp_path), policy={"version": 1, "task_count": 0})

    bare = make_agent(tmp_path / "bare")
    with pytest.raises(SoakError, match="no task sandbox"):
        run_soak(bare, now=lambda: FIXED)

    sandbox = TaskSandbox(NODE_ID, now=lambda: FIXED)
    unjoined = make_agent(tmp_path / "unjoined", join=False, sandbox=sandbox)
    unjoined.start()
    with pytest.raises(SoakError, match="must be joined"):
        run_soak(unjoined, now=lambda: FIXED)
    assert sandbox.reports == []


def test_a_lifecycle_without_the_soak_models_refuses_up_front(tmp_path):
    sandbox = TaskSandbox(NODE_ID, now=lambda: FIXED)
    lifecycle = ModelLifecycle(
        NODE_ID,
        None,
        vram_bytes=1024**3,
        ram_bytes=1024**3,
        now=lambda: FIXED,
    )
    agent = make_agent(
        tmp_path, sandbox=sandbox, model_lifecycle=lifecycle
    )
    with pytest.raises(SoakError, match="has not loaded the soak models"):
        run_soak(agent, now=lambda: FIXED)
    assert sandbox.reports == []


def test_a_smaller_soak_still_counts_exactly(tmp_path):
    agent, _sandbox, _journal = soaked_agent(tmp_path)
    policy = default_policy()
    policy["task_count"] = 50
    policy["window_size"] = 20
    report = run_soak(agent, policy=policy, now=lambda: FIXED)
    assert report["totals"]["tasks"] == 50
    assert [w["tasks"] for w in report["windows"]] == [20, 20, 10]
    assert report["residuals"]["sandbox_reports"] == 50
    assert report["verdict"] == "stable"


# -- the founding document -------------------------------------------------------


def test_readme_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
