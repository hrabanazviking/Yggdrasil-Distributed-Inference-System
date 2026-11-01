# The Transport Benchmark — Y-049

ADR-002 (Y-003) fixed the race before any code ran: the fabric's
transport — MQTT vs NATS vs a minimal signed gossip — is chosen by a
benchmark scored on six criteria **in the ADR's order**, and a
candidate failing criterion 1 (correctness under partition) is
eliminated regardless of speed. This slice runs that race:
`src/yggdrasil/transport_benchmark.py` (`TransportBenchmark`,
`run_benchmark`, `require_transport`) answers a validated
`transport_benchmark_report` v1, and the decision is recorded as an
amendment to ADR-002 with the numbers attached.

## How the race is run

- **Candidates are data** (`transport_candidate` v1), each in the
  configuration the mesh would actually run: MQTT with QoS-1
  persistent sessions, NATS with its durable JetStream store, and
  the minimal gossip with a store-and-forward outbox. A profile
  carries its topology, its persistence rule, the hop arithmetic of
  its latency, the shape of its idle sync traffic, its identity
  story, its failure modes, and its weight — install size and
  Pi-class resident memory **declared** in the profile and carried
  through the report under names that say `_declared`: estimates
  recorded as estimates, never dressed as measurements.
- **Criterion 1 is run, not asserted.** A three-node mesh is split
  for the policy's 60 s (the ADR's own split): node 2 is cut off
  from nodes 0 and 1, and a broker, where the candidate has one,
  sits on node 0 — the hard side of the split. During the split
  every node registers with every other and produces four terminal
  results, addressed round-robin across and within the split. A
  persistent candidate queues what it cannot deliver (the sender's
  outbox, or the broker's queue for an offline subscriber); on the
  heal every queue flushes and the run checks the ADR's two
  demands: **zero lost terminal results** and **registry
  convergence** (every node holds every registration). Convergence
  completes on the candidate's next sync cycle after the heal.
- **Latency is the model's hop arithmetic** (`transport_benchmark_policy`
  v1: 100 tasks on the 3-node LAN shape): hops × per-hop latency +
  serialising (header + payload) at a Pi-class 100 Mbit/s link
  rate, payload sizes cycling deterministically; median and p95
  computed from the per-task numbers.
- **Chatter is the model's own sync traffic, counted**: idle bytes
  per node per minute at 3, 10, and 30 nodes — a broker node's
  keepalive ping + ack with its broker; a gossip node's
  anti-entropy digests to its fanout of peers, sent and answered.
  The fanout cap (3) is why gossip chatter is flat from 10 to 30
  nodes instead of growing with the mesh.
- **The decision follows the ADR's order**: criterion 1
  eliminates; among survivors, lowest combined p95 (offer→claim +
  result) wins, ties broken by chatter at the largest mesh, then
  declared resident memory, then declared install size.

## The record (default policy, this build)

| Candidate | Crit. 1 | Lost results | Converges after heal | Offer→claim median / p95 | Chatter B/node/min @ 3 / 10 / 30 | Declared install / Pi RAM |
|---|---|---|---|---|---|---|
| MQTT (persistent sessions) | pass | 0 of 12 | 30 s (keepalive cycle) | 8.069 / 8.115 ms | 64 / 64 / 64 | 12 MB / 24 MB |
| NATS (JetStream) | pass | 0 of 12 | 10 s (keepalive cycle) | 6.070 / 6.116 ms | 288 / 288 / 288 | 16 MB / 40 MB |
| **Minimal gossip (store-and-forward)** | pass | 0 of 12 | 5 s (anti-entropy round) | **3.074 / 3.120 ms** | 2,304 / 3,456 / 3,456 | **0 MB / 6 MB** |

**Chosen: minimal gossip** — one direct hop beats two brokered
hops on latency; it is the lightest candidate by an order of
magnitude and the only one with no extra always-on service for one
operator to install, certificate, and restart at midnight. It pays
for this in idle chatter (the highest of the three, though
fanout-capped and flat past 10 nodes) — the ADR's order puts
latency ahead of chatter, and the record says so plainly. The
elimination gate is proven, not decorative: the suite strips each
candidate's persistence in turn and the run loses terminal results
across the split and eliminates it by name, even when the stripped
candidate is made the fastest on the field.

## Honesty

Everything here is **SIMULATED** — the report carries
`simulated: true` as a schema constant. No MQTT or NATS broker was
installed or run, no socket was opened, and no LAN was measured:
the candidates race as faithful in-process models of their
topologies and persistence rules, driven by declared parameters.
The real-broker, on-hardware race — brokers installed natively on
Mimir and purplelongship under the citizen suites — belongs to
Part VIII. The losers stay swappable through the Y-069 fabric
conformance kit, exactly as ADR-002 promised.

## Not built here

- The fabric itself — publish/subscribe and direct channels over
  the chosen gossip transport — is built at Y-050
  (`docs/FABRIC.md`): the in-process core of this slice's winning
  candidate, store-and-forward outbox and all.
- TLS carriage of the gossip channels is Y-051; discovery Y-052;
  gossip of registry/health state as a service is built at Y-055
  (`docs/GOSSIP.md`) — the service's full anti-entropy rounds,
  where this slice's was the benchmark's miniature.
- No candidate's broker was installed: MQTT and NATS remain
  recorded configurations, swappable at Y-069, not deployments.
