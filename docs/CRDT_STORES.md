# CRDT stores — registry + membership, formalised (Y-056)

Part III's seventh slice, in `src/yggdrasil/crdt_stores.py`.
The roadmap's sentence: *CRDT stores land (ADR-003):
registry + membership as CRDTs; merge correctness
property-tested.* ADR-003 decided which stores are
CRDTs and fixed their merge rules; Y-053 built the
registry service whose pairwise sync already obeyed
those rules in behaviour. This slice states the same
stores **as** a CRDT and proves the properties the ADR
demands before the stores are trusted: merge is
idempotent and order-insensitive.

## The two CRDTs

ADR-003's table, executed literally:

- **Registry + membership is a last-writer-wins
  element set, one element per node record.** An
  element is a node's whole record — its measured
  Y-022 census. Every write carries a `crdt_stamp` v1:
  the ADR-003 triple `(node_id, lamport, wall_time)`.
  Stamps carry a total order — lamport, then wall
  time, then node_id — so ties break by node_id and
  every replica settles the same contest the same way
  without talking. The newest write wins whole; an
  older write is stale (counted, evented, and retained
  behind any tombstone as merge state so arrival order
  cannot change what a replica holds); the same stamp
  with different facts is a counted **conflict**,
  settled deterministically — the canonically smaller
  census is kept on every replica. A write's stamp
  must be authored by the node the write is about:
  records are written by their own node or carried by
  merge, never forged by a bystander.
- **Models are an observed-remove set per node.** An
  offer is an add tagged with its own stamp; a remove
  tombstones exactly the add-tags the remover had
  observed. An offer the remover never saw — a
  concurrent offer travelling another road — survives
  the remove and is present when it arrives; an
  observed offer removed is gone everywhere the remove
  reaches.

Deletes of node records are **tombstones** under the
same stamp order: a tombstone newer than the write
removes the node, an older write arriving afterwards
cannot resurrect it, and a write newer than the
tombstone is a genuine rejoin. Tombstones are
retained for the policy's registry epochs (ADR-003:
one). `advance_epoch()` prunes an outlived tombstone
only together with the write it superseded, so the
prune itself can never resurrect anyone; proper
retirement of a long-gone node stays Y-180's act.

Each replica keeps a **Lamport clock**: a local act
ticks it, and observing any stamp raises it to at
least that stamp's lamport, so a replica's next local
stamp outranks everything it has seen.

## Merge, and the properties proven

Merge is state-based. `state()` exports the whole
mergeable state as a validated `crdt_state` v1 — the
newest write per node, the tombstones with their
epochs, the model adds, and the removes naming the
adds they observed — and `merge_state()` takes the
union under the stamp order; `merge()` and `sync()`
are the replica-to-replica shapes over that export.

The suite property-tests the union, in
`tests/test_crdt_stores.py`:

- three divergent replicas (competing writes, a
  delete on one road, a model offered on one road and
  withdrawn on another) merged in **every
  permutation** resolve to identical records *and*
  identical mergeable state;
- the same states broken into single-element
  fragments and merged in **seeded shuffles** land at
  exactly the whole-merge state;
- merging the same state twice changes nothing the
  second time (**idempotent**), and
  `(a ∪ b) ∪ c == a ∪ (b ∪ c)` (**associative**).

## The policy is data

A validated `crdt_policy` v1 bounds the store: at
most 512 node records per replica, tombstones
retained one registry epoch — an operator's numbers,
labelled as a policy, not facts about a mesh. The
report is a validated `crdt_report` v1: present and
tombstoned nodes, present model offers, writes
applied and refused stale, removes, merges,
conflicts, epoch prunes, the Lamport clock and epoch,
and the resolved `crdt_node_record` v1 records
themselves. Events are Y-013 envelopes from the
`registry` component — names and counts only.

## Honesty

This is the CRDT formalisation standing beside
Y-053's service, not a rewiring of it: the registry
service keeps serving registrations, and gossip
(Y-055) keeps riding its sync. Records carry
`simulated: true` as a schema constant; the store is
in-process state only — no socket is opened and no
LAN is crossed. What is proven is the merge
mathematics on the contract shapes.

## Not built here

- Real carriage — CRDT state riding the fabric
  (Y-050) over TLS (Y-051) between the peers discovery
  finds (Y-052) — is Y-062 on the real LAN; hardware
  proof is Part VIII's.
- Partition behaviour of the whole mesh (split halves
  working independently, duplicate task executions
  reconciled on healing) is Y-058; this slice proves
  the store-level merge that healing will rely on.
- Restart resilience — a node rejoining and
  re-syncing its CRDT state without a full reload —
  is Y-066.
- No CRDT seam on the node agent: as with the
  registry (Y-053) and gossip (Y-055), the agent gains
  fabric traffic at real carriage (Y-062).
