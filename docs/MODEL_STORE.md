# The Model Store — Y-026

*Part II, sixth slice: on-node model files with hashes. A model
is "present" only when its hash verifies — the Aesir wave
discipline, as code, in `src/yggdrasil/model_store.py`.*

## Presence is a measurement

A filename is not presence. A directory listing is not
presence. An engine's shelf (Ollama's `list_models()` says what
Ollama *reports*) is not presence. Presence is a SHA-256
recomputed from the file's own bytes, at the moment presence is
asked about, matching the hash pinned when the file entered
the store.

Every presence question — `is_present()`, `present_models()`,
`model_entries()`, and a census built on the store — re-hashes
every registered file for that call. There is no cached
"verified" flag to go stale: the moment a file's bytes change,
the next question learns it.

## The store

A `ModelStore` is one directory (the root) on the node:

- `files/` — the model files the store holds, named
  `<model>--<quant>.<format>` on `add()` so two quants of one
  family never collide.
- `model_store_index.json` — the index, shaped by
  `contracts/schemas/model_store_index.schema.json`; each entry
  (`model_store_entry.schema.json`) pins name, path relative to
  the root, SHA-256, byte size, and the operator-declared
  registry facts (family, quant, format, capabilities) plus the
  registration time. The index is rewritten atomically (temp
  file + replace).

Declared facts describe the file; they are never evidence
about it. The hash is the evidence, and it is re-measured,
never trusted from the index.

## Operations

- `add(source, name=…, family=…, quant=…, format=…)` copies a
  file in, hashes **the copy** (the bytes the node actually
  holds), and registers it. Same name + same bytes is
  idempotent. Same name + different bytes is refused *before*
  any copy can touch the pinned file — a name may not be
  quietly re-pointed at other weights; unregister first.
  Empty files are refused: zero bytes is not a model.
- `register_existing(relative_path, …)` registers a file
  already under the root, hashing it in place.
- `verify(name)` / `verify_all()` report each model's true
  status: `verified`, `corrupt` (file present, bytes or size
  differ from the pin), or `missing` (no file at the pinned
  path). Corrupt and missing models are reported, never
  silently dropped from the report — and never counted as
  present.
- `present_models()` / `model_names_for_census()` — the
  verified names, in registration order; the callable the
  census collector (Y-022) and the node agent accept.
- `model_entries()` — validated Y-008 registry entries for
  verified models only. `runnable_on` stays empty: quantisation
  fitness is Y-038's enforcement, not the store's claim.
- `unregister(name)` removes the index entry only. The file
  stays on disk — unregistering is bookkeeping, not deletion,
  and the store does not pretend otherwise.

Index paths must stay under the root: a relative path escaping
via `..`, an absolute path, or a symlink out is refused at
registration and at index load. Hashing streams in 1 MiB
chunks; a model file is never loaded whole into memory.

## The agent's store

A node agent built with `model_store=` fills its census models
seam from the store by default: `agent.census()` lists exactly
the hash-verified models in `models_present`, and a tamper
shows up in the next `refresh_census()` as a `models_present`
change. An explicit `census_models=` still wins when an
operator hands one in.

## What the hash does not prove

Hashing proves the bytes are the bytes that were registered.
It does not prove the weights are good, safe, or truly the
model their name claims — the provenance of a source is the
operator's concern here, and the fetch duty's (Y-037) when a
node pulls a model from a peer or a named source; that duty
will verify against the same pins.

## Not built here

No loading or unloading of models into engine slots (Y-027 —
loaded is a different fact from present, and the agent's health
report still claims no loaded models), no fetching (Y-037), no
corrupt-model failure shape beyond this slice's status report
(Y-032 names the failure classes), and no hardware claim: the
suite's fixture files are SIMULATED byte strings with real
hashes — the hashing discipline is proven, no real model's
presence on any named device is claimed.
