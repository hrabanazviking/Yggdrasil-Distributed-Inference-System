"""Local queue tests — Y-031's acceptance.

The queue must prove: (a) priority order — the task's own
criticality (critical > standard > casual), FIFO within a
priority, no caller-set field reordering the line — recorded
on validated queued_task v1 entries that carry no payload
content; (b) the bound — a full queue refuses by the Y-007
name queue_full, a duplicate id refuses by name, and both
refusals are counted and recorded, never silent drops;
(c) the numbers — a validated queue_report v1 with depth,
capacity, free slots, utilisation, the by-priority
breakdown, the next task, and the oldest task's *measured*
wait (priority can leave a casual elder waiting behind newer
critical work, and the report must say so), plus lifetime
totals that only rise; (d) service — peek names without
taking, dequeue hands the executor the task itself in
service order, cancel removes a held task and refuses
history, held_task returns a copy; (e) events — every move a
validated Y-013 envelope with counts and names only; (f) the
node agent integration — queue depth told identically by
health() and the Y-030 heartbeat, queue_task only while
joined, execute_next_queued serving the head through the
sandbox and taking it off the queue only after the run, a
governor-refused run leaving its task held, status carrying
the report; (g) an unwired agent keeps reporting depth 0,
exactly as before; (h) the founding README is untouched.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from yggdrasil.governor import ResourceGovernor
from yggdrasil.identity import (
    LocalKeyStore,
    NodeKeypair,
    create_mesh_root,
    issue_node_certificate,
)
from yggdrasil.local_queue import (
    LocalQueue,
    QueueError,
    default_policy,
    validate_entry,
    validate_report,
)
from yggdrasil.node_agent import NodeAgent, NodeAgentError, NodeConfig
from yggdrasil.observability import validate_event
from yggdrasil.sandbox import TaskSandbox

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
NODE_ID = "test-node-1"
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"

TASK_IDS = [
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
    "44444444-4444-4444-8444-444444444444",
    "55555555-5555-4555-8555-555555555555",
]


def task(task_id, *, criticality=None, kind="generate.text", caller="test", **extra):
    record = {
        "id": task_id,
        "kind": kind,
        "caller": caller,
        "payload": {"prompt": "a secret prompt that must never leak"},
    }
    if criticality is not None:
        record["criticality"] = criticality
    record.update(extra)
    return record


class Clock:
    def __init__(self):
        self.moment = FIXED

    def __call__(self):
        return self.moment

    def advance(self, **kwargs):
        self.moment += timedelta(**kwargs)


# -- priority order -----------------------------------------------------------------


def test_priority_is_the_tasks_criticality_and_fifo_within_it():
    queue = LocalQueue(NODE_ID, now=lambda: FIXED)
    queue.enqueue(task(TASK_IDS[0], criticality="casual"))
    queue.enqueue(task(TASK_IDS[1]))  # no criticality: the envelope default, standard
    queue.enqueue(task(TASK_IDS[2], criticality="critical"))
    queue.enqueue(task(TASK_IDS[3], criticality="critical"))
    queue.enqueue(task(TASK_IDS[4], criticality="casual"))
    entries = queue.entries()
    assert [e["task_id"] for e in entries] == [
        TASK_IDS[2],
        TASK_IDS[3],
        TASK_IDS[1],
        TASK_IDS[0],
        TASK_IDS[4],
    ]
    assert [e["priority"] for e in entries] == [
        "critical",
        "critical",
        "standard",
        "casual",
        "casual",
    ]
    assert [e["priority_rank"] for e in entries] == [0, 0, 1, 2, 2]
    for entry in entries:
        validate_entry(entry)
    assert queue.peek()["task_id"] == TASK_IDS[2]


def test_an_entry_records_the_facts_and_never_the_payload():
    queue = LocalQueue(NODE_ID, now=lambda: FIXED)
    entry = queue.enqueue(
        task(
            TASK_IDS[0],
            criticality="critical",
            deadline_ms=5000,
            requirements={"model": "m.gguf", "capabilities": ["generate"]},
        )
    )
    assert entry == {
        "version": 1,
        "task_id": TASK_IDS[0],
        "node_id": NODE_ID,
        "kind": "generate.text",
        "caller": "test",
        "priority": "critical",
        "priority_rank": 0,
        "criticality": "critical",
        "model": "m.gguf",
        "enqueued_at": "2026-06-01T12:00:00Z",
        "seq": 0,
        "deadline_ms": 5000,
    }
    assert "secret prompt" not in repr(entry)
    assert "secret prompt" not in repr(queue.report())
    assert "secret prompt" not in repr(queue.events)


def test_no_caller_field_reorders_the_line():
    queue = LocalQueue(NODE_ID, now=lambda: FIXED)
    queue.enqueue(task(TASK_IDS[0]))  # standard, no deadline
    queue.enqueue(task(TASK_IDS[1], deadline_ms=1))  # standard, urgent deadline
    # Same priority: strict FIFO by queue seq — a deadline is
    # an execution bound (Y-029), not a queue-jumping licence.
    assert queue.peek()["task_id"] == TASK_IDS[0]


# -- the bound ----------------------------------------------------------------------


def test_a_full_queue_refuses_queue_full_and_counts_it():
    queue = LocalQueue(NODE_ID, {"version": 1, "max_depth": 2}, now=lambda: FIXED)
    queue.enqueue(task(TASK_IDS[0]))
    queue.enqueue(task(TASK_IDS[1]))
    assert queue.is_full
    with pytest.raises(QueueError, match="queue_full") as caught:
        queue.enqueue(task(TASK_IDS[2]))
    assert caught.value.error_code == "queue_full"
    assert len(queue) == 2
    report = queue.report()
    assert report["totals"]["rejected"] == 1
    assert report["totals"]["enqueued"] == 2


def test_a_duplicate_id_is_refused_and_counted():
    queue = LocalQueue(NODE_ID, now=lambda: FIXED)
    queue.enqueue(task(TASK_IDS[0]))
    with pytest.raises(QueueError, match="already held"):
        queue.enqueue(task(TASK_IDS[0]))
    assert queue.report()["totals"] == {
        "enqueued": 1,
        "dequeued": 0,
        "cancelled": 0,
        "rejected": 1,
    }


def test_a_malformed_task_and_a_bad_policy_are_refused():
    queue = LocalQueue(NODE_ID, now=lambda: FIXED)
    with pytest.raises(QueueError, match="task envelope"):
        queue.enqueue({"id": TASK_IDS[0]})
    with pytest.raises(QueueError, match="queue policy"):
        LocalQueue(NODE_ID, {"version": 1, "max_depth": 0})
    assert default_policy() == {"version": 1, "max_depth": 64}
    assert len(queue) == 0


# -- service --------------------------------------------------------------------------


def test_dequeue_hands_the_task_itself_in_service_order():
    queue = LocalQueue(NODE_ID, now=lambda: FIXED)
    queue.enqueue(task(TASK_IDS[0], criticality="casual"))
    held = task(TASK_IDS[1], criticality="critical")
    queue.enqueue(held)
    dequeued_task, entry = queue.dequeue()
    assert dequeued_task == held
    assert entry["task_id"] == TASK_IDS[1]
    assert len(queue) == 1
    assert queue.dequeue()[0]["id"] == TASK_IDS[0]
    assert queue.dequeue() is None


def test_peek_takes_nothing_and_held_task_is_a_copy():
    queue = LocalQueue(NODE_ID, now=lambda: FIXED)
    original = task(TASK_IDS[0])
    queue.enqueue(original)
    assert queue.peek()["task_id"] == TASK_IDS[0]
    assert len(queue) == 1
    copy = queue.held_task(TASK_IDS[0])
    assert copy == original
    copy["payload"] = {"prompt": "mutated"}
    assert queue.held_task(TASK_IDS[0]) == original
    assert queue.held_task(TASK_IDS[1]) is None


def test_cancel_removes_only_what_is_held():
    queue = LocalQueue(NODE_ID, now=lambda: FIXED)
    queue.enqueue(task(TASK_IDS[0]))
    queue.enqueue(task(TASK_IDS[1]))
    assert queue.cancel(TASK_IDS[0]) is True
    assert queue.cancel(TASK_IDS[0]) is False  # history cannot be cancelled
    assert [e["task_id"] for e in queue.entries()] == [TASK_IDS[1]]
    assert queue.report()["totals"]["cancelled"] == 1


def test_remove_by_id_takes_as_served():
    queue = LocalQueue(NODE_ID, now=lambda: FIXED)
    queue.enqueue(task(TASK_IDS[0]))
    entry = queue.remove(TASK_IDS[0])
    assert entry["task_id"] == TASK_IDS[0]
    assert queue.remove(TASK_IDS[0]) is None
    assert queue.report()["totals"]["dequeued"] == 1


# -- the numbers -----------------------------------------------------------------------


def test_the_report_says_how_busy_in_numbers():
    clock = Clock()
    queue = LocalQueue(NODE_ID, {"version": 1, "max_depth": 4}, now=clock)
    empty = queue.report()
    validate_report(empty)
    assert empty["depth"] == 0 and empty["free_slots"] == 4
    assert empty["utilisation"] == 0.0 and empty["is_full"] is False
    assert empty["oldest_task_id"] is None and empty["oldest_wait_ms"] is None
    assert empty["next_task_id"] is None
    queue.enqueue(task(TASK_IDS[0], criticality="casual"))
    clock.advance(seconds=30)
    queue.enqueue(task(TASK_IDS[1], criticality="critical"))
    report = queue.report()
    assert report["depth"] == 2 and report["free_slots"] == 2
    assert report["utilisation"] == 0.5
    assert report["by_priority"] == {"critical": 1, "standard": 0, "casual": 1}
    # The head is the critical newcomer, but the longest wait
    # belongs to the casual elder — and the report says so.
    assert report["next_task_id"] == TASK_IDS[1]
    assert report["oldest_task_id"] == TASK_IDS[0]
    assert report["oldest_wait_ms"] == 30_000.0
    assert queue.wait_ms(TASK_IDS[1]) == 0.0
    assert queue.wait_ms(TASK_IDS[2]) is None


def test_waits_are_measured_on_the_queues_clock():
    clock = Clock()
    queue = LocalQueue(NODE_ID, now=clock)
    queue.enqueue(task(TASK_IDS[0]))
    clock.advance(milliseconds=250)
    assert queue.wait_ms(TASK_IDS[0]) == 250.0
    queue.dequeue()
    event = queue.events[-1]
    assert event["type"] == "queue.task_dequeued"
    assert event["payload"]["wait_ms"] == 250.0


def test_events_are_validated_and_carry_counts_only():
    queue = LocalQueue(NODE_ID, {"version": 1, "max_depth": 1}, now=lambda: FIXED)
    queue.enqueue(task(TASK_IDS[0]))
    with pytest.raises(QueueError):
        queue.enqueue(task(TASK_IDS[1]))
    queue.cancel(TASK_IDS[0])
    kinds = [e["type"] for e in queue.events]
    assert kinds == ["queue.task_enqueued", "queue.task_rejected", "queue.task_cancelled"]
    for event in queue.events:
        validate_event(event)
        assert event["source"] == {"node_id": NODE_ID, "component": "node_agent"}
    assert queue.events[1]["payload"]["error_code"] == "queue_full"


# -- node agent integration -------------------------------------------------------------


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
        {"version": 1, "node_id": NODE_ID, "device_class": "vm", "key_store_dir": str(store_dir)}
    )
    return NodeAgent(config, now=lambda: FIXED, **kwargs)


def joined_agent(tmp_path: Path, **kwargs) -> NodeAgent:
    agent = make_agent(tmp_path, **kwargs)
    agent.start()
    agent.join()
    return agent


def test_agent_health_and_heartbeat_tell_the_queues_depth(tmp_path):
    queue = LocalQueue(NODE_ID, now=lambda: FIXED)
    agent = joined_agent(tmp_path, local_queue=queue)
    assert agent.health()["queue_depth"] == 0
    agent.queue_task(task(TASK_IDS[0], criticality="critical"))
    agent.queue_task(task(TASK_IDS[1]))
    assert agent.health()["queue_depth"] == 2
    beat = agent.heartbeat()
    assert beat["load"]["queue_depth"] == 2
    assert beat["health"]["queue_depth"] == 2
    assert agent.next_queued_task()["task_id"] == TASK_IDS[0]
    assert agent.status()["queue"]["depth"] == 2
    assert agent.queue_report()["by_priority"] == {"critical": 1, "standard": 1, "casual": 0}


def test_queue_task_only_while_joined(tmp_path):
    queue = LocalQueue(NODE_ID, now=lambda: FIXED)
    agent = make_agent(tmp_path, local_queue=queue)
    with pytest.raises(NodeAgentError, match="queue_task"):
        agent.queue_task(task(TASK_IDS[0]))
    agent.start()
    agent.join()
    agent.queue_task(task(TASK_IDS[0]))
    agent.drain()
    with pytest.raises(NodeAgentError, match="queue_task"):
        agent.queue_task(task(TASK_IDS[1]))  # a draining node gathers nothing new
    assert agent.cancel_queued_task(TASK_IDS[0]) is True  # but it can shed what it holds
    agent.stop()


def test_agent_queue_full_refusal_keeps_its_name(tmp_path):
    queue = LocalQueue(NODE_ID, {"version": 1, "max_depth": 1}, now=lambda: FIXED)
    agent = joined_agent(tmp_path, local_queue=queue)
    agent.queue_task(task(TASK_IDS[0]))
    with pytest.raises(NodeAgentError, match="queue_full"):
        agent.queue_task(task(TASK_IDS[1]))


def test_execute_next_queued_serves_the_head_and_takes_it_off(tmp_path):
    queue = LocalQueue(NODE_ID, now=lambda: FIXED)
    sandbox = TaskSandbox(NODE_ID, now=lambda: FIXED)
    agent = joined_agent(tmp_path, local_queue=queue, sandbox=sandbox)
    agent.queue_task(task(TASK_IDS[0], criticality="casual"))
    agent.queue_task(task(TASK_IDS[1], criticality="critical"))
    result, _stream, report = agent.execute_next_queued(
        lambda t, c: iter(["done"]), engine="fake", model="m"
    )
    assert result["task_id"] == TASK_IDS[1]  # the critical head ran first
    assert result["status"] == "completed"
    assert report["status"] == "completed"
    assert len(queue) == 1
    assert agent.health()["queue_depth"] == 1
    agent.execute_next_queued(lambda t, c: iter(["done"]), engine="fake", model="m")
    assert len(queue) == 0
    assert agent.execute_next_queued(lambda t, c: iter(["x"]), engine="fake", model="m") is None


HOT = {
    "thermal_c": 95.0,
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


def test_a_governor_refused_run_leaves_its_task_held(tmp_path):
    governor = ResourceGovernor(NODE_ID, POLICY, readings=lambda: dict(HOT), now=lambda: FIXED)
    queue = LocalQueue(NODE_ID, now=lambda: FIXED)
    sandbox = TaskSandbox(NODE_ID, now=lambda: FIXED)
    agent = joined_agent(tmp_path, local_queue=queue, sandbox=sandbox, governor=governor)
    agent.queue_task(task(TASK_IDS[0]))
    with pytest.raises(NodeAgentError, match="governor refused"):
        agent.execute_next_queued(lambda t, c: iter(["x"]), engine="fake", model="m")
    # The task did not vanish with the refusal: it stays held,
    # depth unchanged, ready for a cooler moment.
    assert len(queue) == 1
    assert queue.report()["totals"]["dequeued"] == 0


def test_an_unwired_agent_reports_depth_zero_and_refuses_queue_calls(tmp_path):
    agent = joined_agent(tmp_path)
    assert agent.health()["queue_depth"] == 0
    assert agent.heartbeat()["load"]["queue_depth"] == 0
    assert agent.status()["queue"] is None
    assert agent.queue_report() is None
    with pytest.raises(NodeAgentError, match="no local queue"):
        agent.queue_task(task(TASK_IDS[0]))


# -- the founding document ---------------------------------------------------------------


def test_readme_unchanged():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
