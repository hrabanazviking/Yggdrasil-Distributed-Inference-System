# Restart Resilience (Y-066)

Y-056 proved CRDT replicas converge when they
merge. This slice answers the uglier question: a
node *stops* — its process dies, its in-memory
replica with it — and later comes back. What does
it hold, what has the mesh done without it, and
how does it rejoin **without a full reload**? The
answer is a delta discipline, kept by
`RejoinCoordinator` in
`src/yggdrasil/restart_resilience.py`.

## A crash is modelled honestly

`crash(node)` throws the node's live replica
away. The only survivor is the node's
**checkpoint** — a validated `crdt_state` v1 the
node saved while it was up (`checkpoint(node)`),
standing in for durable local storage. A write
that was neither checkpointed nor gossiped to a
peer before the crash is *gone*, and the suite
proves it is gone rather than pretending
durability the design does not have. A
checkpointed write, by contrast, survives the
crash locally and propagates to the peer by delta
on the rejoin.

## The mesh does not wait

While a node is down the survivors keep writing,
removing, and syncing among themselves. Any act
addressed at the down node — asking for its
store, syncing with it, checkpointing it, taking
its digest — is refused by name (`node_down`),
never silently queued for a node that may never
return.

## Re-sync is a digest and two deltas

`restart(node)` raises a fresh replica and
restores the checkpoint locally — local durable
storage, not network traffic. The node stands
`restarted`: live, holding only what its
checkpoint held. `rejoin(node, via=peer)` then:

1. **exchanges digests** — a validated
   `restart_digest` v1 from each side: per-write
   stamps plus a SHA-256 hash of each census,
   tombstone stamps, model add/remove tags, the
   epoch and the Lamport clock. Stamps and hashes
   only; census content never rides a digest.
2. **computes each way's delta** — a
   `restart_delta` v1 carrying a partial
   `crdt_state` with exactly the elements the
   other side's digest shows it lacks or holds
   stale: a write travels when the receiver holds
   none for that node, an older stamp for it, or
   the same stamp over a different census (the
   hash sees what stamps cannot). A write the
   receiver already holds *fresher* never travels
   backwards — the Y-056 merge would refuse it
   stale anyway, so sending it would be waste
   dressed as sync.
3. **applies both deltas by the Y-056 merge** and
   states plainly, in the record, whether the two
   replicas' mergeable state **converged**
   (identical writes, tombstones, adds, removes).

The headline measurement: a node that
checkpointed a settled three-node state, missed
two census rewrites, a model add, a model
remove, and a fourth citizen's registration
while down, rejoins receiving **exactly those 5
elements** — strictly fewer than the peer's 7 —
sending 0, and converging.

## The law is schema constants

The `restart_policy` v1 fixes `sync_mode:
"delta"` and `full_reload_allowed: false` — a
policy naming a full reload fails validation.
Stronger: the coordinator exposes no full-state
transfer at all. Its report carries a
`full_reloads` counter that stands at zero
because there is no road that could raise it. A
rejoin whose delta would exceed the policy's
`max_delta_elements` (default 1024) is refused
`delta_too_large` — counted, applied nowhere,
the pair left exactly as it stood — rather than
quietly becoming the bulk transfer the policy
forbids.

One bound deserves its own words: a node that
kept **no** checkpoint rejoins from nothing, so
its incoming delta is every element it lacks —
which is all of them. That is still a delta,
judged and counted element by element, never a
reload; the record states the count, so the cost
of skipping checkpoints is a published number,
not a hidden one.

## Every rejoin is judged

`rejoin` and `apply_delta` (the seam raw deltas
arrive by) answer a validated `restart_record`
v1: `rejoined` / `applied`, or refused by name
and counted apart — `unknown_peer`,
`self_rejoin`, `peer_down`, `node_down`,
`not_restarted` (a rejoin attempted on a node
that never went down), `delta_too_large`,
`malformed_digest`, `malformed_delta`,
`delta_not_for_me`. A refused delta application
is recorded too, but is not counted among refused
*rejoins*. API misuse — a double crash, a restart
of an up node, a duplicate membership, an unknown
node — raises `RestartError` by name: those are
the caller's own errors, not traffic.

## Honesty

Everything here is SIMULATED: `simulated: true`
is a schema constant on the digest, the delta,
the record, and the report. A "crash" is the
in-process loss of one replica object and a
"checkpoint" an in-process state record standing
in for durable storage; no process was killed,
no disk was written, no socket opened, no LAN
crossed (real carriage was Y-062's own proof;
killing real processes on real citizens is the
Y-067 death drill and Part VIII's hardware road).

## Not built here

- The registrar's death drill — killing the
  first node ever started and proving the mesh
  notices nothing but a missing heartbeat — is
  Y-067; clock discipline Y-068.
- Real carriage of digests and deltas over the
  fabric + TLS is deployment wiring on Y-062's
  proven road; this slice judges the exchange
  in-process, as the Part III slices before it
  judged theirs.
- No node-agent seam: as with the Part III
  slices before it, the agent gains no restart
  seam in this slice.
