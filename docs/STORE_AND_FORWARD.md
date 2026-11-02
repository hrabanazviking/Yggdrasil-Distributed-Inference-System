# Store-and-Forward (Y-061)

The task channel (Y-057) carries a task to a node
that is there to receive it. A sleeping node is
not there. This slice is the fabric's waiting
room: **a task for a sleeping node waits in the
fabric with a TTL; expiry is an event, not a
silence.** The waiting room is `StoreAndForward`
in `src/yggdrasil/store_and_forward.py`.

## A task waits, whole, under a TTL

`queue(sender, recipient, task, ttl_seconds=None)`
validates the task as a Y-007 `task_envelope` v1
and holds it, whole, for its named recipient. The
queueing may name its own `ttl_seconds`; otherwise
the policy's `default_ttl_seconds` applies, and no
task ever waits past the policy's
`max_ttl_seconds` — a TTL outside that range is
refused `ttl_out_of_bounds`. A task id the mailbox
already knows — held, delivered, expired, or
cancelled — is refused `duplicate_task`: an id is
spent once, so a waiting task can never be quietly
replaced by a second task wearing its name. A
malformed envelope is refused `malformed_task`.

## The waiting room is bounded, per side

The policy caps what waits, on three sides:

- the room as a whole — `max_held_tasks`
  (refusal: `mailbox_full`);
- what waits for one recipient —
  `max_held_per_recipient` (refusal:
  `recipient_full`), so one sleeping node's pile
  cannot crowd out another's;
- what one sender may leave waiting —
  `max_held_per_sender` (refusal: `sender_full`),
  so one sender cannot fill the room alone.

Every refusal is a `StoreForwardError` carrying
its reason, counted in the report, and evented.
A refused task is held nowhere.

## Waking delivers, exactly once, in order

`deliver(recipient)` is the sleeping node's
waking call: it hands over every task still held
for that recipient, in the order they were
queued, and marks each one delivered. A second
call hands over nothing — delivery is once, never
a replay. Tasks waiting for other recipients are
untouched, and a task whose TTL ran out in the
night is not handed over at all: it expired.

## Expiry is an event, not a silence

A held task whose TTL runs out is expired —
swept on any mailbox act and on an explicit
`expire()` call — and never quietly dropped:

- the expiry is evented
  (`store_forward.task_expired`);
- it is counted apart in the report;
- and it is written as a validated
  `store_and_forward_notice` v1, which the task's
  sender reads back with `expiry_notices_for(...)`.

The sender learns its task died of age, by name
and with its times, instead of waiting on a
silence.

## A sender may take its task back

`cancel(task_id, sender)` closes a held task at
its sender's word. A cancel by anyone else is
refused `not_sender`; a cancel of a task no
longer held — delivered, expired, or already
cancelled — is refused `not_held`; a cancel of an
unknown task is refused `unknown_task`. A
cancelled task is never delivered.

## Everything is counted

Each queued task is a validated
`store_and_forward_record` v1 — identifiers and
times only, never payload content. `report()`
answers a validated `store_and_forward_report`
v1: the policy, every record in queue order,
every expiry notice, how many tasks stand held,
and the lifetime totals — queued, delivered,
expired, cancelled — with refusals counted apart
by name. Events are Y-013 envelopes from the
`fabric` component (`store_forward.task_queued`,
`store_forward.task_delivered`,
`store_forward.task_expired`,
`store_forward.task_cancelled`, and the
`*_refused` family).

## The law as data

`store_and_forward_policy` v1 carries the
discipline: `expiry_evented` is always `true`,
`deliver_once` is always `true`, the default TTL
never exceeds the maximum TTL, and every bound is
a positive, bounded number. The default policy —
a policy, not a fact about a LAN — is a one-hour
default wait, a one-day maximum, a room of 256
tasks, at most 64 for one recipient and 128 from
one sender. A policy naming weaker discipline
fails validation.

## Honesty

The mailbox is an in-process holding:
`simulated: true` is a schema constant on its
report, no socket is opened, no LAN is crossed,
and no sleeping hardware was woken. What is
proven is the hold, TTL, delivery, and expiry law
on the contract shapes.

## Not built here

- Real carriage on the home LAN — the fabric on a
  real wire — is built at Y-062
  (`docs/LAN_FABRIC.md`); this waiting room's held
  tasks ride that wire when a later slice wires the
  mailbox to the composition, not yet. Fabric
  observability (message rates,
  convergence, drops on the operator's door) is
  built at Y-064 (`docs/FABRIC_OBSERVABILITY.md`);
  hardware proof is Part VIII's.
- Claiming and executing a delivered task follow
  the task channel's discipline (Y-057) once the
  node wakes; this slice holds envelopes and
  decides nothing about routing, claiming, or
  execution. Duplicate-execution reconciliation
  across a partition stays Y-058's law.
- No node-agent seam: as with the registry
  (Y-053), gossip (Y-055), CRDTs (Y-056), the task
  channel (Y-057), partitions (Y-058), provenance
  (Y-059), and rate discipline (Y-060), the agent
  does not gain store-and-forward at real carriage
  (Y-062) either — the composition adds no agent
  seam, as Y-062's own doc records.
