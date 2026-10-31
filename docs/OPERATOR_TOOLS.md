# Node Operator Tools (Y-042)

*The operator's door (Y-017) opened read-only — it could
list the tree, but it could not touch it, and its own doc
said the verbs would arrive with the machinery they act on.
The machinery has arrived ring by ring through Part II.
This slice grows the door its hand: drain, pause, inspect,
and retire a node, from the CLI.*

## The verbs, on the agent

Two verbs were already the agent's (`start`, `join`,
`drain`, `stop`). Y-042 adds the operator's states to the
same lifecycle ledger in `src/yggdrasil/node_agent.py`:

- **pause** — `joined → paused`. The brake that is not a
  goodbye: the node stays *present* — identity, models,
  queue, and in-flight leases all kept, and a held lease
  may finish (`end_task` stays open) — but it takes
  nothing new: no new leases, no new held work, no model
  fetches. Health names it exactly as it names a drain:
  `governor: {shedding: true, reason: "paused"}`.
- **resume** — `paused → joined`, the only way back.
- **retire** — from `started`, `joined`, `paused`,
  `draining`, or `stopped`, to the terminal state
  `retired`. Refused while leases are in flight (drain and
  end them first — the same discipline as stop), refused
  from `created` (a node that never started has nothing to
  retire). A retired agent *cannot* be restarted:
  `start()`'s allowed origins stay `created`/`stopped`.
  A node that is wanted again is re-provisioned and started
  as a new agent, so the old one's book — journal, events,
  operator ledger — is never silently rewritten.
  `status()` records `retired_at`. Retirement is a
  lifecycle farewell, not a wipe: keys, journal, and the
  model store on disk are the operator's property and are
  left exactly where they lie.
- **drain** (unchanged verb, widened door) — now also
  allowed from `paused`, as is `stop`: an operator who
  paused a node need not resume it to shed or stop it.

The Y-030 heartbeat grows with the lifecycle: the
`node_health` v1 enum now names all seven states, a paused
node beats as `present`, and a retired node beats as
`gone`. Idle generosity (Y-041) withdraws for both new
states through its existing rule — neither is `joined`, so
neither advertises spare.

## The ledger — `src/yggdrasil/operator_tools.py`

`OperatorTools` wraps one agent in this process. Every
verb ordered through it lands as a validated
`operator_action` v1:

- who ordered it — `actor`, a plain operator name recorded
  on every action (default `"operator"`). It is a name in a
  local ledger, never a credential: holding these tools
  *is* the authority, because whoever can run code in the
  node's own process already owns the node;
- the operator's stated `reason`, when one was given;
- the states it moved between (`from_state` → `to_state`);
- or the **refusal** that met it, with the node's own words
  kept. A refusal is recorded *first* and only then raised
  as `OperatorToolsError` — a refused drain is part of the
  node's history, not a vanished keystroke.

`report()` is the validated `operator_report` v1: totals,
applied and refused counted apart, counts by verb,
retirement stated outright, and the full history.
Every action is also emitted as a Y-013 EventEnvelope from
the `operator_cli` component — the component Y-013's closed
vocabulary has carried since the contract was written —
`operator.drained` / `operator.paused` / `operator.resumed`
/ `operator.inspected` / `operator.retired`, and
`operator.refused` for a refusal. Events carry action,
outcome, and state names only: no reason text, no payload
content.

## Inspect

`inspect()` assembles one validated `node_inspection` v1
from the agent's own records: lifecycle state and liveness,
device class, whether the identity has verified, the
leases in flight, queue depth, models loaded and present,
the governor's shedding verdict and reason, the count of
*active* failure incidents (quarantines and latches, from
Y-032's report), journal entries counted (Y-039), the last
generosity verdict (Y-041), the degradation status
(Y-040), whether a census has ever been measured (Y-022),
and the citizen's readiness where a citizen is wired.

A component that is not wired answers `null` — unwired is
reported, never dressed as a calm zero. A bare agent
inspects with `queue_depth: null` beside a citizen's honest
`0`, and the difference is the point. Inspecting changes
nothing on the node; the asking itself is ledgered like
every other verb.

## From the CLI

```
yggdrasil node drain [--reason "…"]
yggdrasil node pause [--reason "…"]
yggdrasil node resume
yggdrasil node inspect
yggdrasil node retire [--reason "…"]
```

(all take `--json` like every door command). The commands
drive an `OperatorTools` handed to `cli.main(tools=…)` by
the caller — the deployment wiring that owns a live agent.
An applied verb prints its ledgered action and exits 0; a
refused verb prints the node's refusal on stderr and exits
2, with the refusal standing in the ledger all the same.

## Honest limits

- **The tools act on one agent in this process.** There is
  no fabric yet (Part III), so no verb reaches across a
  network to a daemon running elsewhere, and none is
  claimed to. With no tools handed in, the `node`
  subcommands refuse by name (exit 2): there is deliberately
  **no demo agent** behind the verbs — draining a fake node
  would be a lie in the shape of a feature, and the door's
  simulated demo mesh stays read-only behind its own views.
- **The `simulated` flag follows the node, never the
  wish**: tools over a citizen built from injected probes
  record `simulated: true` (the citizen report's own flag);
  tools over a plain agent record `false`.
- **No remote authority is designed here.** Who may order
  a verb over the fabric — and how that order is signed —
  is Part III/VI country; this slice's ledger records the
  actor's name exactly as given and authenticates nothing.
