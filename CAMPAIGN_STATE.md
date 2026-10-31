# Yggdrasil — Campaign State

- **Campaign:** The Growing of the Tree (244 slices, 11 parts)
- **Roadmap:** `docs/YGGDRASIL_BUILD_ROADMAP_2026-10-30.md` (filed into
  this repo; also held at `~/workspace/your_files/yggdrasil/`)
- **Branch:** `yrsa/growing-the-tree` (own clone: `~/workspace/yggdrasil-build/`)
- **Opened:** 2026-10-30, at Volmarr's word ("draw up an advanced
  massive roadmap for this project and then begin working on it")
- **Push discipline:** the roadmap commit is pushed (ordered). Slice
  commits bank LOCALLY; further pushes await Volmarr's word.
- **Hard bounds:** no Docker; no README.md ever touched (spec README
  md5 `89a37a4e18cab9a998e1751c32e7bfd5` at campaign open); contracts
  before code; simulations labelled SIMULATED; hardware claims only
  from hardware runs.

## Position

- Banked: 14 / 244. In progress: Part I (next: Y-015 fake engines).

## Slice log

| Slice | State | Evidence |
|---|---|---|
| — | campaign opened | roadmap pushed @ `d9d7a2ce` (ls-remote verified == local) |
| Y-001 | BANKED | `docs/SPEC_CONCORDANCE.md`: every spec + WorldNet section mapped to a roadmap part or named deferred with reason |
| Y-002 | BANKED | ADR-001 language: Python first; compiled-path criteria (measured thresholds) recorded |
| Y-003 | BANKED | ADR-002 transport: six benchmark criteria fixed; choice runs at Y-049 |
| Y-004 | BANKED | ADR-003 state: CRDT vs single-writer log per store, merge rules |
| Y-005 | BANKED | ADR-004 engines: Aesir/Ollama/llama.cpp/OpenAI-compatible wrapped; per-engine honesty rules |
| Y-006 | BANKED | Node contract v1: 5 schemas + narrative; validated by Y-009 harness |
| Y-007 | BANKED | Task contract v1: envelopes + stream shape + 16-name error taxonomy; validated |
| Y-008 | BANKED | Registry contract v1: spec §4.2 model entry schema with graded measured speeds |
| Y-009 | BANKED | Scaffolding: package + pyproject + systemd unit as data + manual-dispatch CI; pytest 9 passed, ruff clean (venv) |
| Y-010 | BANKED | Threat model: `docs/THREAT_MODEL.md` + register `security/threat_register.json` (10 threats; roadmap's 4 named enemies = T-01–T-04) shaped by `threat_record.schema.json`; every mitigation mapped to its building slice, residual risks stated, no control claimed built; pytest 16 passed, ruff clean |
| Y-011 | BANKED | Identity foundation: pure-Python Ed25519 (RFC 8032, proven against the RFC's published test vectors fetched from rfc-editor.org; NOT constant-time, stated) in `src/yggdrasil/ed25519.py`; per-node keypairs + mesh-root-issued node certificates (`contracts/schemas/node_certificate.schema.json`, chain/fingerprint/window verification) in `src/yggdrasil/identity.py`; key custody behind a store interface — local store mode 0600, no overwrite, Kista detected-not-faked (integration = Y-196), custody reported honestly; `docs/IDENTITY.md` (revocation NOT built — T-01 residual stands); pytest 30 passed, ruff clean; README md5 unchanged |
| Y-012 | BANKED | Nine-worlds role model as data: `roles/role_catalog.json` — all nine role definitions (function quoted word-for-word from spec README §2.1, tested with dash variants normalised for the README's U+2011 typesetting; expected capabilities in one vocabulary = task-contract capabilities + mesh functions; criticality ceilings that bite — svartalfheim/alfheim/vanaheim/niflheim standard, asgard/midgard/jotunheim/nidavellir/muspelheim critical; `binds_hardware` const false in schema) shaped by `contracts/schemas/role_definition.schema.json`; operator assignments as data (`contracts/schemas/role_assignment.schema.json`) + `RoleBook` in `src/yggdrasil/roles.py` (assign supersedes, unassign recorded, history kept, effective ceiling = highest hat, hatless node handed nothing, `eligible()` predicate for Part IV; assignments project into Y-006 NodeIdentity, tested); `docs/ROLES.md` (enforcement NOT built — fabric carriage = Part III, router enforcement = Part IV); two in-slice defects found by tests and fixed (validators built without the schema registry → unresolvable roles $ref; spec-quote dash typesetting); pytest 45 passed, ruff clean; README md5 unchanged |
| Y-013 | BANKED | Observability contract: `contracts/observability_contract.md` — exactly three shapes, structured/local/no phone-home; LogRecord (`contracts/schemas/log_record.schema.json`: level + closed 10-component vocabulary + dotted event + scalar fields + `payload_logged` const false + Y-007 error_code on failures), MeterSample (`meter_sample.schema.json`: counter/gauge/histogram_summary, closed unit list, ≤8 labels), EventEnvelope (`event_envelope.schema.json`: Verdandi shape family — type/source/at/per-source seq/correlation/payload; plugin's exact emitted field names verified at Y-197 as a mapping, NOT claimed here); `src/yggdrasil/observability.py` (validators, record/event builders refusing payload+credential keys, `METERS` catalog as data — counters only rise, durations never negative, unknown meters refused — and `LocalJsonlSink`, transport literally `local-file`, no network path); `docs/OBSERVABILITY.md` (no component emits yet — emitters = Part II onward, dashboard = Y-202); pytest 58 passed, ruff clean; README md5 unchanged |
| Y-014 | BANKED | Loopback multi-node simulator: `src/yggdrasil/simulator.py` — in-process only (transport `loopback-simulated`, no sockets/threads/network paths), many fake nodes on one machine (spawn; acceptance runs 30), each a validated Y-006 NodeIdentity + CapabilityCensus fixture pair (fixture keys NOT cryptographic — real keys = Y-011) with an injectable task handler (the Y-015 plug point; default plain SIMULATED echo); registry snapshot + peer discovery views as validated data; stopped node reports `gone`, leaves peer views, targeted tasks unreachable (`partition_unreachable`); tasks travel end to end as validated Y-007 StreamEvents + exactly one terminal ResultEnvelope with provenance, placed by a deterministic first-capable-by-node_id stand-in (explicitly NOT Verðandi routing — Part IV); targeted incapable node returns `refused` naming `model_not_present`/`no_capable_node`, handler `SimulatedTaskFailure` becomes a `failed` result with its Y-007 code, and no-capable-node raises instead of forging provenance; virtual clock (fixed epoch, advances by hand or declared simulated latency only) makes runs deterministic — identical placement/timings/timestamps across fresh simulators, tested; joins/stops/outcomes recorded as Y-013 EventEnvelopes from the `simulator` component, no payload content; `docs/SIMULATOR.md` (no transport/TLS/gossip, queue depth reported 0, no resource model — fabric = Part III, governors = Part II); pytest 71 passed, ruff clean; README md5 unchanged |
