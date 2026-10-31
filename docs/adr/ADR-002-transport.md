# ADR-002 — Transport: chosen by benchmark, contracts first — Y-003

**Status:** Accepted (criteria fixed; choice pending Part III, Y-049).
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
