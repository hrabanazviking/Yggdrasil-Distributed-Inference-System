# Node Conformance Kit — the gate every engine adapter must pass (Y-046)

ADR-004's gate, made a test pack. Until this slice, each
adapter (Ollama Y-023, Aesir Y-024, llama.cpp /
OpenAI-compatible Y-025) carried its own acceptance suite,
and a new adapter had nothing but those suites to copy from.
This slice (`src/yggdrasil/conformance.py`) is the single
bar: `ConformanceKit` examines any candidate adapter before
a node may host it, and `require_conformant()` is the
joining gate in one call — an adapter that fails the kit
does not join the mesh, however promising.

## How a run goes

The caller builds the adapter the way deployment would
(transport wiring is the caller's, as in Y-023 – Y-025) and
names what the kit may exercise:

- `generate_model` — a listed model a generate task may run;
- `embed_model` — likewise for an embed task;
- `unreachable_adapter` — the same adapter built over a dead
  transport, for the checks that need a corpse;
- `simulated` — the caller's declaration about the wiring
  (see Honesty, below).

A check the naming does not reach is `skipped` in the
report, with the reason in its detail — a stated absence,
never a quiet pass. An adapter that raises where the
contract says return, or returns a record that fails its
schema, fails that check with the exception named; the kit
itself raises only on its own misuse.

## The sixteen checks

**Contract** — `interface_surface` (the adapter's `engine`
is one of the contract's names and all six contract methods
are callable), `discovery_shape` (a validated Y-008
EngineCapability naming the adapter's own engine),
`discovery_repeatable`, `model_list_shape`,
`model_entries_honest` (every registry entry is listed,
schema-valid, and claims no capability discovery itself
does not claim; `skipped_models`, where an adapter keeps
one, names only listed models it did not build),
`embed_completes` (one vector per input, where an embed
model is named and claimed).

**Census honesty** — `census_seams_agree`
(`capability_for_census()` / `model_names_for_census()`
answer exactly what `discover()` / `list_models()` answer),
`stream_follows_generate` (stream claimed without generate
fails — streaming is generate's mode, not a decoration; an
engine that claims generate *without* stream passes: it
says it cannot stream, and says so, per ADR-004),
`empty_shelf_claims_nothing`, `unreachable_claims_nothing`
(a dead engine returns `reachable: false` as a finding,
never raises, and claims nothing).

**Streaming** — `generate_completes` (a validated
ResultEnvelope whose provenance is this node, this engine,
this model), `stream_shape_and_text` (StreamEvents
validated, sequenced from 0 without a gap, opening and
closing by the contract, and the token events' text *is*
the result's text), `no_fake_stream` (`generate` with
`stream=False` yields exactly one `completed` event and no
token events — a finished answer is never re-chunked).

**Errors** — `unknown_model_named` (a task naming a model
no surface lists ends failed or refused with a Y-007 code,
never completed), `unservable_kind_refused` (refused
`no_capable_node`), `unreachable_task_named` (a task on the
dead engine ends by name, never completed, never raised).

## The report

`run()` answers the validated `conformance_report` v1:
every check in the kit's fixed order, passed / failed /
skipped counted apart, and `verdict` — `conformant` only
when no check failed. Each completed run records one Y-013
event (names and counts only).

## Acceptance

The in-tree suite drives the kit over all three real
adapters — four engine names — on fake transports speaking
their own wire shapes: all conformant (Ollama and both
OpenAI-compatible flavours 16/16; Aesir with the embed check
an honest skip, its native surface serving no embed route).
A minimal honest stub adapter also passes, and a stub broken
one named way at a time — stream without generate, invented
registry entries, a re-chunked fake stream, stream text
disagreeing with its result, an unknown model that
"completes", an unservable kind served, claims on a dead
engine, a missing contract method — fails exactly the check
that names the break, and `require_conformant` refuses it
naming that check.

## Not built here / honesty

- The kit is a **pre-join gate** run at deployment and test
  time. No node agent seam grows at this slice and no
  citizen factory is rewired: an adapter joins when its
  operator runs the kit green, and the report is the
  evidence. Wiring the kit into a factory is deployment
  policy, not a contract change.
- `simulated` is the caller's declaration, carried verbatim:
  the kit runs against whatever transport the adapter was
  built with and cannot see past that seam. The in-tree runs
  are over fake transports and are declared
  `simulated: true` — no live Aesir, Ollama, or llama.cpp
  server is claimed here; on-hardware proof belongs to the
  citizen slices and Part VIII.
- The kit judges the contract surface the adapters share
  today. A future adapter kind with a genuinely new door
  (audio, say) earns its own check in a later slice; until
  then an unclaimed capability is simply not exercised,
  and a named-but-unclaimed one is a stated skip.
- The mixed-engine soak (one node, 1,000 tasks) landed
  in Y-047 (`docs/SOAK.md`); the Part II gate landed at
  Y-048 (`docs/PART_II_GATE.md`), running this kit green
  for all three citizens against their engines.
