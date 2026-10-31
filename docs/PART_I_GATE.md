# Part I Gate — Y-020

*Part I — Roots: decisions, contracts, scaffolding (Y-001 – Y-020) —
is complete. This document is the gate record: what was checked,
by what code, and the numbers that came back, all re-measured
from the tree at gate time by `src/yggdrasil/part1_gate.py`
(`check_part1_gate()`), whose tests are `tests/test_part1_gate.py`.*

## The gate, in plain words

The roots hold. Every contract, decision record, and piece of
scaffolding Part I promised is present in the repo and consistent
with every other piece; the founding specification is untouched;
the simulator skeleton runs; and there is no inference code in
the tree yet — by design. Part II (the node agent) may now rise
on these roots.

## What was checked, and what came back

| Check | Result at the gate |
|---|---|
| Slices Y-001 – Y-019 present on their banked files | 19 slices, 61 deliverable files, all present and non-empty |
| Founding README untouched | md5 `89a37a4e18cab9a998e1751c32e7bfd5`, unchanged from campaign open |
| Concordance published in-repo (Y-001) | `docs/SPEC_CONCORDANCE.md`, 34 mapping rows, deferrals named with reasons |
| Contract schemas valid + versioned (Y-009/Y-016) | 24 schemas |
| Registered data files valid (Y-016) | 5 data files (manifest, role catalog, threat register, provenance register, campaign file) |
| Licence & provenance (Y-018) | 17 pieces noticed, 0 vendored |
| Campaign mirror (Y-019) | 11 parts, 244 slices, no drift; next unbanked after Y-001 – Y-019 is Y-020 itself |
| CI discipline (Y-009) | manual-dispatch only, no automatic triggers |
| No Docker (standing law) | no Dockerfile / compose / dockerignore anywhere in the tree |
| Simulator skeleton (Y-014/Y-015) | 3 fake nodes spawned; one task completed end to end as a SIMULATED result with provenance, on the loopback-simulated transport, no network access |
| Zero inference code | 13 source modules scanned by AST; no engine SDK, model loader, or network/process client imported; the only engine-shaped code is the fake engines, still declaring `simulated` |

Acceptance at the gate: full suite 143 passed, ruff clean.

## How the gate can fail — proven, not promised

The gate's tests damage scratch copies of the tree and watch it
refuse: a missing deliverable fails naming its slice (Y-003's
ADR), an emptied deliverable fails (Y-014's simulator doc), one
appended byte on the README fails the md5 check, a module
importing `torch` or `openai` fails naming the import (while the
same words in a comment or a string — data, not code — pass), and
a stray `Dockerfile` fails on the standing no-Docker law.

## Honesty

This gate is build-time paperwork over the repo as it stands,
like Y-016/Y-018/Y-019. It proves the roots are present,
consistent, and free of inference code. It proves nothing about
hardware: the simulator run inside it is SIMULATED, no engine
adapter exists yet (Part II, under ADR-004), no fabric exists
yet (Part III), and the mesh has never run on a device. Those
claims wait for their parts, and their hardware.
