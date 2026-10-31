"""Node journal tests — Y-039's acceptance.

The journal must prove: (a) one validated journal_entry v1 per
task the node ran — engine, model, quant, and verification
from the result's own provenance, the measured timings, the
sandbox's tokens/chars and bound hit, the task's kind, caller,
criticality, and context references as names; (b) agreement —
a result naming another task, a provenance naming another
node, a sandbox report for another task/node, a status or
error disagreement are each refused by name, and nothing is
written for a refused recording; (c) no content — the task's
payload and the result's text never enter an entry, a report,
or the durable file; (d) failed, cancelled, and partial runs
are journaled with their Y-007 names, exactly as completed
ones are; (e) entries recorded without a sandbox report carry
null measurements and are counted as unmeasured in the
report, never dressed with invented numbers; (f) durability —
a journal with a path appends one JSON line per entry, a
fresh journal on the same file loads the book and continues
its sequence, and a corrupt, foreign, or renumbered line is
refused by line number; (g) the numbers — a validated
journal_report v1 with totals by status/engine/model,
partial count, summed and mean run times, summed tokens, and
the durability truth (memory says not durable); (h) lookups —
by task (a retried task keeps both entries), model, and
engine, in journal order; (i) events — every recording a
validated Y-013 journal.task_recorded envelope with names and
counts only; (j) the node agent — execute_task journals every
finished run (through the queue too), a governor-refused run
is not journaled, status carries the report, an unwired agent
keeps no book; (k) the citizen factories wire a journal
(the Pi citizen end to end, durable when a path is named);
(l) the founding README is untouched.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from yggdrasil.census import ProbeSet
from yggdrasil.governor import ResourceGovernor
from yggdrasil.identity import (
    LocalKeyStore,
    NodeKeypair,
    create_mesh_root,
    issue_node_certificate,
)
from yggdrasil.local_queue import LocalQueue
from yggdrasil.node_agent import NodeAgent, NodeAgentError, NodeConfig
from yggdrasil.node_journal import (
    JournalError,
    NodeJournal,
    validate_entry,
    validate_report,
)
from yggdrasil.observability import validate_event
from yggdrasil.pi_citizen import build_pi_citizen_agent
from yggdrasil.sandbox import TaskSandbox
from yggdrasil.simulator import SimulatedTaskFailure

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
NODE_ID = "test-node-1"
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"

TASK_A = "11111111-1111-4111-8111-111111111111"
TASK_B = "22222222-2222-4222-8222-222222222222"
TASK_C = "33333333-3333-4333-8333-333333333333"


class Clock:
    def __init__(self):
        self.moment = FIXED

    def __call__(self):
        return self.moment

    def advance(self, **kwargs):
        self.moment += timedelta(**kwargs)


def task(task_id, *, kind="chat.generate", caller="test-caller", **extra):
    record = {
        "id": task_id,
        "kind": kind,
        "caller": caller,
        "payload": {"prompt": "a secret prompt that must never leak"},
    }
    record.update(extra)
    return record


def run_through_sandbox(journal_task, *, engine="ollama", model="m1", chunks=("answer",)):
    """Run one task in a fresh sandbox and return its triple."""
    sandbox = TaskSandbox(NODE_ID, now=lambda: FIXED)
    return sandbox.run(journal_task, lambda t, c: iter(list(chunks)), engine=engine, model=model)


# -- one entry per run -------------------------------------------------------


def test_a_completed_run_is_journaled_from_its_own_records():
    journal = NodeJournal(NODE_ID, now=lambda: FIXED)
    t = task(
        TASK_A,
        criticality="critical",
        context_refs=[{"collection": "lore", "id": "saga-1", "version": 3}],
    )
    result, _stream, report = run_through_sandbox(t)
    entry = journal.record_run(t, result, report)
    validate_entry(entry)
    assert entry["seq"] == 0
    assert entry["task_id"] == TASK_A
    assert entry["node_id"] == NODE_ID
    assert entry["kind"] == "chat.generate"
    assert entry["caller"] == "test-caller"
    assert entry["criticality"] == "critical"
    assert entry["engine"] == "ollama"
    assert entry["model"] == "m1"
    assert entry["quant"] is None  # the sandbox's provenance names no quant
    assert entry["status"] == "completed"
    assert entry["error_code"] is None
    assert entry["partial"] is False
    assert entry["bound_hit"] is None
    assert entry["timings_ms"] == result["timings_ms"]
    assert entry["tokens_out"] == report["tokens_out"]
    assert entry["chars_out"] == report["chars_out"]
    assert entry["context_refs"] == ["lore/saga-1@v3"]
    assert entry["recorded_at"] == "2026-06-01T12:00:00Z"
    assert len(journal) == 1


def test_quant_and_verification_ride_from_the_results_provenance():
    journal = NodeJournal(NODE_ID, now=lambda: FIXED)
    t = task(TASK_A)
    result = {
        "task_id": TASK_A,
        "status": "completed",
        "provenance": {
            "node_id": NODE_ID,
            "engine": "aesir",
            "model": "m2",
            "quant": "Q4_K_M",
            "verification": "invariant",
        },
        "timings_ms": {"queue": 1.0, "run": 42.0, "total": 43.0},
        "partial": False,
        "result": {"text": "a secret answer that must never leak"},
    }
    entry = journal.record_run(t, result)
    assert entry["quant"] == "Q4_K_M"
    assert entry["verification"] == "invariant"
    assert entry["tokens_out"] is None  # no sandbox report: unmeasured, not invented
    assert entry["chars_out"] is None
    assert journal.report()["unmeasured_entries"] == 1


def test_failed_and_cancelled_runs_are_journaled_with_their_names():
    clock = Clock()
    journal = NodeJournal(NODE_ID, now=clock)
    sandbox = TaskSandbox(NODE_ID, now=lambda: FIXED)

    def boom(t, c):
        raise SimulatedTaskFailure("engine_error", "it broke")
        yield  # pragma: no cover — makes this a generator

    failed_task = task(TASK_A)
    result, _s, report = sandbox.run(failed_task, boom, engine="fake", model="m1")
    failed = journal.record_run(failed_task, result, report)
    assert failed["status"] == "failed"
    assert failed["error_code"] == "engine_error"

    cancelling = TaskSandbox(NODE_ID, now=lambda: FIXED)

    def two_chunks(t, c):
        yield "first "
        yield "second"

    cancel_task = task(TASK_B)
    seen = []

    def on_event(event):
        seen.append(event)
        if event["event"] == "token":
            cancelling.cancel(TASK_B)

    result2, _s2, report2 = cancelling.run(
        cancel_task, two_chunks, engine="fake", model="m1", on_event=on_event
    )
    cancelled = journal.record_run(cancel_task, result2, report2)
    assert cancelled["status"] == "cancelled"
    assert cancelled["error_code"] == "cancelled_by_caller"
    assert cancelled["partial"] is True
    assert cancelled["bound_hit"] == "cancelled"
    assert [e["seq"] for e in journal.entries()] == [0, 1]


# -- agreement -----------------------------------------------------------------


def test_records_that_disagree_are_refused_by_name_and_nothing_is_written():
    journal = NodeJournal(NODE_ID, now=lambda: FIXED)
    t = task(TASK_A)
    result, _s, report = run_through_sandbox(t)

    wrong_task = dict(result, task_id=TASK_B)
    with pytest.raises(JournalError, match="result names task"):
        journal.record_run(t, wrong_task, report)

    foreign = json.loads(json.dumps(result))
    foreign["provenance"]["node_id"] = "another-node"
    with pytest.raises(JournalError, match="provenance names node"):
        journal.record_run(t, foreign, report)

    with pytest.raises(JournalError, match="sandbox report names task"):
        journal.record_run(t, result, dict(report, task_id=TASK_B))
    with pytest.raises(JournalError, match="sandbox report names node"):
        journal.record_run(t, result, dict(report, node_id="another-node"))
    with pytest.raises(JournalError, match="disagrees with result status"):
        journal.record_run(t, result, dict(report, status="failed", error_code="engine_error"))
    assert len(journal) == 0
    assert journal.events == []


def test_a_malformed_record_is_refused_by_its_schema():
    journal = NodeJournal(NODE_ID, now=lambda: FIXED)
    with pytest.raises(JournalError, match="task envelope fails its schema"):
        journal.record_run({"id": TASK_A}, {"task_id": TASK_A})
    assert len(journal) == 0


# -- no content -----------------------------------------------------------------


def test_no_payload_or_result_text_ever_enters_the_journal(tmp_path):
    path = tmp_path / "journal.jsonl"
    journal = NodeJournal(NODE_ID, path=path, now=lambda: FIXED)
    t = task(TASK_A)
    result, _s, report = run_through_sandbox(t, chunks=("a secret answer",))
    journal.record_run(t, result, report)
    blob = json.dumps(journal.entries()) + json.dumps(journal.report())
    blob += path.read_text()
    blob += json.dumps(journal.events)
    assert "a secret prompt" not in blob
    assert "a secret answer" not in blob


# -- durability -------------------------------------------------------------------


def test_a_durable_journal_survives_reopening_and_continues_its_sequence(tmp_path):
    path = tmp_path / "nested" / "journal.jsonl"
    clock = Clock()
    journal = NodeJournal(NODE_ID, path=path, now=clock)
    assert journal.durable is True
    assert journal.storage == "jsonl_file"
    t1 = task(TASK_A)
    result1, _stream1, report1 = run_through_sandbox(t1)
    journal.record_run(t1, result1, report1)
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    assert validate_entry(json.loads(lines[0]))["seq"] == 0

    reopened = NodeJournal(NODE_ID, path=path, now=clock)
    assert len(reopened) == 1
    t2 = task(TASK_B)
    result2, _s2, report2 = run_through_sandbox(t2)
    entry = reopened.record_run(t2, result2, report2)
    assert entry["seq"] == 1  # history is continued, never renumbered
    assert [e["task_id"] for e in reopened.entries()] == [TASK_A, TASK_B]
    report = reopened.report()
    assert report["durable"] is True
    assert report["storage"] == "jsonl_file"
    assert report["last_seq"] == 1


def test_a_corrupt_foreign_or_renumbered_file_is_refused_by_line(tmp_path):
    t = task(TASK_A)
    result, _s, report = run_through_sandbox(t)
    good = NodeJournal(NODE_ID, now=lambda: FIXED).record_run(t, result, report)

    corrupt = tmp_path / "corrupt.jsonl"
    corrupt.write_text(json.dumps(good) + "\nnot json at all\n")
    with pytest.raises(JournalError, match="line 2 is not valid JSON"):
        NodeJournal(NODE_ID, path=corrupt, now=lambda: FIXED)

    foreign = tmp_path / "foreign.jsonl"
    foreign.write_text(json.dumps(dict(good, node_id="another-node")) + "\n")
    with pytest.raises(JournalError, match="line 1 names node"):
        NodeJournal(NODE_ID, path=foreign, now=lambda: FIXED)

    renumbered = tmp_path / "renumbered.jsonl"
    renumbered.write_text(json.dumps(dict(good, seq=7)) + "\n")
    with pytest.raises(JournalError, match="sequence is broken"):
        NodeJournal(NODE_ID, path=renumbered, now=lambda: FIXED)


# -- the numbers --------------------------------------------------------------------


def test_the_report_counts_by_status_engine_and_model_with_measured_sums():
    clock = Clock()
    journal = NodeJournal(NODE_ID, now=clock)
    for task_id, engine, model in (
        (TASK_A, "ollama", "m1"),
        (TASK_B, "ollama", "m2"),
        (TASK_C, "aesir", "m1"),
    ):
        t = task(task_id)
        result, _s, report = run_through_sandbox(t, engine=engine, model=model)
        # run_through_sandbox's sandbox has a real timer; timings are
        # whatever it measured — the report must sum the entries' own.
        journal.record_run(t, result, report)
        clock.advance(seconds=30)
    report = journal.report()
    validate_report(report)
    assert report["total_entries"] == 3
    assert report["by_status"] == {
        "completed": 3,
        "failed": 0,
        "refused": 0,
        "cancelled": 0,
        "expired": 0,
    }
    assert report["by_engine"] == {"ollama": 2, "aesir": 1}
    assert report["by_model"] == {"m1": 2, "m2": 1}
    entries = journal.entries()
    assert report["total_run_ms"] == round(sum(e["timings_ms"]["run"] for e in entries), 3)
    assert report["mean_run_ms"] == round(report["total_run_ms"] / 3, 3)
    assert report["total_tokens_out"] == sum(e["tokens_out"] for e in entries)
    assert report["total_chars_out"] == sum(e["chars_out"] for e in entries)
    assert report["unmeasured_entries"] == 0
    assert report["first_recorded_at"] == entries[0]["recorded_at"]
    assert report["last_recorded_at"] == entries[-1]["recorded_at"]
    assert report["durable"] is False
    assert report["storage"] == "memory"


def test_an_empty_journal_reports_zeros_and_nulls():
    report = NodeJournal(NODE_ID, now=lambda: FIXED).report()
    validate_report(report)
    assert report["total_entries"] == 0
    assert report["mean_run_ms"] is None
    assert report["first_recorded_at"] is None
    assert report["last_recorded_at"] is None
    assert report["last_seq"] is None


# -- lookups and events ----------------------------------------------------------------


def test_lookups_keep_journal_order_and_a_retried_task_keeps_both_entries():
    journal = NodeJournal(NODE_ID, now=lambda: FIXED)
    for task_id, model in ((TASK_A, "m1"), (TASK_B, "m1"), (TASK_A, "m2")):
        t = task(task_id)
        result, _s, report = run_through_sandbox(t, model=model)
        journal.record_run(t, result, report)
    assert [e["seq"] for e in journal.for_task(TASK_A)] == [0, 2]
    assert [e["task_id"] for e in journal.for_model("m1")] == [TASK_A, TASK_B]
    assert [e["task_id"] for e in journal.for_engine("ollama")] == [TASK_A, TASK_B, TASK_A]
    assert journal.for_model("never-ran") == []


def test_every_recording_is_a_validated_event_with_names_and_counts_only():
    journal = NodeJournal(NODE_ID, now=lambda: FIXED)
    t = task(TASK_A)
    result, _s, report = run_through_sandbox(t)
    journal.record_run(t, result, report)
    (event,) = journal.events
    validate_event(event)
    assert event["type"] == "journal.task_recorded"
    assert event["task_id"] == TASK_A
    assert event["payload"]["status"] == "completed"
    assert event["payload"]["engine"] == "ollama"


# -- the node agent ------------------------------------------------------------------------


def provision(store_dir: Path, *, device_class: str = "vm") -> None:
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


def make_agent(tmp_path: Path, **kwargs) -> NodeAgent:
    store_dir = tmp_path / "keys"
    provision(store_dir)
    config = NodeConfig.from_dict(
        {"version": 1, "node_id": NODE_ID, "device_class": "vm", "key_store_dir": str(store_dir)}
    )
    return NodeAgent(config, now=lambda: FIXED, **kwargs)


def joined_agent(tmp_path: Path, **kwargs) -> NodeAgent:
    agent = make_agent(tmp_path, **kwargs)
    agent.start()
    agent.join()
    return agent


def test_execute_task_journals_the_run_and_status_carries_the_report(tmp_path):
    journal = NodeJournal(NODE_ID, now=lambda: FIXED)
    sandbox = TaskSandbox(NODE_ID, now=lambda: FIXED)
    queue = LocalQueue(NODE_ID, now=lambda: FIXED)
    agent = joined_agent(tmp_path, sandbox=sandbox, local_queue=queue, node_journal=journal)
    agent.queue_task(task(TASK_A))
    result, _stream, _report = agent.execute_next_queued(
        lambda t, c: iter(["done"]), engine="ollama", model="m1"
    )
    assert result["status"] == "completed"
    entries = agent.journal_entries()
    assert len(entries) == 1
    assert entries[0]["task_id"] == TASK_A
    assert agent.status()["journal"]["total_entries"] == 1
    assert agent.node_journal is journal
    agent.drain()
    agent.stop()


def test_a_governor_refused_run_never_reaches_the_journal(tmp_path):
    journal = NodeJournal(NODE_ID, now=lambda: FIXED)
    sandbox = TaskSandbox(NODE_ID, now=lambda: FIXED)
    hot = {
        "thermal_c": 95.0,
        "memory_used_bytes": 100,
        "memory_total_bytes": 1000,
        "cpu_share_used": 0.1,
    }
    governor = ResourceGovernor(NODE_ID, readings=lambda: hot, now=lambda: FIXED)
    agent = joined_agent(tmp_path, sandbox=sandbox, governor=governor, node_journal=journal)
    with pytest.raises(NodeAgentError, match="governor refused"):
        agent.execute_task(task(TASK_A), lambda t, c: iter(["x"]), engine="ollama", model="m1")
    assert agent.journal_entries() == []
    assert agent.journal_report()["total_entries"] == 0
    agent.drain()
    agent.stop()


def test_an_unwired_agent_keeps_no_book(tmp_path):
    agent = make_agent(tmp_path)
    assert agent.node_journal is None
    assert agent.journal_entries() == []
    assert agent.journal_report() is None
    assert agent.status()["journal"] is None


# -- the citizens -----------------------------------------------------------------------------


def test_the_pi_citizen_factory_wires_a_journal(tmp_path):
    store_dir = tmp_path / "keys"
    provision(store_dir, device_class="pi")
    config = NodeConfig.from_dict(
        {"version": 1, "node_id": NODE_ID, "device_class": "pi", "key_store_dir": str(store_dir)}
    )
    journal_file = tmp_path / "journal.jsonl"
    agent, _report = build_pi_citizen_agent(
        config,
        store_root=tmp_path / "store",
        probes=ProbeSet(
            cpu=lambda: {
                "arch": "aarch64",
                "cores": 4,
                "model": "Raspberry Pi 5 Model B Rev 1.1",
            },
            ram_gb=lambda: 16598576 / (1024 * 1024),
            accelerators=lambda: [{"kind": "none"}],
            disk_free_gb=lambda: 202.0,
            thermal_c=lambda: 41.9,
            vram_gb=lambda: None,
        ),
        readings=lambda: {
            "thermal_c": 41.9,
            "memory_used_bytes": 2 * 1024**3,
            "memory_total_bytes": 16 * 1024**3,
            "cpu_share_used": 0.25,
        },
        neighbours={
            "samtal_service": "active",
            "aurelia_port": "listening",
            "kiosk_port": "not_listening",
            "hailo_device": "absent",
        },
        simulated=True,
        now=lambda: FIXED,
        journal_path=journal_file,
    )
    assert agent.node_journal is not None
    assert agent.node_journal.durable is True
    agent.start()
    agent.join()
    fixture = tmp_path / "fixture-model.gguf"
    fixture.write_bytes(b"SIMULATED fixture model bytes for the journal citizen test")
    agent.model_store.add(
        fixture, name="fixture-model", family="fixture", quant="Q4_K_M", format="gguf"
    )
    agent.load_model("fixture-model")
    result, _s, _r = agent.execute_task(
        task(TASK_A),
        lambda t, c: iter(["citizen answer"]),
        engine="fake",
        model="fixture-model",
    )
    assert result["status"] == "completed"
    assert agent.journal_report()["total_entries"] == 1
    assert len(journal_file.read_text().splitlines()) == 1
    agent.drain()
    agent.stop()


def test_the_founding_readme_is_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
