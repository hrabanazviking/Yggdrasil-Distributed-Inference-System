# Fabric Observability (Y-064)

Every fabric component already counts what it
does: the WAN shaper (Y-063) counts crossings and
drops, gossip (Y-055) counts convergence, the rate
door (Y-060) counts throttling, provenance (Y-059)
counts unsigned drops. What the operator did not
have is one door where those counts stand
together. **This slice is that door:**
`FabricObserver` in
`src/yggdrasil/fabric_observability.py` —
message rates, convergence, and drops, judged
the same way every time.

## An observation is data

A node reports its standing over one window as a
validated `fabric_observation` v1: sent,
delivered, and dropped messages in
`window_seconds`, every drop counted apart by
named reason in `dropped_reasons`, messages
throttled at its doors, gossip rounds run, its
convergence word (`null` when it reports none),
and the registry entries it holds. Counts and
names only — never payload content (Y-013's
rule).

Counts that cannot be true are refused by name
and counted apart, stored nowhere:

- delivered plus dropped past sent —
  `counts_inconsistent`;
- drop reasons not summing to the dropped
  count — `reasons_incomplete`. A drop the door
  cannot explain is not silently absorbed.

The latest observation from a node replaces its
older one: the report is the mesh *now*, not an
archive.

## Rates and drops are computed, not reported

`sent_per_second` is an observation's own sent
over its own window; `drop_rate` is dropped over
sent (zero where nothing was sent). The mesh
rate is the sum of the nodes' rates and the mesh
drop rate the totals' own ratio. No node can
flatter the door by reporting a rate of its own
invention.

## Staleness and alerts are named

The watch is a validated
`fabric_observability_policy` v1: an observation
older than `stale_after_seconds` (default 120 s)
at report time is stale, and its node is named
in `stale_nodes`; a node or mesh drop rate above
`max_drop_rate` (default 0.05) raises
`drop_rate_high`; nodes reporting
`converged: false`, or a latest gossip report
standing unconverged, raise `nodes_not_converged`;
an empty door says `no_observations` instead of
pretending health. Alerts are always computed —
`alerts_enabled` is a schema constant `true`, so
a policy naming a silent door fails validation.

`report()` answers a validated
`fabric_observability_report` v1: per-node
standings with their computed rates and alerts,
the mesh totals with drops counted apart by
reason, the count of refused observations, and
the convergence standing.

## Real reports feed the door

- A validated `wan_report` v1 (Y-063) is ingested
  per sending node: each node's crossings on the
  shaper become its observation over a
  caller-named window. Convergence is not the
  shaper's word, so it is reported `null`.
- A validated `gossip_report` v1 (Y-055) sets the
  door's convergence standing — converged or not,
  rounds run, messages exchanged, node count —
  until a newer report replaces it.

Neither component is rewired; the door reads the
reports they already produce.

## Honesty

Everything here is SIMULATED: `simulated: true`
is a schema constant on the observation and the
report, the door aggregates in-process reports,
opens no socket, and crosses no LAN (real
carriage was Y-062's own proof). A node's
observation is only as true as the node's own
counting; the door judges consistency and
freshness — it does not manufacture traffic
facts.

## Not built here

- Mixed-version meshes degrading by contract are
  Y-065; restart resilience and re-sync Y-066;
  the registrar's death drill Y-067.
- The audit tap (Y-071) is a different door: an
  operator-consented event feed. This slice is
  counted standings, not a live event stream.
- No node-agent seam: as with the Part III slices
  before it, the agent gains no observability seam
  in this slice; deployment wiring hands the door
  its observations.
