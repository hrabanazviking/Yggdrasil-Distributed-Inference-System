# The Task Contract v1 — Y-007

*One envelope for work, one for results, one stream shape, one error
taxonomy. Machine shapes in `contracts/schemas/`.*

## TaskEnvelope (`task_envelope.schema.json`)

- `id` — UUID, the task's only name; duplicates are reconciled by id
  (Part III, Y-058), never double-answered.
- `kind` — dotted task kind (`chat.generate`, `embed.corpus`,
  `vision.describe`, …); semantic routing (Y-079) keys on it.
- `payload` — kind-specific object; tasks are **data, never code**
  (threat model Y-010), unless a signed task kind explicitly says so.
- `criticality` — `casual | standard | critical`: what the task may
  buy (cheapest adequate → most reliable + verification).
- `context_refs` — Urðr references (collection, id, version), resolved
  at the executing node (Y-105); context is never copied into envelopes.
- `requirements` — model, quant floor, capabilities, context size:
  matched against census truth (Y-076), never hoped.
- `verification` — required? at what level (`schema | invariant |
  shadow | audit`)? Skuld (Part VI) owns the levels' meanings.
- `deadline_ms`, `scopes`, `deterministic` — deadline honesty (Y-081),
  Urðr privacy scopes (Y-117), and the no-model deterministic tier
  (Y-162).

## ResultEnvelope (`result_envelope.schema.json`)

Exactly one per task, terminal. `status` ∈ completed / failed /
refused / cancelled / expired. `provenance` (node, engine, model,
quant, verification mark) is mandatory — an answer without a receipt
is not a mesh answer (Y-098). `partial: true` marks an honest partial
from a broken run (Y-165).

## StreamEvent (`stream_event.schema.json`)

`accepted → started → token/partial… → completed|failed`, sequenced
per task. Long generations pin to their node; a mid-stream death is
Skuld's resume-or-restart judgement (Y-082).

## Error taxonomy (`error_codes.schema.json`)

Sixteen named failures, from `no_capable_node` to `malformed_envelope`.
A failure that has no name yet gets one added here — components never
invent ad-hoc error strings.
