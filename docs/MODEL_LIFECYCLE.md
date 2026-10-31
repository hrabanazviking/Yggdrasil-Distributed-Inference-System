# Model Lifecycle (Y-027)

`src/yggdrasil/model_lifecycle.py` — which of the models a node
*holds* (Y-026's hash-verified store) are *resident*: loaded
into slots under the node's memory budgets, and what it costs,
in measured time, to change that residency.

## Slots and budgets

The node offers `max_slots` slots and two memory pools: the
accelerator pool (VRAM budget) and the system pool (RAM
budget). A loaded model occupies one slot and charges its
store-pinned `size_bytes` against one pool.

- Placement `"auto"` (the default) follows the
  accelerator-first law: the accelerator pool when the node
  has one the model could ever fit, else the system pool.
- An explicit placement (`"accelerator"` / `"system"`) is
  honoured exactly — never silently re-placed.
- Budgets are given directly, or derived from a Y-022 census
  with `budgets_from_census()` (`vram_gb` / `ram_gb` at 1024³;
  a census with no `vram_gb` means an accelerator budget of
  0 — absence read as absence).

## Loading is gated on presence

A model loads only when the store verifies it *now* (Y-026
re-hashes on every ask). A corrupt, missing, or unregistered
model is refused by name with its true status. Loading an
already-loaded model is idempotent: the resident record comes
back, its measured cost unchanged.

## Eviction, swaps, and the costs Verðandi reads

When a load cannot fit, the manager evicts least-recently-used
residents of that pool that are evictable — not pinned, not in
use — measuring each unload. A load that displaced at least
one resident is a **swap**; the ledger counts swaps,
evictions, and total measured swap time (evict unloads + the
incoming load). When nothing evictable can make room, the load
is refused by name, with the model's size, the pool's budget,
its usage, and the free slots in the message.

`acquire()` / `release()` mark a resident model in use (the
Y-029 sandbox will hold one per running task): an in-use model
is never evicted, and an explicit `unload()` of one is refused
until released. `pin=True` at load guards eviction only; the
operator may always unload a pinned model by name.

`report()` — also named `verdandi_facts()`, the name Part IV
asks by — answers the validated `model_lifecycle_report` v1
shape: budgets, pool usage, the resident records
(`loaded_model` v1), and one `model_swap_fact` v1 per model
the store knows, loaded or not. `swap_in_cost_ms` is the last
**measured** load cost on this node, or `null` when the model
has never been loaded here: a cost never measured is unknown,
never estimated. Y-087's warmth math consumes exactly this.

## The loader seam, and its honesty

Moving weights into an engine is the loader's act, injected
at construction like the adapters' transports: `load(name, *,
size_bytes, placement) -> handle`, `unload(handle, *, name,
placement)`. The default is `SimulatedLoader` — an in-process
recorder that moves no weights. Every report carries
`loader_simulated` from the loader's own `simulated`
attribute, and every lifecycle event states it too, so a
residency built on the simulated loader is never dressed as a
hardware fact. Loader failures propagate as
`ModelLifecycleError` and consume no slot.

Costs are timer readings around the loader calls
(`time.perf_counter` by default; the timer is injectable, so
the suite's costs are exact). Every load, unload, eviction,
swap, and refusal is a Y-013 `EventEnvelope` from the
`node_agent` component in the manager's `events` list.

## Node agent

A `NodeAgent` built with `model_lifecycle=` delegates
`load_model()` (started/joined only) and `unload_model()`
(also while draining), surfaces lifecycle refusals as agent
errors, and reports the resident models in `health()`'s
`models_loaded` and in `status()`. Without a lifecycle wired,
both stay empty, exactly as before.

## Not built here

The task sandbox has landed (Y-029 — `execute_task` holds
an acquire here for exactly the run's duration, and releases
it whatever the outcome); quantisation fitness has landed
(Y-038 — a lifecycle built with `fitness_checker=` refuses a
quant unfit for this node, by name with every reason, after
presence verifies and before the loader is asked; see
QUANT_FITNESS.md); the resource governor has landed (Y-028 — it
only ever *recommends* resident shedding at its hard thermal
line; unloading remains this manager's act, under its own
in-use protections), no routing (Part IV reads the
facts; nothing here places a task), and no hardware claim:
the suite's loader is SIMULATED and its model files are the
store's SIMULATED fixtures — residency and swap-cost
*accounting* is proven, no real model's load time on any
named device is claimed.
