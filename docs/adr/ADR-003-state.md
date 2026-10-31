# ADR-003 — State: CRDT where the spec demands, logs where it doesn't — Y-004

**Status:** Accepted, 2026-10-30. **Roadmap decision:** §2.4.

## Decision

| Store | Mechanism | Why |
|---|---|---|
| Mesh membership + model registry | **CRDT** (LWW-element-set per node record; observed-remove for models) | Many writers, partition-prone; the spec §3.5/§4.2 demands convergence without a registrar. |
| Urðr decision ancestry + outcome ledger | **Single-writer append log per node, merged by (node, seq)** | Facts are authored by exactly one node; appends never conflict; a total order per author is enough (Y-102/Y-103). |
| Urðr contexts + knowledge collections | **Versioned objects, single-writer per collection** (owner node), CRDT merge only for the household-shared collection | Collections have owners (Y-106/Y-117); version conflicts surface to the owner (Y-112), never silently resolved. |
| Task lifecycle journal | **Single-writer log** (the hosting Verðandi role-holder), replicated read-only | One role-holder mutates a task's state (Y-075); failover hands the log over (Y-089). |
| Node health | **No store** — latest-report-wins over the fabric, aged by heartbeat rules | Health is a stream, not a history (Y-054). |

## Merge rules (for the CRDT stores)

- Every record carries `(node_id, lamport, wall_time)`; ties break by
  `node_id` — deterministic on every replica.
- Deletes are tombstones retained for one registry epoch; a node gone
  past the epoch is pruned by Y-180's retirement, not by gossip whim.
- Merge is idempotent and order-insensitive; Y-056 property-tests
  both before the stores are trusted.
