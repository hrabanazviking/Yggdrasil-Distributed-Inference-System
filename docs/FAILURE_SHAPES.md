# Failure Shapes (Y-032)

`src/yggdrasil/failure_shapes.py` — the node's accidents,
sorted: **engine crash, OOM, thermal throttle, model
corrupt** — each detected, contained, and reported
distinctly, because the four need four different responses
and a generic `failed` would take the wrong medicine for
three of them.

## The four shapes

| Shape | Y-007 name(s) | Detected from | Containment |
|---|---|---|---|
| `engine_crash` | `engine_error`, `engine_unreachable` | a task result / sandbox report / exception scoped to an engine | that engine alone is **quarantined** (`engine_available()` False) until cleared by name (`clear_engine()`); the node and its other engines stay up |
| `oom` | `oom`; a `MemoryError`; readings with memory fully consumed (fraction ≥ 0.99) | task doors + `check_memory()` | the task is terminated (its sandbox result is the record), `oom_active` latches and load shedding is recommended until a measured reading shows headroom (`failure.oom_cleared`) |
| `thermal_throttle` | `thermal_shed`; a governor verdict carrying `thermal` / `thermal_hard` | `check_thermal()` + task doors | the governor's own **load shed** (Y-028), named as the failure it is; one incident when throttling starts (severity `warning` at the shed line, `critical` at the hard line), `failure.thermal_cleared` when it ends |
| `model_corrupt` | `model_hash_mismatch`; a store verification of `corrupt` or `missing` | `check_models()` (the store's re-hash, Y-026) + task doors | the model is **quarantined** (`model_available()` False) and, when resident in a wired lifecycle, **unloaded** through it (`model_quarantined_and_unloaded`); an in-use model that refuses unload stays quarantined with `unloaded: false` in the evidence |

Codes outside the four — `queue_full`,
`deadline_unmeetable`, `cancelled_by_caller`,
`model_not_present`, `no_capable_node` — are outcomes, not
node failures: the ingest doors return `None` for them and
no incident is recorded. A completed result is never an
incident.

## Detection doors

1. **Polling** — `check_all()` (models → thermal → memory,
   in that order, so one check can report several shapes,
   each under its own name): the wired model store is
   re-hashed, the wired governor's verdict is read, memory
   readings come from the call or the governor. A component
   that is not wired is not polled, and its shape is not
   claimed.
2. **Task doors** — `ingest_result()` (a ResultEnvelope's
   provenance names the engine/model),
   `ingest_sandbox_report()` (the Y-029 report's recorded
   code), and `ingest_exception()` (by `error_code`, by type
   for `MemoryError`, by caller-named engine scope
   otherwise; an unscoped, uncoded exception is not a
   shape).

No spam: a known corruption (same model, status, pinned
and actual hash) and an ongoing throttle/OOM latch record
**one** incident, not one per check. An unknown (null)
reading clears nothing.

Restoration is reported, never assumed: a quarantined
model found verified again is un-quarantined with a
`failure.model_restored` event (restoring the pinned bytes
is the operator's act); an engine's quarantine lifts only
through `clear_engine()`.

## The record

Every incident is a validated `failure_incident` v1
(`contracts/schemas/failure_incident.schema.json`): seq,
shape, Y-007 code, source (`engine` / `sandbox` /
`model_store` / `governor` / `readings`), subject, task id,
severity, containment, and an evidence block (thermal,
memory fraction/bytes, expected/actual hash, store status,
residency/unload — null where a shape does not measure
that). `report()` answers the validated `failure_report`
v1: totals `by_shape`, the active quarantines and latches,
every incident in order, and `uncontained` (0 by
construction — every shape here records its containment;
the field stays so a future uncontained shape must say so
in numbers). Detections, quarantines, and clearings are
also Y-013 EventEnvelopes (`failure.<shape>`,
`failure.model_restored`, `failure.engine_cleared`,
`failure.thermal_cleared`, `failure.oom_cleared`) carrying
identifiers, shapes, codes, and containment only — never
payload content.

## Node agent

An agent built with `failure_monitor=` grows:

- `execute_task` feeds every finished sandbox report to
  the monitor before returning it — a crashed engine or a
  corrupt model is contained as part of the run's own
  bookkeeping, and the task's verdict is unchanged;
- `check_failures(readings=None)` polls through the
  monitor and returns the report; `failure_report()`
  returns the current report without polling;
- `status()` carries the full failure report under
  `failures`. With no monitor wired, `failure_report()` and
  `status()["failures"]` are `None` and `check_failures`
  refuses by name, exactly as the other unwired seams do.

## Not built here

Containment is bookkeeping with teeth at the seams this
tree owns — quarantines the router (Part IV) and operator
can read, a resident corrupt model actually unloaded, the
governor's shed named. This module **kills no engine
process** (no process client exists in the tree), confines
no memory at OS level, and restarts nothing — process
supervision is the deployment's (systemd) and later
slices'. No hardware claim: the suite's stores, readings,
and governors are fixtures on this VM; detection on named
hardware belongs to the citizens (Y-033 – Y-035), and
power/thermal telemetry has landed at Y-044 (measured
readings and governor citations; live runs on named
hardware belong to Part VIII — see POWER_TELEMETRY.md).
