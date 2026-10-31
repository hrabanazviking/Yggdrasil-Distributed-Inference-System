# Engine Adapter III — llama.cpp + OpenAI-compatible — Y-025

*Part II, fifth slice: the last two citizens of ADR-004's
order of regard wrapped behind the node contract, in
`src/yggdrasil/openai_compat_adapter.py`. Engines are wrapped,
never re-implemented — this module implements no model runner.*

ADR-004's order of regard ends with two flavours that speak
one wire shape:

3. **llama.cpp server** (`llama-server`) — the bare-metal
   fallback. Engine name `llamacpp`
   (`LlamaCppAdapter`, default base `http://127.0.0.1:8080`).
   The shared shape plus its own `GET /health`.
4. **Generic OpenAI-compatible local endpoints** — anything
   on the LAN that speaks the shape, labelled as what it is.
   Engine name `openai-compatible`
   (`OpenAICompatibleAdapter`, default base
   `http://127.0.0.1:8000`). No vendor is inferred from an
   answer, and no native route beyond the shared shape is
   ever called.

## What it does

| Adapter call | Endpoint surface | Contract shape out |
|---|---|---|
| `discover()` | `GET /v1/models` (llama.cpp: `GET /health` read alongside) | validated Y-008 `EngineCapability` |
| `list_models()` | `GET /v1/models` | the endpoint's own model ids, in its order |
| `model_entries()` | the models answer | validated Y-008 registry `ModelEntry`s |
| `generate(task, node_id)` | `POST /v1/chat/completions` | StreamEvents, then a terminal ResultEnvelope |
| `embed(task, node_id)` | `POST /v1/embeddings` | a terminal ResultEnvelope |
| `run_task(task, node_id)` | generate/embed, collecting | `(ResultEnvelope, [StreamEvent])` — the simulator's return shape |
| `health_status()` (llama.cpp only) | `GET /health` | the health word (`"ok"`), or None |

Discovery is also the census's live truth: the node agent
(Y-021/Y-022) accepts `census_engines=` / `census_models=`
constructor seams, and both flavours offer
`capability_for_census()` / `model_names_for_census()` under
exactly those names — two flavours can ride the census side
by side, each under its own engine name.

## Honest capability mapping

ADR-004's per-engine honesty rules, as this adapter applies
them:

- **`generate`** — only when the endpoint is reachable AND
  at least one model is listed. A reachable endpoint with an
  empty list cannot generate anything on this node yet.
- **`stream`** — follows `generate`: SSE streaming
  (`POST /v1/chat/completions` with `stream: true`, `data:`
  lines closed by `data: [DONE]`) is the path `generate()`
  actually drives.
