# Gossip of state — registry and health, epidemically (Y-055)

Part III's sixth slice, in `src/yggdrasil/gossip.py`.
The roadmap's sentence: *registry and health propagate
epidemically; convergence time measured in the
simulator at 3, 10, and 30 nodes.* Until this slice the
registry spread by pairwise sync a caller arranged
(Y-053) and heartbeats were judged by whichever tracker
happened to hear them (Y-054). This slice is the
epidemic carriage between those stores: anti-entropy
rounds in which every node exchanges state with a
small, fanout-limited set of peers, and the whole mesh
comes to hold one truth without any node ever talking
to everyone.

## The epidemic

A `GossipMesh` holds `GossipNode`s — each one a Y-053
registry replica, a Y-054 liveness tracker, and a
forwarding store keeping the newest heartbeat it has
seen for every subject. In each round every node, in
sorted order, initiates push-pull exchanges with its
peers for that round: the `fanout` peers following it
in the sorted ring of node ids, the starting point
rotating by fanout places each round — deterministic,
so runs replay exactly, and bounded, so no node's
round ever costs the mesh more than the policy allows.

One exchange is two messages (one each way) and moves
both kinds of state at once:

- **Registry** state travels by Y-053's own
  `sync_with` — entries and withdrawal tombstones
  merged by the receiving replica's rules, not by any
  parallel logic here.
- **Health** state travels as validated Y-030
  heartbeats: each side offers every beat in its
  forwarding store, and the receiver stores a strictly
  newer sequence and feeds it to its own Y-054 tracker,
  whose freshness rules judge it.

Because a beat learned second-hand enters the
forwarding store, it is gossiped onward in later
rounds — state spreads node to node to node, which is
the epidemic the roadmap names.

## The policy is data

Gossip is judged against a validated `gossip_policy`
v1: the fanout bound (default 3 peers per round), the
virtual seconds one round stands for (default 5 — the
anti-entropy round Y-049's benchmark measured for the
chosen gossip), and the mesh's node bound (default
256). The report's `max_fanout_used` is the largest
number of exchanges any one node initiated in any one
round; it never exceeds the policy's fanout, and the
suite asserts it.

## Convergence, measured at 3, 10, and 30 nodes

A mesh is **converged** when every node holds the same
registry entries and tombstones, the same newest
heartbeat for every subject, and a liveness standing
for every other subject at that beat's sequence.
`measure_convergence()` runs that test in the
simulator at the roadmap's own sizes — a fresh mesh
per size, every node registering itself and publishing
its own heartbeat at the origin moment, then rounds
until convergence — and answers validated
`gossip_convergence` v1 records. Measured in this
slice's suite (SIMULATED, virtual clock):

| Nodes | Rounds | Virtual time | Exchanges | Messages |
|---|---|---|---|---|
| 3 | 1 | 5 s | 6 | 12 |
| 10 | 2 | 10 s | 60 | 120 |
| 30 | 2 | 10 s | 180 | 360 |

These are the model's numbers on its declared
parameters — rounds × the policy's round interval —
not measurements of a real LAN, and the records say so
(`simulated: true` is a schema constant).

## Partition, heal, and harmless repetition

A partition is modelled in the open, as in the fabric
(Y-050): `cut(nodes)` splits the named nodes away,
exchanges across the seam are refused
`partitioned_peer` (counted in the report and recorded
as `gossip.exchange_refused` events), and a fact
originating on one side does not reach the other.
`heal()` reunites the mesh; ordinary rounds from there
converge the two sides, proven in the suite for both
registry entries and heartbeats.

Repetition is the epidemic's normal weather, and it is
harmless by the receiving stores' own rules:

- a **duplicate** beat (same sequence) is counted apart
  in `beats_duplicate` and refreshes nothing — a
  replayed recording cannot keep a dead peer present
  (Y-054's rule, enforced at the forwarding store);
- a **stale** beat (older sequence) displaces nothing
  and is counted in `beats_stale` / `stale_sequence`;
- a **withdrawal tombstone** gossips like any other
  state: carried across a heal, it deletes a stale
  entry still held on the far side, and the old
  registration arriving afterwards is refused
  `stale_sequence` at the replica's door — no
  resurrection by gossip.

Refusals at the other doors are named too: a
`malformed_beat` never enters a store, and a node
publishing a heartbeat that names another node is
refused outright (`GossipError`) — a node originates
only its own health word.

## Counted state

`report()` returns the validated `gossip_report` v1:
every node's entry, tombstone, and beat counts with
its present/suspect/gone standings, the rounds run and
the virtual time they stand for, exchanges and
messages spent, exchanges refused and why, the largest
fanout used, duplicate and stale gossip counted apart,
whether the mesh stands converged at that moment, and
any convergence measurements recorded on it. Events
are Y-013 envelopes from the `fabric` component
(`gossip.round_completed`, `gossip.converged`,
`gossip.exchange_refused`) — names and counts only,
never payload content.

## Honesty

The mesh is an in-process epidemic model: records and
reports carry `simulated: true` as a schema constant,
no socket is opened, no LAN is crossed, and gossip
here rides direct calls between the stores — not the
Y-050 fabric's topics and not Y-051's TLS. What is
proven is the propagation *rules*: epidemic reach,
fanout discipline, partition and heal, duplicate and
stale harmlessness under Y-053/Y-054's own merge
rules, and convergence measured at three mesh sizes.

## Not built here

- The CRDT formalisation of the registry and
  membership stores — these same merge rules stated as
  a CRDT and property-tested — is Y-056.
- Real carriage — gossip riding the fabric (Y-050)
  over TLS (Y-051) between the peers discovery finds
  (Y-052), on the real LAN — is Y-062; hardware proof
  is Part VIII's. Gossip overhead as a published
  per-node-count load shape is Y-072 (this slice's
  measurements are its first numbers).
- No gossip seam on the node agent: as with the
  registry (Y-053), gossip is a mesh component's own
  act; the agent's fabric words remain its Y-054
  *liveness* seam (`liveness_tracker=`) until real
  carriage at Y-062 gives it a wire. No citizen factory
  wires a gossip mesh in this slice.
- Message-level signing of gossiped state is Y-059's
  road; clock discipline — peers reporting skew, and
  temporal logic distrusting any single clock — is
  Y-068.
