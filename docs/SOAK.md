# Node Soak — one node, 1,000 tasks, leak and drift in numbers (Y-047)

The long run, as a reusable harness. Every earlier Part II
slice proved a component on a handful of tasks; nothing had
run a node long enough for slow faults to show — a lease
taken and never returned, a sandbox execution left active,
records multiplying beyond the tasks that made them, a
completion rate sagging as the run goes on. This slice
(`src/yggdrasil/soak.py`) is that run: `SoakHarness` drives
one joined node through the policy's tasks and answers the
validated `soak_report` v1; `require_stable()` is the gate
in one call.

## The workload

The policy is data (`soak_policy` v1; the default is the
roadmap's own: **1,000 tasks, windows of 100**, completion
rate may drift by at most 0.1, mean output by at most
0.25). The workload is a pure function of the task count,
so a fresh harness replays it identically:

- three of Y-015's fake engines in rotation — echo,
  latency-shaped, failure-shaped — under three engine
  names (`ollama`, `aesir`, `llamacpp`) and three model
  names, the way a multi-engine node (Y-045) mixes them;
- the failure engine fails every fifth of *its own* calls
  with `engine_error`: dealt 333 of the 1,000 tasks, its
  tally is arithmetic — **66 failed, 934 completed**;
- every 25th task is an `embed.text` batch, the rest
  `chat.generate`, with prompt lengths cycling over ten
  values so every full window carries the same expected
  output shape.

Tasks go through the agent's own `execute_task` door —
identity, lifecycle, governor, task guard, failure
monitor, and journal all see them exactly as a caller's
tasks. A task refused *before* it starts is counted
`refused` and the run continues; the retained-record
expectations are reduced by exactly the refused count
(the journal's Y-039 rule). A node whose wired lifecycle
has not loaded the soak models is refused *up front*,
by name — the harness does not manufacture 1,000 door
refusals and call it a soak.

## Leak, in numbers

After the last task, the report's `residuals` read the
node's operational state: in-flight leases, sandbox
active executions, and queue depth must all be zero.
Retained records are counted exactly, not waved at: one
sandbox report per task that reached the sandbox, and
(when a journal is wired) one journal entry per such
task — more is multiplication, less is loss, both are
the `leaked` verdict. Process memory rides along as the
peak-RSS watermark before / after / growth: a number for
the operator, gating nothing — a peak watermark is not a
leak proof.

## Drift, in numbers

Results are partitioned in run order into windows
(`soak_window` v1: outcomes, completion rate, measured
mean run ms / tokens / chars). Drift compares the first
window against the last: completion-rate drift and
mean-output drift fraction against the policy's
tolerances — past them, the verdict is `drifted`. The
mean-run-ms drift fraction is *reported* (null when the
first window's mean is zero — no fraction is invented
over a zero base) but never gates: wall-clock means on a
shared machine are weather, not character.

The verdict is one word with a precedence: `leaked`
outranks `drifted` outranks `stable`. Each run records
one Y-013 `soak.<verdict>` event, counts only.

## Acceptance

The in-tree suite runs the full 1,000-task soak and
checks the arithmetic tally by engine (334 / 333 / 333
dealt; 66 failures, all the failure engine's), the zero
residuals and exact retained counts, the ten windows and
their sums, a second run replaying identically with the
retained counts growing by exactly another run, a
zero-tolerance policy naming the same workload `drifted`,
a governor that refuses everything yielding 1,000 counted
refusals with no leak verdict from the counting alone,
and misuse (no sandbox, not joined, unloaded soak models,
unshaped policy) refused before any task is dealt.

## Not built here / honesty

- Everything here is **SIMULATED** — the report's
  `simulated` field is a schema constant, `true`. The
  fake engines perform no inference and no live Aesir,
  Ollama, or llama.cpp server is claimed; the on-hardware
  soak belongs to Part VIII.
- No node agent seam grows and no citizen factory is
  rewired: the harness is run by whoever wants the
  evidence, and the report is the evidence.
- The soak measures one node in-process, sequentially.
  Concurrent load, mesh-level soak, and multi-day runs
  are later parts' work, not this slice's claim.
- The Part II gate — three real citizens (Pi, laptop,
  VM) running the conformance kit green against their
  real engines — landed at Y-048 (`docs/PART_II_GATE.md`),
  and it inherits nothing from this slice's verdict: the
  gate re-ran the kit and the citizens itself.
