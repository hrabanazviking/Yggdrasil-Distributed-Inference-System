# The Task Channel (Y-057)

Until this slice the fabric (Y-050) could carry any
message, the registry (Y-053) could say who can do
what, and the sandbox (Y-029) could run a task on one
node — but a task itself had no road between nodes.
The task channel (`src/yggdrasil/task_channel.py`)
is that road: task offers, claims, and result streams
ride the fabric with the Y-007 envelopes, and
backpressure is explicit.

## The road

A `TaskChannel` stands on one `Fabric`. Nodes
`join(node_id)` (attached to the fabric and subscribed
to the offers topic) and `leave(node_id)`.

- **Offers** — `offer(offerer, task)` validates the
  task as a Y-007 `task_envelope` v1, records it open,
  and publishes it on the fabric topic `tasks.offers`;
  every joined node's fabric inbox receives a validated
  `fabric_message` whose payload is the envelope whole.
  A task id already known is refused `duplicate_task`;
  an offer past the policy's `max_open_tasks` is
  refused `channel_full`.
- **Claims** — `claim(task_id, claimant)` is a direct
  fabric message to the offerer, and the first claim
  wins. A claim on a claimed task is refused
  `already_claimed`, on a closed task `task_closed`,
  on an unknown task `unknown_task`, and a node
  claiming its own offer `self_claim`. Whether a node
  *should* claim is Verðandi routing (Part IV), and
  capability fitness is Y-038's placement law — the
  channel carries claims, it does not route.
- **Streams** — the claimant sends Y-007
  `stream_event` v1 records with
  `send_stream(task_id, claimant, event)`, each a
  direct fabric message to the offerer. Sequences must
  be consecutive from 0 (a gap or replay is refused
  `out_of_order`); an event from anyone but the
  claimant is refused `not_claimant`, for an unclaimed
  task `not_claimed`, naming another task
  `task_mismatch`, malformed `malformed_event`. The
  per-task total is bounded by
  `max_stream_events_per_task` (`stream_too_long`).
- **Results** — `send_result(task_id, claimant,
  result)` validates a Y-007 `result_envelope` v1
  whose `task_id` names this task and whose
  `provenance.node_id` is the claimant (any other name
  is refused `provenance_mismatch`). The result rides
  a direct fabric message to the offerer and closes
  the task; every later act on it is refused
  `task_closed`. `stream_for(task_id)` and
  `result_for(task_id)` hand the offerer exactly what
  the channel carried.

## Backpressure is explicit

The policy's `stream_window` (in the validated
`task_channel_policy` v1, with `max_open_tasks` and
`max_stream_events_per_task`) is how many stream
events one task may have delivered-but-unacknowledged.
The offerer frees the window with
`acknowledge(task_id, offerer, up_to_seq)` — acks are
cumulative and monotonic, and an ack past the last
delivered seq is refused `ack_ahead`. An event offered
while the window is full is refused `backpressure`:
the refusal names how many events stand
unacknowledged, the event is delivered nowhere and
hidden in no buffer, and the claimant may resend the
same seq once the offerer acks. Backpressure refusals
are counted apart in the report, so a mesh that is
pushing back says so in numbers.

## Counted state

`report()` answers a validated `task_channel_report`
v1: the channel's nodes, every task's validated
`task_channel_record` v1 (offerer, claimant, status,
stream count, the window's two edges `last_seq` /
`acked_seq`, terminal result status), how many tasks
stand open, and the lifetime totals — offers, claims,
stream events, and results, made and refused, with
every refusal reason counted by name. Refusals are
`TaskChannelError`s carrying their reason, recorded
as `task_channel.<act>_refused` Y-013 events from the
`fabric` component — names, ids, and counts only,
never payload content.

## Honesty

Everything here is **SIMULATED** — `simulated: true`
is a schema constant on the report, and the fabric
underneath is in-process: no socket is opened, no LAN
is crossed. What is proven is the channel's
offer/claim/stream/result discipline and its explicit
backpressure on the contract shapes.

## Not built here

- Partition behaviour — a split mesh working in
  halves, and duplicate task executions reconciled by
  task id on healing — is Y-058. This slice's claims
  are single-mesh words; nothing here reconciles two
  claims made on opposite sides of a cut.
- Message provenance enforcement (every message
  signed, unsigned traffic dropped and counted) is
  Y-059; per-node rate and size quotas beyond this
  channel's own bounds are Y-060; store-and-forward
  for a sleeping node with a TTL is Y-061.
- Real carriage on the home LAN is Y-062, and
  hardware proof is Part VIII's.
- No task-channel seam on the node agent: as with
  the registry (Y-053), gossip (Y-055), and the CRDT
  stores (Y-056), the agent gains fabric traffic at
  real carriage (Y-062). Routing — choosing *which*
  node should claim — is Part IV's Verðandi, never
  this channel.
