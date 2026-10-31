# Graceful Degradation (Y-040)

`src/yggdrasil/degradation.py` — what a node does when it
loses its accelerator: it keeps serving on CPU, and it
*declares* the reduced capability in validated data the
moment a census refresh measures the loss. No silent lies
in either direction — a degraded node is never advertised
as whole, and a CPU-only-by-design node is never called
degraded.

## Baseline vs present

A `DegradationTracker` watches the node's censuses (Y-022):

- The **baseline** is every accelerator kind the node has
  ever measured — its known full capability — plus the
  largest VRAM figure it has ever measured.
- The **present** is the latest census's kinds and VRAM
  figure (`null` when the census measured none — absence
  read as absence).
- Kinds in the baseline but not the present are **lost**,
  and are named in every state and report.
- A kind appearing for the first time *grows* the baseline:
  gaining hardware is an upgrade, never a degradation.

## The four statuses (`degradation_state` v1)

`contracts/schemas/degradation_state.schema.json`:

- `full` — the baseline is present; nothing is lost.
- `degraded_partial` — some kinds are lost, some remain;
  the node still accelerates, on fewer devices.
- `degraded_cpu_only` — every baseline kind is lost; the
  node serves on CPU alone, and `serving_on_cpu_only` is
  `true` in so many words.
- `cpu_only_by_design` — the baseline is empty. A VM
  citizen or a phone shape is CPU-only by *design*, and
  this slice never dresses that as a loss. A tracker that
  has observed nothing yet also reports this status with
  `observations: 0` — the tell that nothing has been
  measured at all.

The state also carries `degraded_since` (the moment the
current degradation began, `null` while whole), the
observation and transition counts, and the resident impact
(below).

## Resident impact, judged by format

At each observation the tracker may be handed the model
lifecycle's resident records (Y-027) and the store's
entries (Y-026). Every model resident on the *accelerator*
pool is sorted by its pinned format against the Y-038
fitness policy's own format table — read from the policy,
never duplicated here:

- a format needing no accelerator (gguf, ggml) →
  `cpu_fallback`: it can keep serving from the system pool;
- a format requiring a kind no longer present →
  `stranded`: it cannot run on this node until the
  accelerator returns, and it is named;
- a format whose accelerator is still present →
  `accelerator`: unaffected;
- a format the policy does not name → `stranded`, never
  assumed CPU-servable (the policy's own
  `on_unknown_format: refuse` law).

Stranded models that are **in use** are additionally named
in `stranded_in_use`: a running task's model is never
yanked from under it. Models resident in the system pool
are untouched by the assessment — they already serve on
CPU.

## Transitions, history, events

Every change of status or of the lost set is a recorded
transition in the `degradation_report` v1
(`contracts/schemas/degradation_report.schema.json`):
from/to status, the kinds lost and regained, the moment.
Crossing into a degraded status counts as a degradation;
crossing back to `full` counts as a recovery. Each crossing
is also a validated Y-013 EventEnvelope from the
`node_agent` component — `degradation.entered`,
`degradation.changed` (the lost set changed while
degraded), `degradation.recovered` — kinds and statuses
only.

Recovery restores the *declaration* at once. It does not
reload anything: residency is the lifecycle's act (Y-027),
and a model unloaded while stranded stays unloaded until
something loads it again. That is stated, not hidden.

## Node agent

An agent built with `degradation_tracker=` grows:

- Every `refresh_census()` feeds the fresh census to the
  tracker — with the lifecycle's resident records and the
  store's entries when those are wired — and the refresh
  result gains `degradation` (the validated state) and
  `degradation_acts`.
- When the fresh state is degraded and a lifecycle is
  wired, the agent **acts** on the assessment through the
  lifecycle: `cpu_fallback` residents are re-placed into
  the system pool (unload + load at `system` placement,
  with Y-027's measured costs, as always); `stranded`
  residents that are not in use are unloaded; in-use
  residents — fallback or stranded — are left running and
  named under `deferred_in_use`. An act that fails is
  named under `failures`. Nothing is re-placed silently:
  the acts ride in the refresh result, and the lifecycle's
  own `model.loaded` / `model.unloaded` events record the
  moves.
- `degradation_state()` / `degradation_report()` read the
  ledger back; `status()` carries the state under
  `degradation`. With no tracker wired, all of this is
  exactly as before: a `None` state, and a refresh result
  with no degradation keys — the agent judges no loss it
  was never asked to watch.

All four citizen factories (Pi, laptop, VM, phone) wire a
tracker and feed it their factory-measured census at
build, so the baseline is the machine's real capability
from the first refresh: the Pi and VM citizens open
`cpu_only_by_design`, the laptop citizen opens `full`
with its measured CUDA baseline.

## Not built here

No engine is restarted, no driver is touched, and no
attempt is made to *revive* a lost accelerator — this
slice declares and adapts; revival is operations, and a
device that returns is handled by the recovery path above.
No task is migrated to another node (that is the fabric's
and Verðandi's work, Parts III–IV), no in-flight task is
interrupted, and no model is reloaded at recovery.
Degradation of *engines* (an accelerator present but its
engine gone) is the failure monitor's country (Y-032);
this slice is the hardware-capability loss the census can
measure. Nothing here promises the Hailo chip anything: a
`hailo` loss would be declared exactly like a `cuda` one,
from the census's own measurement, no more and no less.

## Honesty

The tracker judges the censuses it is handed; on a real
node those come from the real probes, so a real loss will
be declared by the same code the fixtures exercise. But no
real accelerator has died under a live citizen for this
slice: the acceptance losses are probe fixtures shaped
like losses, labelled SIMULATED in the tests, and a
hardware-loss run on named hardware is **NOT PROVEN**
until one happens and is recorded.
