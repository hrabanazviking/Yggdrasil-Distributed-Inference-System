# The Node Contract v1 — Y-006

*The interface every engine adapter (Part II) and every Norn
(Parts IV–VI) speaks. Written before code, per the roadmap's order.
Machine shapes live in `contracts/schemas/`; this document is the
meaning behind them.*

## Identity

A node is a `NodeIdentity` (`node_identity.schema.json`): a stable
`node_id`, a keypair whose public half is the node's name in the mesh
(private half issued under Y-011), a device class, and the nine-worlds
roles it currently wears (Y-012 — hats, not hardware).

## Capability census

A node publishes a `CapabilityCensus` (`capability_census.schema.json`):
**measured** CPU/RAM/accelerators/disk, the engines it can reach with
per-engine capabilities (`engine_capability.schema.json`), and the
models physically present (hash-verified per Y-026). Nothing in a
census may be assumed or copied from a spec sheet; the census carries
`measured_at` and goes stale honestly (Part III ages it out).

Per-engine honesty (ADR-004): each engine reports `provenance_grade`
— `proven`, `partial`, or `unproven` — mirroring the engine's own
ledger (e.g. Aesir's capability grades). The mesh never promises a
consumer more than the engine itself claims.

## Model lifecycle

Four verbs, spoken over the fabric (Part III) or locally:

- `model.ensure` — the model is (or becomes) present and hash-verified.
- `model.load` / `model.unload` — slot management under the node's
  VRAM/RAM budget (Y-027); swap costs are measured and reported.
- `model.list` — present ∪ loaded, with quants.

A `load` that cannot fit is refused with `queue_full`/`oom` semantics
from the error taxonomy — never a silent eviction of another caller's
warm model without a journal entry.

## Task execution

A node accepts a `TaskEnvelope` (Y-007) only when the census proves it
can run it (capability, model, quant floor). Execution is bounded
(time, tokens, output), cancellable, and streams `StreamEvent`s. The
terminal act is exactly one `ResultEnvelope` carrying full provenance
(task, node, engine, model, quant, timings).

## Health

A node emits `HealthReport` (`health_report.schema.json`) on the
fabric's heartbeat cadence: liveness, queue depth, loaded models,
governor state, clock skew. Part III ages reports present → suspect →
gone; no component keeps its own private liveness opinion.
