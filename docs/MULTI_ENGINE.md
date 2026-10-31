# Multi-Engine Node — Aesir and Ollama side by side, slots accounted across engines (Y-045)

Until this slice a node could be *told about* several engines
(the census carries a list, Y-022), but nothing owned the fact
that they live on the **same node**, sharing the same slots
and the same memory. This slice (`src/yggdrasil/multi_engine.py`)
is that owner: `MultiEngineHost`.

## Side by side, each in its own words

The host holds the node's engine adapters (Y-023 – Y-025) by
their own `engine` names; two adapters claiming one name are
refused at construction. `census_engines()` answers every
hosted adapter's own validated discovery, in hosting order,
for the Y-022 collector's engines seam — Aesir's ledger grade
(`proven`, from AES-SRV-010 in its own ledger) and Ollama's
`unproven` grade ride side by side, neither averaged into the
other.

`census_models()` answers the *union* of the engines' model
lists for the collector's models seam. The report keeps each
engine's list separate: a model two engines both list is two
listings in the report and one name in the census — never
silently merged where the difference matters.

Discovery is asked fresh on every report and routing
decision. The host keeps no cached picture of an engine that
may have died.

## Slots accounted across engines

Residency still belongs to the one model lifecycle (Y-027):
its `max_slots` and its VRAM/RAM pools are the node's, shared.
The host adds the missing attribution — `assign(model, engine)`
loads the model through the lifecycle (hash-verified presence,
quant fitness, budgets, measured cost: all Y-027/Y-038's gates,
unchanged) and records a validated `engine_assignment` v1
naming the engine that residency serves.

- A model resident for one engine cannot be assigned to
  another — one residency, one engine; unassign it first.
  Re-assigning to its own engine is idempotent.
- Assigning a model the engine's own surface does not list is
  refused by name (`model_not_present`) before any slot is
  spent.
- When the shared slots run out, Y-027's eviction and refusal
  rules decide, across engines alike: an Ollama load can evict
  an Aesir residency under the same LRU law, a pinned resident
  blocks the load by name, and an evicted residency drops its
  assignment rather than leaving a record that claims a slot
  the model no longer holds.
- The report's per-engine `slots_used` always sums to the
  node's `slots.used`.

## Routing by the model's own facts

`run_task` picks the engine a task runs on, in this order:

1. an explicitly named engine (it must be hosted, and must
   list the task's model);
2. the model's assignment;
3. the one hosted engine that lists the model;
4. refusal — `model_not_present` when no hosted engine lists
   it, and an ambiguity refusal naming every candidate when
   several engines list an unassigned model.

Choosing between engines that could both serve is Verðandi's
judgement (Part IV); this host refuses to guess it. The
chosen adapter's own `run_task` serves the task — its
refusals (Aesir's resident-only rule, either engine's
unservable kinds) stay the engine's own, under its own Y-007
names — and the host counts each run against the engine that
ran it (completed / failed / refused, from the result
envelope's own status). Every assignment, unassignment, and
routing refusal is a Y-013 event (names and counts only).

## The report

`report()` answers the validated `multi_engine_report` v1:
per-engine fresh discovery, model list (an unreachable engine
lists nothing — its discovery already says why), slot share
and run counts; the shared slot ledger; every live
assignment; and `loader_simulated` carried from the lifecycle.

## Node agent

A `NodeAgent` built with `multi_engine=` delegates
`assign_model_to_engine()` (started/joined) and
`unassign_model()` (also while draining), and `status()`
carries the report under `multi_engine`. Without a host
wired, the agent accounts nothing across engines, exactly as
before.

## Not built here / honesty

- The host wires adapters it is *handed* — transports are
  deployment wiring (the socket-carrying seam of Y-023 –
  Y-025), so no citizen factory wires a host at this slice and
  no live Aesir or Ollama server is claimed here. The in-tree
  acceptance drives the real adapters over fake transports
  speaking Aesir's and Ollama's own wire shapes, with the
  lifecycle on its SIMULATED loader (`loader_simulated: true`
  in every fixture report).
- No routing intelligence: placement between engines that
  could both serve a model is Part IV (Verðandi). This host
  routes only on stated facts — an explicit name, an
  assignment, a single listing.
- The conformance kit every new adapter must pass is Y-046;
  the mixed-engine soak is Y-047.
