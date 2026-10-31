# The Local Queue (Y-031)

`src/yggdrasil/local_queue.py` — the node's holding pen:
tasks held per node, in priority order, behind a depth
bound, with the node's busyness reported in numbers. Until
this slice the health report's `queue_depth` was honestly
0; now, when a queue is wired, it is the queue's true depth.

## Priority, from the task's own word

A task's priority **is** its Y-007 criticality:
`critical` (rank 0) before `standard` (rank 1) before
`casual` (rank 2) — the order the task contract has always
named them, with the envelope's own default (`standard`)
when a task names none. Within one priority the queue is
strictly FIFO by the queue's own monotonic sequence. No
caller-set field reorders the line: a deadline is an
execution bound for the sandbox (Y-029), not a
queue-jumping licence, and the payload is never consulted
for order.

Every held task is recorded as a validated `queued_task`
v1 entry (`contracts/schemas/queued_task.schema.json`):
task id, node, kind, caller, the derived priority *and*
its rank, the declared model (from the task's
requirements, or `null`), the enqueue moment, the queue
sequence, and the declared deadline. Entries, reports, and
events carry identifiers, kinds, priorities, and counts
only — never payload content; an entry can be audited to
show exactly why a task stands where it stands.

## The bound, and refusals that are counted

A `queue_policy` v1 (`queue_policy.schema.json`) names the
node's `max_depth`; the shipped default (64) is an
operator's policy, not a fact about any machine. Two
refusals, both by name, both counted in the report's
`rejected` total and recorded as `queue.task_rejected`
events — a busy node turning work away says so instead of
dropping tasks silently:

| Refusal | Name |
|---|---|
| queue already holds `max_depth` tasks | `QueueError` with Y-007 `error_code = "queue_full"` |
| the same task id is already held | duplicate, refused by name |

## A busy node says how busy, in numbers

`report()` answers the validated `queue_report` v1
(`queue_report.schema.json`):

- `depth`, `capacity`, `free_slots`, `is_full`, and
  `utilisation` (depth ÷ capacity — measured, never
  estimated);
- `by_priority` — the depth broken down critical /
  standard / casual;
- `next_task_id` — the head of the service order;
- `oldest_task_id` + `oldest_wait_ms` — the *longest* wait,
  which is not necessarily the head's: priority can keep a
  casual elder waiting behind newer critical work, and the
  report names that elder and its measured wait (the
  queue's clock against its enqueue moment) rather than
  hiding it behind the head's freshness;
- lifetime `totals` — enqueued, dequeued, cancelled,
  rejected — which only rise.

## Service

- `enqueue(task)` validates the envelope, derives the
  priority, and returns the entry.
- `peek()` names the next entry without taking it;
  `held_task(task_id)` returns a copy of a held envelope
  for the executor about to serve it.
- `dequeue()` takes the head of the service order and
  returns `(task, entry)` — the work itself, for an
  executor — closing that entry's wait.
- `cancel(task_id)` removes a held task (recorded as
  cancelled); it returns False for a task not held — a
  *running* task is the sandbox's to cancel (Y-029), a
  finished one is history.
- `remove(task_id)` takes a named task off as served —
  the dequeue bookkeeping by id, used by the agent's
  execute-next path below.

Every move is recorded as a validated Y-013 EventEnvelope
from the `node_agent` component (`queue.task_enqueued`,
`queue.task_dequeued`, `queue.task_cancelled`,
`queue.task_rejected`) with counts, priorities, and
measured waits only. All mutations are lock-guarded, so an
enqueue from a future fabric thread and a dequeue from an
executor thread cannot tear the line.

## Node agent

An agent built with `local_queue=` grows:

- `queue_task(task)` — joined only. A draining node takes
  no new held work, exactly as it takes no new leases; the
  governor is *not* asked at queue time (admission is a
  lease-time act, Y-028), so a hot node's queue can hold
  work for a cooler moment.
- `cancel_queued_task(task_id)` — joined or draining: a
  shedding node can drop what it holds.
- `next_queued_task()` — the head entry, untaken.
- `execute_next_queued(runner, engine=, model=)` — peek
  the head, run it through `execute_task` (the lease, the
  governor's admission, the model's use lease, the
  sandbox's bounds), and only *then* take it off the
  queue. A run refused before it starts leaves its task
  held, not vanished; an empty queue returns None.
- `health()` reports the true queue depth (the Y-030
  heartbeat's load section agrees by construction — the
  endpoint refuses a beat whose load disagrees with the
  health report), and `status()` carries the full
  `queue_report`. With no queue wired, all of this is
  exactly as before: depth 0, report `None`.

## Not built here

The queue is **in-process memory**: it is not durable —
a restarted agent holds nothing, and no task is claimed
held across a restart (the durable record of work done is
the node journal, Y-039). It is not a mesh queue — no peer
sees or shares this line (fabric carriage is Part III,
load-aware placement reading these numbers is Part IV).
No task expires in the queue in this slice: a deadline is
enforced by the sandbox at run time (Y-029), and queue-wait
expiry was considered with Y-032's failure work and stays
out of its four shapes (a deadline outcome is the sandbox's
`deadline_unmeetable`, not a node failure — see
FAILURE_SHAPES.md). No hardware claim: the
suite's clocks are injected and its agents run on this VM;
the citizens are Y-033 – Y-035.
