# Engine adapter II — Project Aesir (Y-024)

`src/yggdrasil/aesir_adapter.py` wraps the house's own engine —
first in ADR-004's order of regard — behind the node contract.
It speaks Aesir's **native** service API as it stands (Aesir's
own `docs/API_SUPPORT_MATRIX_V1.md`, protocol v1), not its
Ollama-shaped compatibility mode:

| Route | What the adapter takes from it |
|---|---|
| `GET /health/live` | Schema-v1 process liveness (`status: "ok"`) — reachability only; it makes no model claim and is treated as none. |
| `GET /health/ready` | Schema-v1 readiness with the **one resident model** (`model`, `digest`, `backend`, `profile`, `context`); 503 means reachable but not ready. Only the resident model may generate. |
| `GET /v1/models` | Catalog ids. Visibility is not runnability — the matrix says so, and the adapter repeats it wherever the list is surfaced. |
| `POST /v1/generate` | Native generation. Non-streamed: one JSON answer with service-phase `timings_ms`. Streamed: NDJSON text records plus exactly one terminal record — **a stream ending without its terminal record is a failure, never a success** (the matrix's rule, enforced in `run_task`, partial text kept). |

Generation options are mapped through the matrix's own
whitelist (`system`, `max_tokens`, `timeout_ms`, `temperature`,
`top_k`, `top_p`, `min_p`, `repeat_penalty`, `seed`) — the
server rejects unknown fields, so the adapter never forwards
one. A task naming a non-resident model fails as
`model_not_present` before any generation is attempted.
Run timings prefer Aesir's own service measurement
(prefill + decode) when the answer carries it, and fall back
to the adapter's timer when it does not.

## The ledger feeds the census — the slice's law

Aesir keeps a canonical capability ledger
(`CAPABILITY_LEDGER.md`): every capability carries exactly one
status — `verified`, `partial`, `scaffold`, `simulated`,
`missing`, or worse. `parse_ledger()` reads that markdown
shape into data (entry id → normalised status; compound
annotations read by their leading word, historical/withdrawn
statuses normalise to `other` — a status the mesh cannot name
is a status the mesh does not claim on).

Native generation and streaming are governed by one entry,
**AES-SRV-010** ("Authenticated native loopback CUDA
generation", `verified` at the time this adapter was written).
The rules binding `discover()` — and therefore the census
(Y-022) that carries its record through
`capability_for_census()`:

- a capability is claimed only when its governing entry is
  `verified` or `partial` **and** the live surface agrees
  (ready resident session). Neither word alone suffices;
- `provenance_grade` mirrors the entry — `verified` →
  `proven`, `partial` → `partial`, anything else → `unproven`
  — and is **never laundered upward** (ADR-004). Every
  result's provenance cites it:
  `verification: "aesir-ledger:AES-SRV-010=verified"`;
- `ledger_summary()` counts the snapshot's statuses, so the
  census and the operator's door can show the shape of
  Aesir's book as the node holds it.

## What is not claimed

- **No embeddings.** Aesir's native health surface exposes
  embeddings as explicitly absent and no native embed route
  exists. AES-RAG-003 (query embedding generation) is
  `partial` and engine-internal — a partial internal path is
  not a served endpoint. Embed tasks are refused by name.
- **No vision.** The native generate route rejects `images`
  outright; there is no image input to this API as it stands.
  Vision tasks are refused by name.
- **No registry entries from ids.** `/v1/models` publishes
  ids alone — no family, no quant, no size — so no Y-008
  `ModelEntry` can be honestly built; every listed model is
  recorded in `skipped_models` by name instead.
- **No live Aesir service was spoken to in this slice's
  tests.** The suite's fake transport speaks Aesir's wire
  shapes faithfully and proves the mapping, ledger gating,
  streaming, and failure logic against them. Speaking to a
  real server on real hardware belongs to the citizen
  slices — the laptop citizen (Y-034) is Aesir's GPU line's
  own proof, on its own device.
- **Authentication is the transport's.** The native service
  is bearer-authenticated from a private key file Aesir
  itself owns; the key never enters this module, and a
  refused key surfaces as `scope_denied`.

## Not built here

Hash-verified model presence has landed as its own slice (the model store, Y-026 — a
catalog listing is not hash proof, and Aesir's `model_digest`
is carried as provenance data, not yet verified against
on-disk bytes by this mesh), model load/unload slots
(Y-027), bounded task execution (Y-029), the adapter
conformance kit has landed as its own slice (Y-046 — the
gate every adapter, this one included, must pass; this
adapter passes it in the kit's own suite, over a fake
transport, SIMULATED), Aesir's Ollama-mode surface as a
separate citizen view (the Ollama adapter, Y-023, already
speaks that wire shape against Ollama itself), and any
fabric carriage of results (Part III).
