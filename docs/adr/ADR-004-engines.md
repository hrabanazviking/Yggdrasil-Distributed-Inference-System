# ADR-004 — Inference engines: wrapped, never re-implemented — Y-005

**Status:** Accepted, 2026-10-30. **Roadmap decision:** §2.1.

## Decision

Yggdrasil implements **no model runner**. Each node wraps the engines
that already exist behind the node contract (Y-006), in this order of
regard:

1. **Project Aesir** — the house's own engine; where a device runs
   Aesir, it is the node's first citizen.
2. **Ollama** — the common local shelf.
3. **llama.cpp server** — the bare-metal fallback.
4. **Generic OpenAI-compatible local endpoints** — anything on the
   LAN that speaks the shape, labelled as what it is.

## Honesty rules (binding on every adapter, enforced by Y-046)

- Capabilities are reported **per engine** from the engine's own
  surface (`engine_capability.schema.json`), including a
  `provenance_grade` that mirrors the engine's own ledger. Aesir's
  `partial` stays `partial` in the mesh; the mesh never launders a
  grade upward.
- An engine that cannot stream says so; the fabric never fakes a
  stream by chunking a finished answer.
- Adapter conformance (Y-046) is a gate: an adapter that fails the
  kit does not join the mesh, however promising.
