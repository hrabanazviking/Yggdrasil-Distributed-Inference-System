# The Node Journal (Y-039)

`src/yggdrasil/node_journal.py` — the node's book of itself:
every task the node ran, with engine, model, timings, and
outcome. It is the raw material Urðr ancestry is built from
(Part V): until this slice the record of a run scattered —
the sandbox kept its reports in its own list, the queue
counted what it let go, the failure monitor sorted the
accidents — and nothing could answer *what have you run, on
what, how long did it take, and how did it end?*

## One entry per run, from the run's own records

`record_run(task, result, sandbox_report=None)` takes the
finished run's own three records — the Y-007 task envelope,
its terminal ResultEnvelope, and the Y-029 sandbox report
when one exists — and writes one validated `journal_entry`
v1 (`contracts/schemas/journal_entry.schema.json`):

- task id, kind, caller, criticality (the envelope's own
  default, `standard`, when it names none);
- the task's context references as **names** —
  `collection/id`, with `@v<version>` when the reference
  carries one — never the referenced content;
- engine, model, quant, and verification from the result's
  own provenance (absent quant/verification are `null`,
  never guessed);
- the outcome under its Y-007 status name, the error code
  when it failed, and the partial flag;
- the measured timings from the result (`queue` / `run` /
  `total` ms), and — when a sandbox report rides along —
  its measured `tokens_out`, `chars_out`, and `bound_hit`.

**The records must agree.** A result naming a different
task, a provenance naming a different node, a sandbox
report for another task or another node, a sandbox status
that disagrees with the result's, or a sandbox error code
that disagrees with the result's are each refused by name,
and nothing is written for a refused recording. A journal
that accepts mismatched records is a rumour book, not
ancestry.

**Facts, never content.** Entries carry identifiers, names,
counts, and timings only. The task's payload and the
result's text never enter an entry, the report, the events,
or the durable file — proven by test.

An entry recorded without a sandbox report carries `null`
measurements and is counted in the report as an
`unmeasured_entries` member — unmeasured is stated, never
dressed with invented numbers.

## Durability, when the operator says so

- With no path, the journal is in-process memory, and its
  report says so: `storage: "memory"`, `durable: false`.
- With a `path`, every entry is appended as one JSON line
  to that local file before `record_run` returns. A journal
  opened on an existing file **loads** it: every line is
  parsed and validated, must name this node, and must
  continue the file's own sequence (0, 1, 2, …) exactly. A
  corrupt, foreign, renumbered, or gapped line is refused
  by line number — never silently skipped or truncated
  away — and new entries continue the sequence rather than
  renumbering history.

The transport is a local file; there is no network path in
the module. No fsync-per-entry guarantee is claimed beyond
the append itself, and no rotation or compaction exists
yet.

## Reading the book back

- `entries()` — every entry, in journal (finish) order.
- `for_task(task_id)` — every entry for one task; a retried
  task keeps both of its entries.
- `for_model(model)` / `for_engine(engine)` — the runs one
  model or engine carried, in journal order. These are the
  views Y-078's fitness memory and Part V's outcome ledger
  (Y-103) will read.
- `report()` — the validated `journal_report` v1
  (`journal_report.schema.json`): totals by status, engine,
  and model; the partial count; summed and mean measured
  run time; summed tokens/chars over the measured entries,
  with the unmeasured count beside them; the first and last
  recorded moments; the last sequence number; and the
  durability truth.

Every recording is also a validated Y-013
`journal.task_recorded` EventEnvelope (sequence, status,
engine, model, run time, error code — names and counts
only), so the node's observability stream and its journal
can be cross-checked against each other.

## Node agent

An agent built with `node_journal=` grows:

- `execute_task` journals **every finished sandbox run** —
  completed, failed, cancelled, expired alike — after the
  failure monitor has sorted it (Y-032), from the run's own
  task, result, and sandbox report. Runs served through the
  queue (`execute_next_queued`) are journaled by the same
  path.
- A run **refused before it starts** — the governor's door
  (Y-028), a non-resident model (Y-027) — never reaches the
  sandbox and is *not* journaled: the journal is of tasks
  the node ran, and a refusal at the door is the governor's
  own event, not a run.
- `journal_entries()` reads the book; `journal_report()`
  answers its numbers; `status()` carries the report under
  `journal`. With no journal wired, all of this is exactly
  as before: an empty list, a `None` report.

All four citizen factories (Pi, laptop, VM, phone) wire a
journal over their agent: in-memory by default, or the
durable file when the factory is handed `journal_path=`.

## Not built here

The journal is **this node's own book**: it is not Urðr —
no graph, no replication, no cross-node ancestry (that is
Part V, which will treat these entries as its raw
material) — and it is not the task lifecycle journal of
Part IV (Y-075, the Verðandi role-holder's single-writer
log); it is the per-node record both will draw on. It does
not journal model loads, fetches, or lifecycle moves —
those have their own records and events (Y-027, Y-037);
this book is tasks run, and only tasks run. A file an
operator edits by hand is refused at load, not repaired.
Nothing here claims a hardware run beyond the citizens'
own suites.
