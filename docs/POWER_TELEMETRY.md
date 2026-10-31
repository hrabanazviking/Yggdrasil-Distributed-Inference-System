# Power and Thermal Telemetry — the governor's decisions cite them (Y-044)

The governor (Y-028) has judged heat since it landed, but its
thermal reading was an anonymous number: no zones, no watts,
no provenance, and no way for a verdict to say *what it rested
on*. This slice (`src/yggdrasil/power_telemetry.py`) is the
node's senses for heat and power, wired into the two citizens
the roadmap names — the Pi (Y-033) and the laptop (Y-034) —
and the citation that ties a governor verdict to them.

## What is measured, and how

`system_power_snapshot()` reads one real machine from files
only — no process client, so the Part I gate's banned-import
scan stays green:

- **Thermal zones** — `/sys/class/thermal/thermal_zone*/`:
  each zone's own `type` name and its millidegree `temp`.
- **Power supplies** — `/sys/class/power_supply/*/`: kind
  (battery / mains / usb / other), status, voltage, current,
  power, charge capacity, micro-units converted. A supply
  that publishes voltage and current but no power gets its
  watts as their product — arithmetic on measured values.
- **Board power rails** — `/sys/class/hwmon/*/power*_input`,
  summed, used only when no supply publishes its own power.
- **Energy counters** — `/sys/class/powercap/*/energy_uj`
  (RAPL-class totals, matched by name across samples).

A value no file offers is **null**, never invented. This VM
exposes no thermal zone and no power supply; its snapshot is
honestly empty, and the suite asserts exactly that against
the real machine.

## Readings, rates, and the report

A `PowerTelemetry` collector turns snapshots into validated
`power_telemetry_reading` v1 records: the zones, the hottest
zone by name, the supplies, the whole-machine watts, and
`cpu_power_w` — a *rate* derived between two samples as the
summed energy-counter delta over the measured elapsed
seconds. The first sample, a zero-length interval, and a
counter that goes backwards (wrap or reset) each yield null:
a rate is never fabricated from one reading.

The `power_telemetry_report` v1 keeps the sampled window in
numbers: sample count, peaks, mean power, the thermal slope
across the window, the energy consumed between the first and
last samples, and staleness told plainly — a report whose
newest sample is older than `stale_after_s` (default 120 s,
an operator's policy, not a fact) says `stale: true`. Old
heat is never dressed as present heat. Every sample is also
a Y-013 `power_telemetry.sampled` event (measured numbers
and counts only).

Snapshots are injectable: tests and factories hand in a
snapshot callable and the readings say `source: "injected"`;
the system provider says `source: "system_files"`. A
factory's `simulated` flag is carried into every reading
verbatim.

## The governor's decisions cite them

`cite()` pairs a `GovernorState` or a `GovernorDecision`
(Y-028, auto-detected) with the newest telemetry sample and
answers a validated `governor_citation` v1: the verdict's
reasons and shedding flag beside the reading's moment, its
age in seconds, its staleness, the hottest zone by name, and
the watts. A citation taken before any sample says so —
every telemetry field null, `stale: true` — rather than
sampling unasked or inventing a reading.

In the **Pi and laptop factories**, when no `readings`
provider is handed in, the governor's thermal reading *is*
the telemetry's hottest zone, sampled at evaluation time
(memory and CPU still come from the governor's own Y-028
system readings) — so the verdict and its citation rest on
the same measurement, not on two readings that happen to
agree. A factory may be handed a telemetry snapshot through
`power_snapshot=`, which is how the suite proves the wiring
with labelled simulated readings.

## Node agent

A `NodeAgent` built with `power_telemetry=` gains
`sample_power_telemetry()`, `power_telemetry_report()`, and
`governor_citation()` (the governor's *last* state cited —
no verdict is forced by asking, and the citation is None
until the governor has evaluated); `status()["power_
telemetry"]` carries the report. Without telemetry wired,
the agent reports None, exactly as before. The VM and phone
factories do not wire it at this slice — their profiles name
no power source; the module itself is class-agnostic, and
any factory may wire it when its hardware offers the files.

## Not built here / honesty

- No live power/thermal run on Mimir or purplelongship
  under this module is claimed: the in-tree acceptance uses
  injected snapshots shaped from those machines' measured
  facts and a fake sysfs root, and says so. Live runs on the
  named hardware belong to the Part VIII hardware road
  (the soak and thermal drills, Y-174/Y-176, are where real
  heat is acted on over time).
- The telemetry *informs* the governor; it does not widen
  its authority. Shedding still faces forward only, resident
  shedding at the hard line is still only recommended
  (Y-028), and unloading stays the lifecycle's act (Y-027).
- Battery policy for the phone citizen stays Y-036's own
  governor data; this slice reads power supplies as facts,
  and sets no battery policy.
- Fabric carriage of telemetry to peers is Part III;
  the operator's one-dashboard view of it is Y-202.
