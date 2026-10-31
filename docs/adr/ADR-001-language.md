# ADR-001 — Language: Python control plane first — Y-002

**Status:** Accepted, 2026-10-30. **Roadmap decision:** §2.2.

## Decision

The node agent, the fabric, and the three Norns are written in
**Python (asyncio)** first. The spec's pseudocode (§3) sketches
Rust/Tokio; that sketch is honoured as *behaviour*, not obeyed as a
starting language.

## Why

- The workshop's living code is Python (Samtal, Hermes plugins,
  Heimdall); one language means one keeper can tend every tree.
- The mesh is orchestration: its costs are network waits and engine
  waits, not interpreter microseconds — until measurement says
  otherwise.
- A one-human workshop pays for every language it must hold in mind.

## When a compiled component earns its place (the criteria)

A hot path is re-implemented in Mojo (first choice) or Rust/Go only
when **all** of these hold, measured by Part XI's harness on named
hardware:

1. The path consumes > 20% of a node's CPU during a representative
   workload while engines idle-wait < 50% of the time, **or** adds
   > 5 ms median latency per routed task on Pi-class hardware.
2. The Python profile (cProfile/py-spy evidence attached to the ADR
   amendment) names the same path twice, in two separate campaigns.
3. The replacement keeps the contracts byte-identical (conformance
   kit Y-046/Y-069 passes unchanged).

Until then, compiled rewrites are refused as premature — recorded
here so the refusal is a decision, not drift.
