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

- Banked: 10 / 244. In progress: Part I (next: Y-011 identity foundation).

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
