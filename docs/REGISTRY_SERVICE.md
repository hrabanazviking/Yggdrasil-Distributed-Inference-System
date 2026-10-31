# The registry service — every replica holds the truth (Y-053)

Part III's fourth slice, in
`src/yggdrasil/registry_service.py`. The roadmap's
sentence: *nodes register census + models; the registry
is itself replicated (no single registrar to kill).*
The second clause is schema law: the `registry_policy`
v1 carries `replicated: true` and
`single_registrar_allowed: false` as **constants** — a
policy that made one node the registry fails
validation.

## Registration

A node registers as a validated
`registry_registration` v1: its id, a per-node rising
`seq`, its measured Y-022 census riding whole, and its
Y-008 model entries riding whole beside it. The census
must name the registering node itself — a registration
carrying another node's census is refused
`census_node_mismatch` by name, at construction in
`build_registration()` and again at every replica's
door. Every refusal is named, counted in the report's
`refused_reasons`, and emitted as a
`registry.registration_refused` event:
`malformed_registration`, `census_node_mismatch`,
`duplicate_model` (one model named twice),
`stale_sequence`, `conflicting_sequence`,
`registry_full` (the policy's `max_entries`, default
512 — a policy, not a fact). A stored
`registry_entry` v1 keeps the first and last
registration moments and the replica that first
accepted it as `origin_registry` — provenance, never
authority.

`register_local(census, models)` is the holding node's
own door: the replica issues the next sequence for its
own node, so its word never arrives stale at home.

## Replication — there is no registrar

Every `RegistryService` is a full replica. It holds
every entry it accepted or learned, answers every
query from its own copy, and accepts registrations for
any node. `sync_with(other)` snapshots both replicas'
entries and tombstones and merges each into the other;
both end the call holding the union. Merge rules are
identical on every replica and depend only on the
records:

- an unknown node is adopted (counted in
  `entries_learned_via_replication`);
- a newer `seq` replaces the older entry whole;
- the same `seq` with the same facts is a no-op; with
  different facts it is a **conflict** — counted,
  evented as `registry.conflict`, and settled
  deterministically (the canonically smaller record is
  kept on every replica, so the settlement converges);
- an older `seq` is history, not news, and is ignored.

Because the rules are order-independent, replicas
converge to identical entries whichever order syncs
happen in — proven in the suite both ways round a
three-replica mesh. The registrar's death drill is
literal in the tests: register at one replica, sync,
drop that replica — the survivors still serve the
entry whole, and a brand-new replica syncing with any
survivor learns it, origin and all.

## Withdrawal is a tombstone

`withdraw(node_id, seq)` with a sequence newer than
the entry's removes the entry and plants a tombstone
for that node. Tombstones travel on sync alongside the
entries, and a registration at or below a tombstone's
sequence is refused `stale_sequence` wherever it
arrives — an old registration still travelling the
mesh cannot resurrect a withdrawn node. A withdrawal
at or below a live entry's sequence is itself stale
(`withdrawal_stale`): it kills nothing. A genuinely
newer registration after a tombstone is the node's
own return, and is accepted.

## Queries

From a replica's own copy, with no reference to any
other: `entry(node_id)`, `entries()`,
`nodes_with_model(name)` (the placement question Part
IV asks first), and `nodes_with_capability(cap)` —
nodes whose registered census carries a *reachable*
engine claiming the capability. The registry repeats a
census claim exactly as measured; it never upgrades
one. `report()` returns the validated
`registry_report` v1: entries held, distinct models
offered, registrations accepted, refused and why,
entries learned via replication, syncs, withdrawals,
conflicts, and withdrawn node ids — names and counts
only. Events are Y-013 envelopes from the `registry`
component.

## Honesty

Replication here is an in-process sync model: records
and reports carry `simulated: true` as a schema
constant, no socket is opened, and no LAN is crossed.
What is proven is the registry *rules* — registration,
the census-agreement door, sequence freshness, bounded
size, tombstones, deterministic conflict settlement,
and convergence. A registry entry is a node's
registered facts, not a claim the node is alive right
now.

## Not built here

- Heartbeats and present → suspect → gone liveness
  ageing are built at Y-054 (`docs/LIVENESS.md`);
  discovery expiry (Y-052) is peer table freshness, a
  liveness verdict is not a withdrawal, and this
  registry holds facts until they are superseded or
  withdrawn.
- Epidemic gossip of registry and health state is
  Y-055 (this slice's sync is pairwise and called
  directly; gossip is its epidemic carriage), and the
  CRDT formalisation of the registry + membership
  stores is Y-056 (this slice's merge rules are the
  behaviour that slice formalises and property-tests).
- Real carriage — registrations riding the fabric
  (Y-050) over TLS (Y-051) between the peers discovery
  finds (Y-052) — is Y-062 on the real LAN; hardware
  proof is Part VIII's.
- No registry seam on the node agent: the agent gained
  its peers' words at Y-054 as the *liveness* seam
  (`liveness_tracker=`); registry traffic stays a
  component's own act (`RegistryService` used directly)
  until real carriage at Y-062 gives it a wire.
- Registration is a node's word about itself, gated by
  census agreement and sequence — message-level
  signing of registrations is Y-059's road, and
  certificate revocation remains Y-011's named
  residual.
