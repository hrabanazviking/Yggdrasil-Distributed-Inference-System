# Idle Generosity (Y-041)

`src/yggdrasil/idle_generosity.py` — an idle node advertises
spare capacity; a busy one withdraws it. The advertisement
is the present-tense truth Verðandi routes on (Part IV), so
it is evaluated fresh from measured load on every asking,
never cached as a standing promise, and every withdrawal
names its reasons in the data.

## Policy as data (`generosity_policy` v1)

How idle is idle is the operator's call, not a fact about
a machine:

- `max_in_flight` (default 0) — the most tasks that may be
  running while the node still calls itself idle;
- `max_queue_depth` (default 0) — the deepest the local
  queue may be;
- `min_spare_memory_fraction` (default 0.25) — the least
  spare memory that must remain after the present load;
- `withdraw_when_shedding` / `withdraw_when_draining`
  (both default true).

## The advertisement (`spare_capacity` v1)

`contracts/schemas/spare_capacity.schema.json` — the
verdict (`generous` or `withdrawn`), the withdrawal
reasons, the load it was judged from (in-flight count,
queue depth and free slots, free model slots), the spare
on offer, and the capacity it rides on:

- **Reasons, closed vocabulary:** `not_joined`,
  `draining`, `in_flight`, `queue_held`,
  `governor_shedding`, `memory_tight`,
  `memory_unmeasured`. A generous advertisement has none;
  a withdrawn one names at least one.
- **Spare, measured or null:** memory bytes and fraction
  are computed from the governor's readings; CPU spare is
  `1 − cpu_share_used`. A reading nobody measured is
  `null`, never an invented plenty — and unmeasured
  *memory* withdraws the advertisement outright
  (`memory_unmeasured`), because a node that has not
  measured its memory cannot prove it has any to spare.
  An unmeasured CPU share blocks nothing; it is simply
  reported null.
- **Capacity, present-tense:** the accelerator kinds come
  from the node's latest census — the *present* kinds, so
  a node degraded under Y-040 advertises its reduced
  truth, never its baseline — plus the models presently
  loaded.

## Transitions (`generosity_report` v1)

The first evaluation and every flip are recorded: from/to
status, the withdrawal's reasons, the moment.
`generous_since` marks when the current generous spell
began and clears the moment it ends. Each flip is also a
validated Y-013 EventEnvelope from the `node_agent`
component — `generosity.advertised` /
`generosity.withdrawn` — counts and reason names only, no
payload content. A steady state records no new transition:
asking twice changes nothing but the observation count.

## Node agent

An agent built with `idle_generosity=` grows:

- `generosity_advertisement()` — builds the load from the
  agent's own present facts (lifecycle state, in-flight
  leases, the local queue's report, the governor's verdict
  and readings, the lifecycle's free slots, the latest
  census, the loaded models) and evaluates it fresh;
- `generosity_report()` — the ledger read back;
- `status()` carries the last advertisement under
  `generosity` (`None` before the first evaluation, and
  `None` forever on an unwired agent, whose behaviour is
  exactly as before).

All four citizen factories (Pi, laptop, VM, phone) wire a
ledger with the default policy.

## Not built here

No peer has seen an advertisement — there is no fabric
yet (Part III); the advertisement is a validated local
record the fabric will carry later. Nothing here places,
accepts, or routes a task: accepting offered work is
Verðandi's country (Part IV), and a spare advertisement is
an offer of capacity, never a promise that any particular
task will fit — the governor (Y-028) still admits or
refuses each arriving task on its own declared footprint.
The advertisement also does not *hold* capacity: between
the advertisement and a task's arrival the node may take
other work, and the next evaluation will say so.
