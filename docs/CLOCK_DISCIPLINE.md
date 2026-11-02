# Clock Discipline (Y-068)

Y-054's liveness tracker already refuses to age a
peer on the peer's own timestamp: ageing is measured
on the observer's clock, because no node's temporal
logic may trust another node's clock. This slice
builds the other half of that discipline — nodes
report clock skew, and temporal logic (Part VI)
never trusts a single node's clock — as one judged
watch kept by `ClockWatch` in
`src/yggdrasil/clock_discipline.py`.

## A skew is a measurement between two clocks

An observation names an **observer**, a **node**,
the time the node claimed, and the time on the
observer's own clock when the claim was heard. The
watch computes the skew itself —
`skew = claimed − observed` — and never takes a
node's word for its own skew. A validated Y-030
heartbeat feeds the watch without rewiring:
`observe_beat` reads the beat's own `at` (the node's
claimed time) and `seq` into an observation judged
by the same rules, and a beat that fails its own
contract is refused `malformed_beat`.

## No single clock is trusted — the law is constants

The `clock_policy` v1 fixes `single_clock_trusted:
false` and `consensus_method: "median"` as schema
constants, with `min_observers` (default 3, never
below 2), `max_trusted_skew_seconds` (default 5),
`severe_skew_seconds` (default 60), and `max_nodes`
(default 32) as data. The trusted line must fall
before the severe line — the watch enforces that
ordering on top of the schema, as the liveness
tracker enforces its own.

A node's standing is the **median** of its
observers' votes, and each observer contributes its
**latest** sample only — one loud observer cannot
flood the vote, and one wild vote cannot drag a
median the way it drags a mean (the suite proves a
1,000-second outlier leaves a 3-second median
standing). Until at least `min_observers` distinct
observers have reported, a node has no verdict at
all: it is `uncorroborated`, however plausible its
clock looks. Corroborated standings are named:

- **trusted** — median within the trusted line;
- **skewed** — beyond it, up to the severe line;
- **severe** — beyond the severe line.

## Temporal logic comes to the judge door

`judge` is the only road by which a claimed
timestamp from a node may enter temporal logic:

- an unknown node is refused `unknown_node`, an
  uncorroborated one `uncorroborated`, a severe one
  `severely_skewed`, a malformed time
  `malformed_sample` — all by name, all counted;
- a trusted or skewed node's timestamp is accepted
  only **corrected**: the record's `corrected_at`
  is the claimed time minus the corroborated median
  skew. What temporal logic uses is the mesh's
  consensus about the moment, never the node's own
  word for it — a clock 30 seconds fast has its
  timestamps carried back 30 seconds, and a clock
  behind is carried forwards.

Freshness is by sequence, per (observer, node)
pair, the mesh's standing rule: an older sample is
refused `stale_sequence`, an equal one
`duplicate_sequence` — a replayed clock report
refreshes nothing. A node observing itself is
refused `self_observation`; duplicate enrolments
`duplicate_node`, a full watch `mesh_full`, unknown
doors `unknown_node` / `unknown_observer`.

The mesh **consensus offset** — the median of the
corroborated, non-severe nodes' median skews, or
`null` when nothing is corroborated — is reported
as a measured standing. It is never used to
silently correct anyone: corrections happen per
timestamp, at the judge door, and are counted
(`judgements`, `judgements_corrected`).

## Honesty

Everything here is SIMULATED: `simulated: true` is
a schema constant on the record and the report.
Times are reported values judged in-process; no
socket was opened, no LAN crossed, no node's clock
was read directly or set (real carriage was
Y-062's own proof). What is proven is the
discipline: corroboration before trust, a median
that resists outliers, correction instead of
belief, and named refusals.

## Not built here

- The fabric conformance kit — any future
  transport swap passing the same suite — is
  built at Y-069 (`docs/FABRIC_CONFORMANCE.md`).
- Part VI's temporal projection consumes this
  door when it is built; nothing here claims its
  verdicts.
- No node-agent seam: as with the Part III slices
  before it, the agent gains no clock seam in this
  slice; deployment wiring enrols the nodes it
  starts and feeds the watch their reports.
