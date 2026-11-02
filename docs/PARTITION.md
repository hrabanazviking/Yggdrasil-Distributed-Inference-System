# Partition Behaviour (Y-058)

The fabric (Y-050) already modelled a cut in the open
— each side keeps delivering within itself, seam
traffic waits in outboxes — and the CRDT stores
(Y-056) already knew how divergent replicas merge.
What neither decided is what a *task* means across a
split. The partition mesh (`src/yggdrasil/partition.py`)
is that law: a split mesh keeps working in halves,
and on healing the CRDTs merge and duplicate task
executions are reconciled by task id — never silently
double-answered.

## The mesh

A `PartitionMesh` joins nodes onto one `Fabric`;
each node also holds its own Y-056 `CrdtStore`
replica. `register(node, census)` writes a node's
measured census on its own replica (a census naming
another node is refused `census_mismatch`);
`offer_model(node, model)` adds a model offer there.
`sync_stores()` merges every replica with every
other while the mesh is whole; while split it is
refused `partitioned`, because cross-seam state
cannot travel — that is what a partition means.

## The cut

`cut(nodes)` names one side; the rest are the other
side. Naming every node (`cut_all`), nobody, a node
off the mesh (`not_joined`), cutting twice
(`already_partitioned`), or joining mid-split
(`partitioned`) are all refused by name. The fabric
is cut with the mesh, so offers published and claims
sent across the seam wait in the fabric's outboxes
and flush at healing — held, never lost, never
delivered early.

## Halves keep working

Offers, claims, and results follow Y-057's
discipline, judged per side:

- An **offer** is seen by the nodes on the offerer's
  side (mesh-wide while whole). A claim from the far
  side of a split is refused `unknown_task` — from
  there, the task genuinely is not known.
- A **claim** is judged against the claims on the
  claimant's own side: first claim on a side takes
  the task for that side; a second claim on the same
  side is refused `already_claimed`. A task offered
  before the cut was seen by everyone, so both sides
  may claim it — in mutual ignorance. That is the
  duplicate execution healing exists to reconcile.
- A **result** is judged against the claimant of the
  sender's own side (`not_claimant`,
  `provenance_mismatch`, `task_mismatch`,
  `malformed_result`, `duplicate_result` refused by
  name as in Y-057).

## Answers: exactly one, delivered exactly once

Each side's terminal result is held in the mesh's
reconciliation ledger.

- While **whole**, the first result is the answer
  and rides a direct fabric message to the offerer.
- While **split**, a result from the offerer's own
  side is delivered at once — *provisionally* (the
  record says so). A result from across the seam is
  NOT sent to the offerer: which execution is the
  answer is healing's decision, and an answer is
  delivered exactly once.

## Healing reconciles by task id

`heal()` reunites the fabric, makes every task
visible mesh-wide, merges every CRDT replica with
every other (the policy's `merge_on_heal`), and then
settles each task that produced executions on both
sides under the policy's one deterministic winner
rule — `offerer_side_first_then_earliest_finish_then_claimant`:

1. the offerer's own side's result wins when one
   stands — so a provisional answer the offerer
   already holds is **never retracted**;
2. else the earliest finish;
3. ties break by the canonically smaller claimant
   name.

The winner is confirmed as the task's single answer
and delivered if the offerer never held it. Every
other execution becomes a **named duplicate**:
retained in the ledger, listed by
`duplicates_for(task_id)`, counted in the report's
`duplicates_reconciled`, evented as
`partition.duplicate_reconciled` — and never
delivered. A duplicate that arrives *after* healing
from a node that legitimately claimed the task on
its side during the split is reconciled the same
way on arrival; a result from anyone else is
refused by name. `answer_for(task_id)` therefore
returns at most one result, ever, and the offerer's
fabric inbox holds at most one `task_result` per
task — both proven in the suite.

## The CRDT side of healing

While split, each side's registry and membership
writes land only on its own replicas — the report's
`crdt_converged` honestly reads false. Healing
merges all replicas pairwise until every one holds
identical mergeable state; Y-056's stamp order
settles a same-node contest (two sides carrying
different writes for one node) the same way on every
replica, and model offers made on the far side are
present mesh-wide afterwards.

## The law as data

`partition_policy` v1 carries the constants as
schema law: `duplicate_answers_allowed` is always
`false`, `merge_on_heal` always `true`, and the
winner rule is a named constant. A policy naming any
other law fails validation. `report()` answers a
validated `partition_report` v1 — sides, cuts and
heals, per-task `partition_record` v1s, counts with
refusals named, duplicates reconciled, and the
CRDT convergence flag. Events are Y-013 envelopes
from the `fabric` component, names and counts only.

## Honesty

This mesh is a formalisation standing on Y-050's
fabric, Y-056's stores, and Y-057's envelopes — not
a rewiring of any of them; the task channel keeps
its single-mesh bookkeeping, and this slice keeps
the partition ledger beside it. Records carry
`simulated: true` as a schema constant; everything
is in-process — no socket is opened and no LAN is
crossed. What is proven is the partition discipline
and the reconciliation law on the contract shapes.

## Not built here

- Message provenance enforcement — every message
  signed, unsigned traffic dropped and counted — is
  Y-059; per-node rate and size quotas are Y-060;
  store-and-forward for a sleeping node is Y-061.
- Real carriage — the fabric on the real LAN, TLS
  on, discovery unaided — is Y-062; hardware proof
  is Part VIII's.
- Stream events across a partition follow Y-057's
  per-side channel discipline; this slice reconciles
  terminal executions, which is where a second answer
  could ever be forged.
- No node-agent seam: as with the registry (Y-053),
  gossip (Y-055), CRDTs (Y-056), and the task channel
  (Y-057), the agent gains partition behaviour at
  real carriage (Y-062), not before.
