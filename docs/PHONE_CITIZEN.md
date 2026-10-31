# The Phone Citizen — Y-036

*Part II's fourth citizen: the full node agent's shape on
a phone — small models, a battery governor — in
`src/yggdrasil/phone_citizen.py`, wearing a profile that
lives as data in `profiles/phone_citizen.json`
(`citizen_profile` v1, registered in the Y-016 manifest;
this slice adds the schema's two optional phone fields,
`max_model_size_gb` and `battery_policy`). The Pi citizen
is Y-033, the laptop citizen is Y-034, and the VM citizen
is Y-035.*

> **On-device proof: NOT PROVEN.** No phone has run this
> citizen. Unlike Y-033 – Y-035, this slice has **no live
> addendum and none is claimed**: every acceptance run is
> injected-probe **SIMULATED** against a declared,
> representative phone shape. On-device proof stays
> recorded as NOT PROVEN until a real phone runs it.

## The shape, declared — not measured

There is no "machine, measured" table in this document,
because no phone was measured. The acceptance tests are
built from a **declared representative phone shape** —
chosen round numbers for a plausible modern phone class,
labelled SIMULATED wherever they appear:

| Fact | Declared SIMULATED shape (not a measurement) |
|---|---|
| Architecture / cores | aarch64, 8 |
| CPU model string | "Representative phone-class ARM SoC (SIMULATED shape)" |
| RAM | 6.0 GiB |
| Disk free | 32.0 GiB |
| Thermal | 36.0 °C |
| Accelerators | exactly one entry, kind `none` |
| VRAM | no reading |
| Neighbours | phone OS active, cellular radio present |
| Battery | 80 %, not charging (the healthy fixture) |

No measured fact about any real phone — any model, any
chip, any battery size, any thermal behaviour — is stated
anywhere in this slice. When a real phone runs the
citizen, its own measured census replaces this shape and
this document gains the live-run addendum the other
citizens carry; until then the shape above is a fixture,
nothing more.

## The profile (data, not code)

`profiles/phone_citizen.json` names what a node must
measure to wear the phone hat, and the terms on which it
shares a device whose first job is being the user's phone:

- accepted arches `aarch64` / `arm64`; at least 4 cores
  and 4.0 GiB RAM measured — modest floors, fitting a
  phone rather than a workstation;
- the phone governor policy: thermal shed 45 °C / resume
  38 °C / hard 50 °C, memory ceiling 0.50, CPU ceiling
  0.50 — half of memory and of CPU left outside the
  citizen's share; an operator's policy, labelled as
  such, never a measured fact about any phone's thermals;
- a **phone reserve of 2.0 GiB**: the governor ceiling
  must leave at least that much of the measured RAM
  outside the citizen's own share (on the SIMULATED
  shape's 6.0 GiB, the 0.50 ceiling leaves 3.0 GiB);
- **1 model slot**, and the **small-model ceiling**:
  `max_model_size_gb` 1.5 GiB as data — and enforced, not
  just written: `validate_profile` refuses a phone
  profile that names no ceiling, and the evaluation's
  `small_model_ceiling` gate requires the ceiling to fit
  inside the RAM pool the governor fraction actually
  leaves (on a 2 GiB shape the pool is 1.0 GiB and the
  gate fails by name);
- the **battery policy as data**: `battery_policy`
  {shed below 20 %, critical below 10 %} — refused by
  name when missing, and refused when the critical line
  is not below the shed line;
- the neighbour list — the phone OS (service) and the
  cellular radio (device) — each with the observation
  key a caller reports its state by;
- `hailo_rule: never_beyond_aesir_ledger`, as a constant
  the schema enforces (the SIMULATED shape's claim is
  `absent`).

## Evaluation: the citizen report

`evaluate_phone_citizen(census, device_class=…, neighbours=…, battery=…)`:

- takes a real Y-022 census and caller-measured neighbour
  and battery observations (this module holds no process
  or network client — deployment wiring on a phone would
  read the OS's own battery and session surfaces and hand
  the states in; an unshaped or unknown observation is
  refused by name, a missing one reads `unknown`, never
  an invented calm);
- gates on device class, ARM architecture, cores, RAM,
  the small-model ceiling fitting the RAM pool, the
  reserve the ceiling actually leaves, and the **battery
  governor's critical line**;
