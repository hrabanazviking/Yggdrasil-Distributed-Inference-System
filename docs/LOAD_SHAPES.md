# Load Shapes (Y-072)

Every Part III door counts what it does, and
Y-064 puts those counts on the operator's door.
What no slice had published is the mesh's own
running cost — the traffic the mesh spends on
itself, carrying no one's task: heartbeats and
the gossip that carries them. This slice measures
that cost per node count and publishes it as
data, kept by `LoadShapes` in
`src/yggdrasil/load_shapes.py`.

## The plan is data

A `load_shapes_policy` v1 names the mesh sizes
to measure (the roadmap's own gossip sizes by
default: 3, 10, 30 citizens), the steady-state
window in virtual seconds (60 by default), the
per-convergence round budget, and the Y-055
gossip + Y-054 liveness policies the measurement
runs under — fanout 3, one round standing for 5
virtual seconds, a heartbeat every 10. Two laws
ride on top of the schema, enforced by the
measurer: the window must divide evenly by the
heartbeat interval (a window ending mid-beat
would publish a fraction of a beat as a whole
cost), and the gossip policy must be able to
hold the largest planned size.

## The counts come from the components

The policy's law is schema constants:
`counts_come_from_components: true` and
`hardware_claim_allowed: false`. A measurement
builds a *fresh* Y-055 `GossipMesh` of real
components — every node registering its fixture
census through Y-053's registry and beating
through Y-030's heartbeat builder — and reads
every number off the mesh's own report:
exchanges, messages, rounds, beats stored /
duplicate / stale, refused exchanges. The
measurer invents no count. The only arithmetic
it performs is the two published rates —
messages per node, and messages per node per
virtual second over the *measured* elapsed
time — each named as computed in the shape
that carries it. The suite proves the join
phase against Y-055's own
`measure_convergence` for the same size and
policy: the numbers are identical, because
they are the same mesh's numbers.

## Two phases, counted apart

The *join* is a fresh mesh run to convergence —
what it costs a mesh of this size to become one
mesh. The *window* is steady state: at each
heartbeat interval every node publishes a fresh
beat (sequence rising, so the Y-054/Y-055
freshness rules judge it a new word, never a
duplicate), and each beat-round is gossiped to
convergence before the next — what it costs
the mesh to *stay* one mesh while its heart
beats. A phase that spends its round budget
unconverged is published `converged: false`
with its counts standing as counted: a failed
convergence is a measurement, never an error
dressed as one, and the suite proves that door
with a zero round budget.

## Sizes are measured, not asserted

A shape's byte numbers are canonical-JSON
serialisations of the real records the mesh
holds at the window's end: one heartbeat
(`heartbeat_bytes`), one registry entry
(`registry_entry_bytes`), and one node's whole
state offer — its entries, tombstones, and held
beats (`state_offer_bytes`), the payload one
anti-entropy exchange direction offers.
In-process gossip moves objects, not bytes;
these are the sizes of the objects whose
movement the counts describe, labelled as
exactly that.

## The published table

The default plan, measured on the simulator's
virtual clock (SIMULATED — see Honesty):

| Nodes | Join: rounds / messages | Window: beats / rounds / messages | Total messages (exchanges) | Messages per node | Per node per virtual second | Duplicate / stale beats | Heartbeat / entry / state offer (bytes) |
|---|---|---|---|---|---|---|---|
| 3 | 1 / 12 | 18 / 6 / 72 | 84 (42) | 28.0 | 0.8 | 166 / 36 | 586 / 679 / 3,870 |
| 10 | 2 / 120 | 60 / 9 / 540 | 660 (330) | 66.0 | 1.2 | 5,170 / 540 | 586 / 679 / 12,809 |
| 30 | 2 / 360 | 180 / 11 / 1,980 | 2,340 (1,170) | 78.0 | 1.2 | 55,750 / 5,220 | 586 / 679 / 38,349 |

Read plainly: convergence itself is cheap and
barely deepens — two rounds at every size past
three. The steady cost is the window, and inside
it the epidemic's redundancy is the story:
per-node messages grow 28 → 66 → 78 across the
table (the join amortises; the window does not),
and at 30 nodes the mesh counts 55,750 duplicate
beats against 180 fresh ones — push-pull gossip
re-offers what a peer already holds, by design,
and the price of that simplicity stands here in
the open as the number a future transport or
fanout change must beat. The full counted
report behind this table is reproducible at
will with `measure_load_shapes()`.

## The door judges its acts

Measuring a size is an act judged by named
record: a size outside the plan is refused
`size_not_planned`, a size measured twice on
one door `duplicate_size` (the published table
never quietly holds two shapes for one size),
and a size that is not a positive count
`malformed_size` — each refusal counted apart,
measuring nothing. `measure_load_shapes()`
publishes the whole table in one call.

## Honesty

Everything here is SIMULATED: `simulated: true`
is a schema constant on the shape, the record,
and the report. The meshes are in-process
simulator citizens on a virtual clock
(Y-014/Y-055); no socket was opened and no LAN
was crossed. The published numbers are the
simulator's counted cost of the mesh's own
rules — evidence about the *protocol's* shape,
never about any device's network. Real carriage
was proven at Y-062; what the same shapes cost
on real hardware is Part VIII's road, and the
policy's `hardware_claim_allowed: false`
constant exists so no reader can mistake this
table for it.

## Not built here

- Failure drills — node kill mid-task,
  partition mid-stream, corrupt message
  injection — are built at Y-073
  (`docs/FAILURE_DRILLS.md` when it lands);
  the Part III gate is Y-074.
- No node-agent seam: as with the Part III
  slices before it, the agent gains no
  load-shape seam in this slice; the measurer
  is an operator-side instrument over the
  components, run when the table is wanted.
