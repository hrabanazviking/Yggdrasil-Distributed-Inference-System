# The Node Health Endpoint (Y-030)

`src/yggdrasil/health_endpoint.py` — the node's health as one
endpoint-shaped answer: liveness, load, models, and governor
state in the validated `node_health` v1 record
(`contracts/schemas/node_health.schema.json`). This is the
shape Part III's heartbeats carry: when Ratatoskr lands, a
heartbeat on the fabric is this record, transported — the
shape does not change, only the carrier does.

## What a heartbeat carries

- **The Y-006 health report itself** (`health`) — the same
  record `NodeAgent.health()` has answered since Y-021, so
  the endpoint and the agent can never tell two stories. The
  heartbeat's timestamp is the report's own: one moment,
  told once.
- **Liveness and lifecycle** — present while
  started/joined/draining, gone otherwise. A node only ever
  reports *itself* present or gone: `suspect` is in the
  shape because it is a **peer's** ageing verdict on a
  silent node (Y-054), and a self-report claiming it is
  refused by name at build time.
- **Load, counted and measured** — in-flight leases, queue
  depth (the local queue's true depth since Y-031 when one
  is wired; 0 before and unwired — no queue is claimed
  early), the sandbox's active tasks and finished
  executions, and the governor's latest readings (thermal,
  memory fraction, CPU share). Without a governor wired
  those readings are `null`: unknown, never a calm zero
  invented for the occasion.
- **Models** — `loaded` is the lifecycle's residency
  (Y-027) and must agree with the health report's
  `models_loaded`; `present` is re-hashed by the model store
  at ask time when a store is wired (Y-026 — tamper with a
  pinned file and it falls off the next beat), else the
  already-measured census's list, else `null` (unmeasured,
  not empty). `loader_simulated` carries the lifecycle's own
  honesty flag, or `null` with no lifecycle wired.
- **Governor state** — the governor's full validated
  verdict (Y-028) when one is wired, else `null`. Draining
  stays a lifecycle fact in this shape; it is not dressed up
  as a governor reason the governor never gave.
- **Census** — whether a census has been measured at all,
  when, and its timestamp-free fingerprint (Y-022). A
  heartbeat never *forces* a measurement — it reports the
  census the node already holds, so a cheap heartbeat stays
  cheap and a fingerprint change still names a real change.
- **Uptime** — seconds since the last successful start,
  measured on the agent's clock while the node is present;
  `null` before the first start and after a stop.
- **Sequence and honesty fields** — a per-endpoint monotonic
  `seq` (a receiver can spot a missed or replayed beat by
  number), `fabric_connected: false`, and
  `transport: "local-only"` — literal, until Part III.

Component records that disagree — a health report or
governor state naming another node, liveness at odds with
the lifecycle, a load or models section at odds with the
health report — are refused by name (`HealthError`, surfaced
by the agent as `NodeAgentError`). A heartbeat is assembled
from one node's truth or not at all.

## The local handler

There is deliberately **no socket** in this slice: the
endpoint is a local handler at the exact seam a transport
binds to later. `handle(method, path)` answers in-process:

| Request | Answer |
|---|---|
| `GET /health` | `(200, heartbeat)` while present; `(503, heartbeat)` while gone — gone is a report, not a silence |
| `GET /health/heartbeat` | the same, under the heartbeat's own name |
| any other path | `(404, {error: "not_found", known_paths: [...]})` |
| non-GET on a known path | `(405, {error: "method_not_allowed"})` |

A refused path or method serves no beat and consumes no
sequence number.

## Node agent

`NodeAgent.heartbeat()` serves one beat from the agent's own
wired components; `handle_health(method, path)` is the
handler above. Every beat is recorded as a `node.heartbeat`
Y-013 EventEnvelope in the agent's ledger — sequence,
liveness, lifecycle, counts, and the fabric honesty flag,
never payload content — and `status()` carries the
endpoint's beat count and last sequence number. The
`--check` preflight now prints a heartbeat sampled while
joined, alongside the status and health it already printed.

## Not built here

No network server, no fabric carriage, and no peer-side
liveness ageing (present → suspect → gone is Y-054, on
missed beats, judged by the watcher — never by the node
itself). No queue truth beyond the health report's own
depth (the queue itself is Y-031; see LOCAL_QUEUE.md), and
no hardware claim: the suite's snapshots are
fixtures and this VM's own agent; the citizens that run
this on named hardware are Y-033 – Y-035.
