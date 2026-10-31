# Part II Gate — Y-048

*Part II — The Node Agent: every device a citizen (Y-021 –
Y-048) — is complete. This document is the gate record: what
was checked, by what code, and the numbers that came back,
all re-measured from the tree at gate time by
`src/yggdrasil/part2_gate.py` (`check_part2_gate()`), whose
tests are `tests/test_part2_gate.py`.*

## The gate, in plain words

The trunk stands. Every slice of the node agent Part II
promised is present in the repo; the founding specification
is untouched; and the three citizens the roadmap names —
the Pi, the laptop, and the VM — each run the conformance
kit green against their engine and serve a real task through
it, inside this gate. Part III (Ratatoskr, the message
fabric) may now rise on this trunk.

## What was checked, and what came back

| Check | Result at the gate |
|---|---|
| Slices Y-021 – Y-047 present on their banked files | 27 slices, 110 deliverable files, all present and non-empty |
| Founding README untouched | md5 `89a37a4e18cab9a998e1751c32e7bfd5`, unchanged from campaign open |
| Contract schemas valid + versioned (Y-016) | 74 schemas |
| Registered data files valid (Y-016) | 10 data files (manifest, role catalog, threat register, provenance register, campaign file, four citizen profiles, quant fitness policy) |
| Licence & provenance (Y-018) | 17 pieces noticed, 0 vendored |
| Campaign mirror (Y-019) | 11 parts, 244 slices, no drift; next unbanked after Y-001 – Y-047 is Y-048 itself |
| CI discipline (Y-009) | manual-dispatch only, no automatic triggers |
| No Docker (standing law) | no Dockerfile / compose / dockerignore anywhere in the tree |
| No wire of our own | 41 source modules scanned by AST; no network or process client and no engine SDK imported — the adapters speak through injected transports (ADR-004), deployment wiring owns the wire |
| Pi citizen (Y-033 shape: Mimir, Pi 5, aarch64) | ready on its measured shape, heartbeat present; Ollama adapter conformant — 16 checks, 16 passed, 0 skipped; one queued task served through the adapter, completed, streamed text whole (`Hello, mesh`); 0 failure incidents |
| Laptop citizen (Y-034 shape: purplelongship, Ryzen 9 + RTX 2060) | ready, heartbeat present; Aesir adapter conformant — 16 checks, 15 passed, 1 skipped (embed: Aesir serves no embed route, stated not passed quietly); task completed through the adapter, text whole; 0 failure incidents |
| VM citizen (Y-035 shape: this VM, CPU-only) | ready, heartbeat present; llama.cpp adapter conformant — 16 checks, 16 passed, 0 skipped; task completed through the adapter, text whole; 0 failure incidents |

Each citizen at the gate is built by its real factory
(Y-033 – Y-035) on a freshly provisioned identity, loads a
hash-pinned fixture model through the lifecycle, and serves
its task from its own queue through the Y-029 sandbox's
adapter bridge — the same doors the citizen slices proved,
exercised together.

Acceptance at the gate: full suite green (count recorded in
CAMPAIGN_STATE.md), ruff clean.

## How the gate can fail — proven, not promised

The gate's tests damage scratch copies and wrong shapes and
watch it refuse: a missing deliverable fails naming its
slice (Y-047's soak document), an emptied deliverable fails
(Y-021's node agent document), one appended byte on the
README fails the md5 check, a VM citizen handed a
CUDA-bearing shape fails by name (that machine is another
citizen's), and an unknown gate engine fails by name before
any adapter is built.

## Honesty

The three citizens were run LIVE on their real machines in
their own slices — Mimir at Y-033, purplelongship at Y-034,
this VM at Y-035 — and those runs stand as recorded there.
This gate re-runs them in-tree from those same measured
facts, injected as census probes, with transports that are
test doubles speaking each engine's real wire shapes: every
run inside the gate is SIMULATED, and the conformance
reports say so. No live engine server is claimed here — no
Aesir service was active on the laptop at its citizen slice,
no Ollama server has been measured on Mimir, and this VM
hosts no engine at all. What the gate proves is that the
tree as it stands — citizens, adapters, kit — still fits
together green, on the shapes of the real machines. The
fabric that would let the citizens speak to each other is
Part III, and it does not exist yet.
