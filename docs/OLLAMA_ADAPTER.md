# Engine Adapter I — Ollama — Y-023

*Part II, third slice: the first real engine wrapped behind
the node contract. Ollama is the common local shelf (ADR-004's
order of regard: Aesir first, then Ollama, then llama.cpp,
then generic OpenAI-compatible endpoints). The adapter lives in
`src/yggdrasil/ollama_adapter.py`; engines are wrapped, never
re-implemented — this module implements no model runner.*

## What it does

The adapter speaks Ollama's own HTTP API shapes and maps what
the engine's own surface reports into the contract shapes the
mesh already holds:

| Adapter call | Ollama surface | Contract shape out |
|---|---|---|
| `discover()` | `GET /api/tags`, `GET /api/version`, `POST /api/show` per model | validated Y-008 `EngineCapability` |
| `list_models()` | `GET /api/tags` | the server's own model names, in its order |
| `model_entries()` | tags + `POST /api/show` | validated Y-008 registry `ModelEntry`s |
| `generate(task, node_id)` | `POST /api/generate` | StreamEvents, then a terminal ResultEnvelope |
| `embed(task, node_id)` | `POST /api/embed` | a terminal ResultEnvelope |
| `run_task(task, node_id)` | generate/embed, collecting | `(ResultEnvelope, [StreamEvent])` — the simulator's return shape, from a real engine's answers |

Discovery is also the census's live truth: the node agent
(Y-021, grown at Y-023) accepts `census_engines=` /
`census_models=` constructor seams that feed Y-022's collector,
and the adapter offers `capability_for_census()` /
`model_names_for_census()` under exactly those names.

## Honest capability mapping

ADR-004's per-engine honesty rules, as this adapter applies
them:

- **`generate`** — only when the server is reachable AND at
  least one model is present. A reachable server with an empty
  shelf cannot generate anything on this node yet, and the
  census will not say it can.
- **`stream`** — follows `generate`: streaming generate
  (`POST /api/generate` with `stream: true`, NDJSON lines) is
  the path `generate()` actually drives.
- **`vision` / `embed`** — only when a present model's own
  `POST /api/show` capabilities list names them (`vision` /
  `embedding` in Ollama's vocabulary). A model *name* that
  sounds visual is never the evidence, and a failed show
  raises nothing.
- **`provenance_grade`** — stays `unproven` whatever discovery
  finds. Ollama keeps no proof ledger of its own to mirror,
  and ADR-004 forbids laundering a grade upward. Reachability
  is measured here; proven inference is not claimed.
- **Version** — from `GET /api/version` when the server
  publishes one; a server too old for that route is still
  discovered, with the field absent rather than guessed.

Model entries are built only from fields Ollama publishes
(family, parameter size, quantisation level and format, byte
size → `size_gb`). A tag with no family cannot become a
registry entry: its name lands in the adapter's
`skipped_models` ledger instead of being invented around —
while `list_models()` still reports it present, because
presence and registry-worthiness are different claims.
Per-model entry capabilities come from that model's own show
surface alone (plus `stream` where `generate` is claimed): an
embedding model is never dressed as a generator. `runnable_on`
stays empty — which device classes may run a quant is
quantisation fitness (Y-038), not this adapter's word.

## Streaming, told straight

`generate()` with `stream=True` yields a `started` event, one
`token` event per NDJSON chunk in the order the engine sent
them, and a `completed` event; the terminal ResultEnvelope
assembles the full text, carries provenance (node, engine,
model, and the quant the model's tag reported) and the
measured run time (an injectable timer — deterministic in
tests, a real monotonic clock in life).

With `stream=False` the engine is asked for the whole answer
at once and the only event is `completed` — ADR-004's rule:
the fabric never fakes a stream by chunking a finished answer.

Failures are named in the Y-007 taxonomy:

| Failure | Code |
|---|---|
| no answer at all (refused, timed out, gone) | `engine_unreachable` |
| HTTP 404 while asking for one model | `model_not_present` |
| other HTTP failure, malformed NDJSON line, response missing its owed fields | `engine_error` |
| task names no model | `model_not_present` (raised; `run_task` envelopes it) |
| task payload missing its prompt / texts | `malformed_envelope` (raised; enveloped likewise) |
| a kind this adapter cannot serve (e.g. `vision.*`) | refused, `no_capable_node` |

A stream that dies after chunks were delivered returns a
`failed` envelope with `partial: True` and the text the caller
already holds — streamed text is never taken back or hidden.

An unreachable server at *discovery* time is not an exception
at all: `discover()` returns `reachable: False` with every
capability False, because that is exactly the truth the census
needs to carry.

## The transport seam

This module carries **no network client**. Ollama is spoken
through an injected transport — three methods (`get_json`,
`post_json`, `post_lines`) returning parsed JSON or raw
response lines and raising the module's `OllamaUnreachable` /
`OllamaHttpError` — the same injectable-boundary pattern as the
census probes (Y-022) and the simulator handlers (Y-014), and
the reason the Part I gate's zero-network-imports check still
passes over Part II's first adapter. The NDJSON parsing,
request shaping, and error mapping are all in the adapter and
all tested; the socket-carrying transport itself is deployment
wiring at the seam.

Honesty about proof: **no test in this slice claims a live
Ollama server was spoken to.** The suite's fake transport
implements Ollama's wire shapes faithfully and proves the
mapping, streaming, and failure logic against them. Speaking
to a real server on real hardware belongs to the citizen
slices — the Pi (Y-033), the laptop (Y-034), and this VM
(Y-035) — each on its own device, recorded as its own proof.

## Not built here

The llama.cpp / OpenAI-compatible adapter (Y-025), hash-verified
model presence (the model store, Y-026 — `list_models()` here
is Ollama's word, not hash proof), model load/unload slots
(Y-027), bounded task execution (Y-029 — this adapter runs
what it is handed; the sandbox is its own slice), the adapter
conformance kit (Y-046 — the gate every adapter, this one
included, must pass before joining a mesh), and any fabric
carriage of results (Part III).
