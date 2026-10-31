# The Observability Contract v1 — Y-013

*What every component logs and meters, in what shape, kept where.
Written before the components, per the roadmap's order. Machine
shapes live in `contracts/schemas/`; this document is the meaning
behind them. The discipline in one line: **structured, local, no
phone-home** — the tree watches itself for its operator, and tells
nobody else.*

## Three shapes, and only three

Every observable fact in the mesh is one of:

- a **LogRecord** (`log_record.schema.json`) — something happened;
- a **MeterSample** (`meter_sample.schema.json`) — a measured
  number with a name, a kind, and a unit;
- an **EventEnvelope** (`event_envelope.schema.json`) — a lifecycle
  fact other components (or the operator's door, Y-017) may act on.

Free-text log files, ad-hoc counters, and bespoke event dicts are
contract violations, however convenient they feel at 2 a.m.

## LogRecords

Every record carries: timestamp, level (`debug` < `info` < `warn`
< `error`), component (one closed vocabulary: `node_agent`,
`fabric`, `router`, `memory`, `verifier`, `ensemble`, `registry`,
`identity`, `operator_cli`, `simulator`), the emitting node, a
dotted event name (`task.completed`, `model.load_refused`), a
human message, and `payload_logged: false` — a constant the schema
enforces, so every line affirmatively states it carries no payload.

Rules that bite:

- **No payload content, ever.** Prompts, completions, retrieved
  documents, and credentials never appear in a record — not in the
  message, not in `fields`. Records carry ids, counts, timings,
  sizes, and error names. A record that needs the payload to make
  sense is the wrong record.
- **Errors have names.** Any record at `error` level about a failed
  task carries the Y-007 `error_code`; "something went wrong" is
  not a log line.
- **Correlation is cheap and mandatory.** Work done for a task
  carries its `task_id`; work done for a wider flow carries a
  `correlation_id`, so one request can be followed across nodes
  from local records alone.
- **Fields are scalars.** `fields` values are string / number /
  boolean / null — no nested objects, no lists of documents.

## MeterSamples

A meter has a dotted name, a kind (`counter`, `gauge`,
`histogram_summary`), a unit from the closed list, and small
string labels (at most 8). The standard meters every node agent
keeps are named in `src/yggdrasil/observability.py` (`METERS`):
task outcomes, queue depth, model load/swap time, fabric traffic,
verification outcomes, node temperature. Counters only rise;
gauges report the present; histogram summaries report measured
durations — measured, never estimated, per the mesh's honesty
rules. A component may add meters only by adding them to that
catalog as data, with kind and unit, so a dashboard (Y-202) can
trust every name it reads.

## EventEnvelopes — the Verdandi shapes, reused where they fit

Lifecycle events ride in an envelope with the shape family the
workshop's Verdandi event fabric already speaks (the Hermes
plugin's fabric, per the roadmap §1): an envelope carrying
`type`, `source` (node + component), `at`, a per-source `seq`, an
optional correlation/task id, and an event-specific `payload`
object. Y-013 adopts that envelope now so later parts emit one
event language from the start. The payload obeys the LogRecord
redaction rule: ids, counts, timings, names — never inference
content.

Honesty marker: the exact field names the Hermes plugin emits are
verified against the plugin itself at Y-197, which is a mapping
exercise against this envelope, not a redesign. This contract does
not claim that verification has happened.

## Where records live

Records are **local**: appended as JSON Lines to files under the
node's own state directory (or held in memory by the simulator),
readable by the operator's door on that node, and carried over the
fabric only as EventEnvelopes a component explicitly publishes.
There is no telemetry endpoint, no external collector, no
phone-home of any kind — a node with its network cable pulled
loses no observability. Retention is the operator's local policy;
rotation is Part II's housekeeping, shaped by these schemas.

## Honest limits

Y-013 is a contract plus a validating in-process library
(`src/yggdrasil/observability.py`) and a local JSONL sink. No
component emits these records yet — emitters arrive with the
components themselves (Part II onward), the fabric carriage of
events is Part III, and the one-dashboard view is Y-202. Nothing
here has run on hardware, and nothing here claims it has.