- records the battery verdict as **observed**
  (`battery_state`, computed by `battery_state_for`
  against the profile's battery policy): unplugged below
  the 20 % shed line the citizen is *shedding* (ready,
  but its governor refuses new work by policy); unplugged
  below the 10 % critical line the verdict is *critical*
  and the `battery_governor` gate **fails** — a phone
  about to die is not dressed as a ready citizen, and the
  factory refuses it by name. Charging relaxes the shed
  line to the critical line and no lower (15 % on the
  charger is within ceilings; 5 % on the charger sheds).
  Charging unknown reads conservatively as unplugged.
  **An unknown battery level is never read as full**: the
  state is `unknown`, the citizen claims no battery
  headroom, and the absence is recorded as absence;
- records `small_models_only` as **observed**:
  `model_size_allowed` is the discipline in code — a
  model at or under the 1.5 GiB ceiling is a phone model
  here; a larger one is refused for this citizen however
  well it runs on the Pi, laptop, or VM citizens;
- records `on_device_proof` as **observed**, in plain
  words: NOT PROVEN — no phone has run this citizen;
- records every neighbour as **observed** with
  `action_taken: "none"` — the coexistence law in data.
  The citizen never touches the phone OS or the radio;
  coexistence is the reserve plus the governor shedding
  the citizen's own load first, and calls and the user's
  own apps outrank the citizen;
- answers a validated `citizen_report` v1 whose
  `hailo_claim` comes from Y-033's helper — `absent`
  unless the census itself carries a Hailo entry, proven
  only on Aesir's ledger grade, never raised here. An
  NPU in the census does not gate readiness (proven in
  tests): the phone citizen's gates are class, shape,
  reserve, and battery — not accelerator dressing.

## The factory: the whole agent, wired

`build_phone_citizen_agent(config, store_root=…, battery=…)`
refuses a machine whose evaluation is not ready — an x86
machine, an undersized phone shape, or a critically low
unplugged battery is not dressed as a phone citizen — and
then builds the full Part II stack under the profile:
census collector carrying the caller's engine seam into
every refresh, the profile's governor, a hash-pinned
model store (Y-026), a model lifecycle (Y-027) whose
VRAM pool is the census's own — **zero** on the SIMULATED
shape, absence read as absence — and whose RAM pool is
the measured census RAM scaled by the governor's memory
fraction (the reserve binds the budgets, not only the
report), the task sandbox (Y-029), the local queue
(Y-031), and the failure monitor (Y-032) — all handed to
the node agent (Y-021 grown): `status()` carries the
citizen report under `citizen`, and an agent built
without a citizen carries `citizen: None`, exactly as
before.

The acceptance run exercises the built citizen end to
end **in simulation**: a phone-class identity starts and
joins, a hash-pinned fixture model under the small-model
ceiling loads, a queued task runs through the sandbox
behind the leases (under injected calm readings,
SIMULATED), a heartbeat is served, and the failure report
reads zero incidents. The model loader is Y-027's
SIMULATED loader — residency is bookkeeping; no weights
move, and no phone is involved anywhere in the run.

## No live run

There is deliberately no live-run section with results in
this document. The other citizens earned theirs by
running on their named hardware (Mimir, purplelongship,
this VM); no phone in the house has run this code, no
phone was measured, and nothing in this slice pretends
otherwise. The roadmap's own words for this slice say it
plainly: *built and simulated; on-device proof recorded
as NOT PROVEN until a phone runs it.* That is the state
of this slice, and the first real phone run — whenever it
happens — is what changes it, recorded as an addendum
like the other citizens'.

## Not built / not claimed here

- Model fetch duty (Y-037) is wired by this factory: the
  agent carries a `model_fetcher=` over its store (policy
  and transport are the factory's `fetch_policy=` /
  `fetch_transport=` seams; with no transport handed in, a
  fetch fails honestly as `no_transport_wired` — no real
  peer has carried a model for this tree yet; see
  MODEL_FETCH.md).

- Quantisation fitness (Y-038) is wired by this factory: the
  agent carries a `quant_fitness=` judge built over the
  factory's measured census, and the same judge gates its
  lifecycle — an unfit quant is refused at load, by name,
  before the loader is asked (see QUANT_FITNESS.md).

- **No on-device proof of any kind: NOT PROVEN.** No
  phone has run the citizen, been measured by it, or had
  its battery, thermal, or performance behaviour observed
  by this code. Every number in this slice's acceptance
  is SIMULATED against the declared shape above.
- No app or service is installed on any phone by this
  slice: packaging the agent for a phone OS (and its
  battery-optimisation exemptions, background limits, and
  store rules) is deployment work that does not exist
  yet, and is not claimed.
- No speed figure for any model on any device class: no
  inference ran in this slice, on a phone or anywhere.
- No accelerator capability of any kind: the SIMULATED
  shape carries none, an NPU's presence does not gate,
  and the Hailo claim is `absent` by the standing rule.
- No fabric presence: joining is still the local act of
  Y-021 until Part III.
- Power/thermal telemetry acting on real heat is Y-044;
  this slice's battery and thermal lines are policy data
  and injected readings, never a real phone's telemetry.