- **`embed` / `vision`** — **never inferred.** The
  `/v1/models` shape publishes no embedding or vision
  evidence: llama.cpp serves embeddings only under its
  `--embedding` flag and vision only with a multimodal
  projector loaded, a generic endpoint's flags are its own,
  and a model *name* that sounds visual or embed-shaped
  proves nothing (Y-023's rule). These capabilities are
  claimed only for models the **operator declares** at the
  seam — the `embedding_models=` / `vision_models=`
  constructor arguments, deployment wiring like the
  transport itself, from the operator who started the
  server and knows its flags — and only while a declared
  model is actually listed. The default declares nothing
  and claims nothing; a declaration about an absent model
  claims nothing. Execution obeys the same rule: an embed
  task for an undeclared model, a vision task for an
  undeclared model, and image input on an undeclared model
  are all refused by name (`no_capable_node`) — an unclaimed
  capability served anyway would be a lie the census never
  told.
- **`provenance_grade`** — stays `unproven` whatever
  discovery finds. Neither flavour keeps a proof ledger of
  its own to mirror, and ADR-004 forbids laundering a grade
  upward.
- **Version** — absent. The shape publishes no server
  version, and none is guessed.
- **llama.cpp `/health`** — advisory colour only. A server
  still loading its model answers 503 in its own shape;
  `health_status()` records that (the HTTP status is kept in
  `last_health_status`) and discovery's evidence stays the
  `/v1/models` surface, so a loading health word never
  dresses up or tears down a capability claim.

Model entries are built only from fields the endpoint
publishes (family, a quant's name/format/byte size,
parameter count, and llama.cpp's `meta.n_ctx_train` as
context). The plain shape publishes ids alone, so most listed
models cannot become entries: an id with no family/quant —
or one that cannot be a registry name at all, like a raw
file path with spaces and capitals — lands in the adapter's
`skipped_models` ledger instead of being invented around,
while `list_models()` still reports it present, because
presence and registry-worthiness are different claims.
Per-model entry capabilities come from the operator's
declarations alone: a declared embedding model claims
`embed` and is never dressed as a generator. `runnable_on`
stays empty — which device classes may run a quant is
quantisation fitness (Y-038), not this adapter's word.

## The chat shape, told straight

`generate()` speaks `POST /v1/chat/completions`:

- `payload.messages` (a list of role/content dicts) travels
  **verbatim** — the caller owns its conversation.
- Otherwise `payload.prompt` becomes the user turn, with
  `payload.system` (or a `system` option) as the system turn
  before it. On a declared vision model, `payload.images`
  (URLs or data URIs) become `image_url` content parts of
  that user turn.
- Sampling options are forwarded through a whitelist only
  (`temperature`, `top_p`, `max_tokens`, `seed`, `stop`,
  `frequency_penalty`, `presence_penalty`) — some servers
  reject unknown fields outright, and the adapter never
  manufactures an `engine_error` out of its own carelessness.

With `stream=True`, SSE `data:` chunks become `token`
StreamEvents in wire order (both chat `delta.content` and
completions-shaped `text` chunks are read), a usage-only
closing chunk contributes its usage to the result, and the
stream **must** close with `data: [DONE]` — a stream ending
without its terminator fails as `engine_error`: incomplete
is not success. With `stream=False` the only event is
`completed` — ADR-004's rule: a finished answer is never
re-chunked into a fake stream.

Failures are named in the Y-007 taxonomy:

| Failure | Code |
|---|---|
| no answer at all (refused, timed out, gone) | `engine_unreachable` |
| HTTP 401 / 403 (a refused key) | `scope_denied` |
| HTTP 404 while asking for one model | `model_not_present` |
| HTTP 429 (the endpoint's own backpressure) | `queue_full` |
| other HTTP failure, malformed SSE line, error chunk, answer missing its owed fields, stream without `[DONE]` | `engine_error` |
| task names no model | `model_not_present` (raised; `run_task` envelopes it) |
| task payload missing its prompt / messages / texts | `malformed_envelope` (raised; enveloped likewise) |
| embed/vision asked of an undeclared model; a kind this adapter cannot serve | refused, `no_capable_node` |

A stream that dies after chunks were delivered returns a
`failed` envelope with `partial: True` and the text the
caller already holds — streamed text is never taken back or
hidden. Embedding vectors are ordered by the answer's own
`index` fields, never by arrival order (endpoints do answer
out of order, and arrival order is not a contract).

An unreachable endpoint at *discovery* time is not an
exception at all: `discover()` returns `reachable: False`
with every capability False, because that is exactly the
truth the census needs to carry.

## The transport seam

This module carries **no network client**. The endpoint is
spoken through an injected transport — three methods
(`get_json`, `post_json`, `post_lines`) returning parsed JSON
or raw response lines (the SSE lines, verbatim) and raising
the module's `OpenAICompatUnreachable` /
`OpenAICompatHttpError` — the same injectable-boundary pattern
as Y-023/Y-024, and the reason the Part I gate's
zero-network-imports check still passes over Part II's third
adapter. An API key, where a generic endpoint demands one,
lives in the transport; the adapter never sees it.

Honesty about proof: **no test in this slice claims a live
llama.cpp server or a live generic endpoint was spoken to.**
The suite's fake transports implement the wire shapes
faithfully (including llama.cpp's meta-bearing models answer
and a loading server's 503 health) and prove the mapping,
streaming, declaration gates, and failure logic against them.
Speaking to a real server on real hardware belongs to the
citizen slices — the Pi (Y-033), the laptop (Y-034), and this
VM (Y-035) — each on its own device, recorded as its own proof.

## Not built here

Hash-verified model presence has landed as its own slice (the model store, Y-026 —
`list_models()` here is the endpoint's word, not hash proof),
model load/unload slots (Y-027 — llama.cpp's own `/slots`
and load/unload routes are not driven here), bounded task
execution (Y-029 — this adapter runs what it is handed; the
sandbox is its own slice), the legacy `/v1/completions`
route as a driven path (chat completions is the common
denominator both flavours serve; completions-shaped *chunks
and choices* are read where they arrive, but the route is
not driven), llama.cpp's native `/completion`, `/props`,
and `/embedding` routes (the OpenAI-compatible shape is the
contract this adapter wraps; native-only surfaces are a
future slice's word, if ever), the adapter conformance kit
(Y-046 — the gate every adapter, this one included, must
pass before joining a mesh), and any fabric carriage of
results (Part III).
