# Rate and Size Discipline (Y-060)

The threat model names the flooding node (T-03
gossip spam, T-07 resource exhaustion by
legitimate-shaped load). This slice is the answer
at local scale — Níðhöggr's first small tooth:
**per-node quotas on gossip and tasks, and a
flooding node throttled by its peers.** Each peer
keeps its own rate door (`RateGate` in
`src/yggdrasil/rate_discipline.py`) and judges the
traffic that arrives at it. Provenance (Y-059)
judges *whose* traffic is real; this door rations
*how much* of it rides.

## Judgement is peer-side and per sender

`admit(sender, traffic_class, size_bytes)` judges
one message by its sender, its class — `gossip`,
`task`, or `other` — and its size, and returns a
validated `rate_discipline_record` v1. Quotas are
counted **per sender**: a flooding node spends
only its own allowance and can never throttle a
quiet neighbour at this door (proven in the
suite).

## The quotas are a sliding window

The policy's `window_seconds` window holds a
sender's *admitted* messages only. Inside it, one
sender may have admitted at most:

- `max_messages_per_window` messages in total
  (breach: `quota_total`);
- `max_gossip_per_window` of them gossip
  (breach: `quota_gossip`);
- `max_task_per_window` of them tasks
  (breach: `quota_task`);
- `max_bytes_per_window` admitted bytes in total
  (breach: `quota_bytes`).

Throttled messages enter no window and consume no
quota — a flood cannot extend its own punishment
by being counted, and a sender that goes quiet
earns its allowance back as the window slides
past its admitted traffic.

## Size discipline is absolute

A single message larger than `max_message_bytes`
is throttled `oversized` however empty the window
stands; oversized traffic is delivered nowhere and
counted apart. An oversized message alone does
**not** earn a throttle cooldown — size is a
property of the message; flooding is a property of
the sender.

## A quota breach throttles the sender

The first message that would exceed a quota is
throttled by that quota's name, and the sender
enters a throttle cooldown of
`throttle_cooldown_seconds` at this door. While
it lasts, **all** of that sender's traffic is
throttled `throttled`, whatever its class or size.
Throttled traffic does not extend the cooldown.
When the cooldown has passed, the door lifts the
throttle (`rate.throttle_lifted`) and judges the
next message against the window again — if the
window is still full, that message breaches again
and earns a fresh cooldown. This is the peer
throttle the roadmap names: no central policeman,
each peer stops listening to a flooder for a
while, by a rule stated in data.

## Fabric messages judge themselves

`admit_message(message)` judges a raw fabric
message dict: its size is the canonical-JSON byte
count of the message as it stands
(`message_size_bytes`), and its class follows a
stated rule — a publication to a `tasks.*` topic
and every direct message (the task channel's
claims, streams, and results, Y-057) are `task`
traffic; every other publication (registry,
health, and state news) is `gossip` traffic. A
message with no usable sender or class is
throttled `malformed`, as is a direct `admit`
call with an unusable sender, class, or size.
`require_admitted(...)` is the same door for
callers that want a refusal raised as a
`RateError` carrying the reason.

## Everything is counted

`report()` answers a validated
`rate_discipline_report` v1: the policy, every
sender ever judged with its admitted and
throttled totals, admitted bytes, what its
current window still holds, and the moment its
throttle ends where one runs; every record in
order; and the totals — seen, admitted,
throttled, with throttled messages counted apart
by reason. Events are Y-013 envelopes from the
`fabric` component (`rate.message_admitted`,
`rate.message_throttled`, `rate.node_throttled`,
`rate.throttle_lifted`) — names, classes, sizes,
and counts only, never payload content.

## The law as data

`rate_discipline_policy` v1 carries the
discipline: `oversized_allowed` is always
`false`, `throttle_on_quota` is always `true`,
and every quota, bound, window, and cooldown is
a positive, bounded number. The default policy —
a policy, not a fact about a LAN — is a 60-second
window, 240 messages (120 gossip, 60 tasks), 1
MiB of admitted bytes, a 64 KiB single-message
cap, and a 30-second cooldown. A policy naming
weaker discipline fails validation.

## Honesty

The door is an in-process judgement:
`simulated: true` is a schema constant on its
report, no socket is opened, and no LAN is
crossed. What is proven is the quota, size, and
throttle law on the contract shapes.

## Not built here

- Store-and-forward for a sleeping node, with a
  TTL, is built at Y-061
  (`docs/STORE_AND_FORWARD.md`); real carriage on
  the home LAN —
  this door judging real wire traffic — is Y-062;
  hardware proof is Part VIII's.
- Fabric-wide rate observability — message rates,
  convergence, and drops on the operator's door —
  is built at Y-064
  (`docs/FABRIC_OBSERVABILITY.md`); this slice's
  report is one peer's own standing.
- No node-agent seam: as with the registry
  (Y-053), gossip (Y-055), CRDTs (Y-056), the task
  channel (Y-057), partitions (Y-058), and
  provenance (Y-059), the agent gains rate
  discipline at real carriage (Y-062), not before.
- Throttling is per-peer and temporary by design:
  there is no mesh-wide ban, no reputation score,
  and no permanent exile in this slice — a node's
  standing at one door says nothing about another
  peer's door.
