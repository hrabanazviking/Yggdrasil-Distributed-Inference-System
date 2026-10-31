# The Task Execution Sandbox (Y-029)

`src/yggdrasil/sandbox.py` — where admitted work actually
runs: bounded in time, tokens, and output; cancellable; with
streamed partial results. The lifecycle (Y-021) leases work,
the governor (Y-028) refuses it at the door, the lifecycle
manager (Y-027) holds the model — the sandbox is the room
the work runs in, and the room has walls.

## Bounds as data

A `SandboxPolicy` (`sandbox_policy` schema v1) names:

- `max_run_ms` — wall time per task, measured on a monotonic
  timer;
- `max_output_tokens` — the output ceiling in token units;
- `max_output_chars` — the output ceiling in characters;
- `token_counting` — how a "token" is counted (below).

`default_policy()` is the operator's starting point (two
minutes, 4096 token units, 64 KiB) — a policy, not a fact
about any machine.

A task may only **tighten** its bounds: its `deadline_ms`
caps the time bound, and `payload.max_tokens` /
`payload.max_output_tokens` / `payload.max_output_chars`
cap the output bounds. Asking for more than the policy
gives nothing — the report records the bounds actually
applied.

## Token counting is named, never pretended

Two counters exist, and the policy says which is in force:

- `per_chunk` — each chunk the runner yields is one token
  unit (the shape engine adapters stream in: one event per
  token piece);
- `whitespace_words` — whitespace-separated words in the
  text.

Neither is a real tokeniser, and neither claims to be; the
counts in every report are counts *under the named counter*.

## Streaming, and bounds biting mid-stream

The runner yields text chunks; each becomes a Y-007 `token`
StreamEvent as it arrives (an `on_event` callback receives
each in turn), between `accepted`/`started` and exactly one
terminal event. Output the caller already holds is never
taken back:

- **Output bound reached** — the crossing chunk is truncated
  to the room left (or dropped when none is left), the
  runner is closed, and the task **completes** with
  `partial: true`; the report's `bound_hit` names `tokens`
  or `output_chars`.
- **Time bound reached** — the task ends `expired` with
  `deadline_unmeetable`; a chunk that arrives after the
  bound is dropped, not streamed. The time bound is
  *cooperative* — checked between chunks, so a runner that
  never yields cannot be interrupted by this layer (wedged
  engine processes are Y-032's failure shapes and the
  supervisor's, not claimed here).
- **Cancelled** — see below; the result is `cancelled` with
  `cancelled_by_caller`.
- **Runner failure** — a `SimulatedTaskFailure` or any
  exception carrying a Y-007 `error_code` fails the task
  under that code; any other exception is `engine_error`.
  Text streamed before the failure is kept, partial.

## Cancellation

`sandbox.cancel(task_id)` — from any thread, or from an
`on_event` callback — flags the active execution; the loop
sees it between chunks, **closes the runner** (its `finally`
blocks run: cleanup is the runner's, the close is
guaranteed), and ends the task `cancelled` with the partial
text. A runner may also poll its `control.cancelled` inside
long stretches between yields. Cancelling a finished or
unknown task returns `False` — nothing is rewritten after
the fact.

## The runner protocol, and the bridges

`runner(task, control)` returns an iterator of text chunks,
or a single result dict (its `text` streams as one chunk;
its other fields ride into the result). Two bridges bring
the tree's existing shapes in:

- `result_handler_runner(handler)` — a dict-returning
  handler (the simulator / fake-engine shape, Y-014/Y-015);
- `adapter_generate_runner(adapter, node_id)` — an engine
  adapter's `generate()` pair stream (Y-023 – Y-025): its
  token events' text streams through the sandbox, and the
  sandbox's bounded result supersedes the adapter's own —
  the bounds, not the adapter, decide when the run ends.

## The record

Every execution leaves a validated `sandbox_report` v1 —
bounds applied, measured `run_ms`, tokens/chars out, the
bound hit, the error code — in `sandbox.reports`, plus a
`sandbox.task_<status>` Y-013 EventEnvelope from the
`node_agent` component in `sandbox.events`. Payload content
never enters an event: counts, bounds, and names only.

## Node agent

A `NodeAgent` built with `sandbox=` runs tasks through
`execute_task(task, runner, engine=…, model=…)`:

1. `begin_task` takes the lease (joined only; the governor's
   admission, Y-028, rides along when one is wired);
2. a model-lifecycle use lease is taken on the model when a
   lifecycle is wired (Y-027) — the running task's model
   cannot be unloaded or evicted mid-run, and a model that
   is not resident refuses the run by name, lease returned;
3. the sandbox runs the task, streamed through `on_event`;
4. both leases release, whatever the outcome.

`status()` carries the sandbox's policy, its active task
ids, and its finished execution count. Without a sandbox
wired, the agent still only leases, exactly as before.

## Not built here

No OS-level isolation — this sandbox bounds the stream it
is handed, in-process; it does not confine syscalls, files,
or memory (tasks-are-data enforcement is Y-043; engine
crash / OOM containment is Y-032; the local queue that feeds
tasks in has landed at Y-031 — see LOCAL_QUEUE.md, whose
`execute_next_queued` serves its head through this sandbox).
No hardware claim: the suite's runners
are fixtures and a stub adapter, its timer injected; the
citizens that run this on named hardware are Y-033 – Y-035.
