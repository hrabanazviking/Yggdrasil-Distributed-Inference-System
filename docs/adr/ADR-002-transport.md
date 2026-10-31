# ADR-002 — Transport: chosen by benchmark, contracts first — Y-003

**Status:** Accepted (criteria fixed; choice made at Y-049 —
minimal gossip; see the amendment at the end of this record).
**Roadmap decision:** §2.3. **Spec sketch:** MQTT + gossip + TLS (§3.5).

## Decision

The fabric's contracts (envelopes, registry, heartbeats — Y-006/Y-007,
Part III shapes) are written **transport-first**: no component imports
a transport directly. The transport itself — **MQTT vs NATS vs a
minimal signed gossip over TCP/QUIC** — is chosen by the Y-049
benchmark, run on Pi-class hardware with the mesh's real message
shapes.

## The benchmark criteria (fixed here, so the race is fair)

Candidates are scored on, in order:

1. **Correctness under partition** — registry convergence after a
   60 s split, zero lost terminal results.
2. **Latency** — median + p95 task-offer→claim and result delivery on
   a 3-node LAN, Pi included.
3. **Chatter cost** — idle bytes/node/minute at 3, 10, 30 nodes.
4. **Weight** — resident memory on a Pi-class node; install size;
   native (no Docker) service story.
5. **TLS + per-node identity** — first-class, not bolted on (Y-011).
6. **Operability by one human** — failure modes a single operator can
   diagnose at midnight.

A candidate failing criterion 1 is eliminated regardless of speed.
The result is recorded as an amendment to this ADR with the numbers
attached; the loser stays swappable via the Y-069 conformance kit.

## Amendment — Y-049 executed (2026-10-31): minimal gossip chosen

The benchmark ran (`src/yggdrasil/transport_benchmark.py`, report
shape `transport_benchmark_report` v1; full record in
`docs/TRANSPORT_BENCHMARK.md`). All three candidates raced in the
configurations the mesh would actually run — MQTT with QoS-1
persistent sessions, NATS with its durable JetStream store,
minimal gossip with a store-and-forward outbox — under the ADR's
own 60 s partition split, 100-task latency run, and idle chatter
counted at 3, 10, and 30 nodes:

- **Criterion 1 (partition): all three passed** — 0 of 12 terminal
  results lost, registry converged after the heal (gossip 5 s,
  NATS 10 s, MQTT 30 s — each on its own sync cycle). The gate is
  proven to bite: with persistence stripped, each candidate loses
  results across the split and is eliminated regardless of speed.
- **Latency (offer→claim median / p95, model):** gossip
  3.074 / 3.120 ms (one direct hop) · NATS 6.070 / 6.116 ms ·
  MQTT 8.069 / 8.115 ms (two brokered hops each).
- **Chatter (idle bytes/node/minute @ 3 / 10 / 30):** MQTT
  64 / 64 / 64 · NATS 288 / 288 / 288 · gossip 2,304 / 3,456 /
  3,456 (fanout-capped at 3 peers, flat past 10 nodes).
- **Weight (declared estimates):** gossip 0 MB install / 6 MB Pi
  RAM (no extra service) · MQTT 12 / 24 · NATS 16 / 40.
- **TLS + identity:** first-class for all three; gossip's is the
  mesh's own — every message signed with the node's Y-011 key.
- **Operability:** gossip alone adds no always-on service for the
  one operator to install, certificate, and restart.

**Decision: the fabric is built on the minimal signed gossip**
(Y-050), under the ADR's order — latency ahead of chatter, the
chatter cost accepted and recorded. MQTT and NATS remain
swappable via the Y-069 conformance kit. Honesty rider: the race
is SIMULATED — in-process models of each topology's persistence
rules over declared parameters; no broker was installed and no
LAN measured. The on-hardware re-run belongs to Part VIII and
may appeal this verdict with real numbers.
