# Observability Contract — Y-013

*Structured, local, no phone-home. The tree watches itself for
its operator and tells nobody else. The full contract is
`contracts/observability_contract.md`; this page is the short
form.*

## The three shapes

Everything observable in the mesh is exactly one of:

- **LogRecord** (`contracts/schemas/log_record.schema.json`) —
  something happened: timestamp, level, component (one closed
  vocabulary of ten), node, dotted event name, message, scalar
  fields, and the constant `payload_logged: false`.
- **MeterSample** (`contracts/schemas/meter_sample.schema.json`)
  — one measured number: dotted name, kind (`counter`, `gauge`,
  `histogram_summary`), a unit from the closed list, at most eight
  string labels.
- **EventEnvelope** (`contracts/schemas/event_envelope.schema.json`)
  — a lifecycle fact in the Verdandi shape family (type, source,
  at, per-source seq, correlation, payload), so every later part
  emits one event language from the start. The Hermes plugin's
  exact emitted field names are verified at Y-197 as a mapping
  against this envelope; that verification is **not** claimed here.

## The rules that bite

- **No payload content in any record.** Prompts, completions,
  documents, and credentials appear in no message, field, label,
  or event payload — the builder in
  `src/yggdrasil/observability.py` refuses banned keys outright,
  and `payload_logged` is a schema constant, not a promise.
  Records carry ids, counts, timings, sizes, and error names.
- **Errors have names.** Failures cite the Y-007 error taxonomy.
- **Meters are a catalog, not a habit.** The standard meters live
  as data in `METERS` (task outcomes, queue depth, model load/swap
  time, fabric traffic, verification outcomes, temperature); a new
  meter is a catalog row with kind and unit, or it does not exist.
  Counters only rise, and durations cannot be negative — the
  registry refuses both.
- **Local only.** The one sink in this slice appends JSON Lines
  to a file on the node's own disk (`LocalJsonlSink.transport` is
  `"local-file"`; there is no network code path). Fabric carriage
  of events is explicit publication, Part III — never telemetry.

## Honest limits

- Y-013 is a contract, schemas, and a validating library. **No
  component emits these records yet** — emitters arrive with the
  components (Part II onward); the one-dashboard view is Y-202.
- The histogram summary keeps the last measured value per meter;
  distribution aggregation belongs to the dashboard layer and is
  not claimed here.
- Nothing in this slice has run on hardware.
