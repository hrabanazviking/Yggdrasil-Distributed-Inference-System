# The Resource Governor (Y-028)

`src/yggdrasil/governor.py` — the node's politeness, as code:
per-node ceilings on heat, memory, and CPU share, with
graceful refusal. A node sheds load *before* it cooks — by
refusing new work, by name — and never by killing work
already running.

## Ceilings as data

A `GovernorPolicy` (`governor_policy` schema v1) names:

- `thermal_shed_c` — the temperature at which shedding starts;
- `thermal_resume_c` — the lower temperature at which it may
  stop. The gap is **hysteresis**: a node hovering at the shed
  line does not flap between accepting and refusing. A policy
  whose resume line is not below its shed line is refused.
- `thermal_hard_c` (optional) — the temperature at which
  resident-model shedding is *recommended* (see below). Must
  sit above the shed line.
- `max_memory_fraction` — the largest fraction of total
  memory the node's work may occupy;
- `max_cpu_share` — the largest total share of the node's CPU
  new work may claim.

`default_policy()` is the operator's starting point (shed
80 °C, resume 72 °C, hard 90 °C, memory 0.9, CPU 0.9) — a
policy, not a measured fact about any machine.

## Readings are measured, never assumed

A reading set is `thermal_c`, `memory_used_bytes`,
`memory_total_bytes`, `cpu_share_used` — and any of them may
be `None`: **unknown**. An unknown reading disables only its
own check and is reported as `null` in the state; the
governor never treats a missing thermal sensor as a cool
machine (this VM exposes none — the census, Y-022, says the
same) and never invents a memory total.

The default provider, `system_readings()`, reads the real
machine from files and stdlib only: thermal from the census
probe set, memory from `/proc/meminfo` (MemTotal /
MemAvailable), CPU share as the 1-minute `/proc/loadavg` over
the core count. Tests and deployment wiring inject their own
readings callable.

## Verdicts and graceful refusal

`evaluate()` answers the validated `governor_state` v1: the
readings (with the derived `memory_fraction`), the reasons
that bite (`thermal` / `thermal_hard` / `memory` / `cpu`),
`shedding`, and a state of `within_ceilings`, `shedding`, or
`critical` (hard temperature reached). Thermal shedding
**latches**: once started it holds until the temperature
falls below the resume line. Transitions are recorded once —
`governor.shedding_started` / `governor.shedding_cleared`
Y-013 events from the `node_agent` component.

`admit()` answers the validated `governor_decision` v1 for
one new task: admitted, or refused by name — the reasons list
plus the **projected** memory fraction and CPU share the task
would have produced (current usage + the task's declared
footprint), so a refusal is a number, not a shrug. A task is
refused when the node is already shedding, or when its own
footprint would push a ceiling over. Every refusal is a
`governor.task_refused` event naming the task.

Shedding never touches work in flight: admission faces
forward only, nothing is cancelled, and at the hard
temperature the governor only *recommends* resident shedding
(`shed_resident_models_recommended: true`) — unloading stays
the lifecycle's act (Y-027), whose rules already protect
in-use models.

## Node agent

A `NodeAgent` built with `governor=` asks it inside
`begin_task(task_id, memory_bytes=…, cpu_share=…)` before
taking a lease: a refusal raises `NodeAgentError` naming the
governor's reasons and the projections, and no lease is
taken. `health()` reports the verdict in its `governor`
field — the governor's reasons, or `within_ceilings`, and
`draining` combined in front when both bite — and `status()`
carries the last evaluated `GovernorState` (no state is
forced by asking). Without a governor wired, the agent's
health keeps only drain's shedding flag, exactly as before.

## Not built here

The task sandbox has landed (Y-029 — it runs behind this
door: `execute_task` takes the lease, this governor's
admission rides inside it, and the run holds the lifecycle's
use leases), and the health endpoint has landed (Y-030 —
this verdict rides every heartbeat in full, as
`governor_state`; see HEALTH_ENDPOINT.md), and no hardware claim:
the suite's readings are injected fixtures, and the one real
reading taken — this VM's, by `system_readings()` in tests —
reports its thermal honestly unknown. Acting on real heat on
named hardware is Y-044.
