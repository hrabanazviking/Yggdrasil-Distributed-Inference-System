# The Pi Citizen — Y-033

*Part II's first real citizen: the full node agent on
Mimir-class ARM, in `src/yggdrasil/pi_citizen.py`, wearing a
profile that lives as data in `profiles/pi_citizen.json`
(`citizen_profile` v1, registered in the Y-016 manifest). The
laptop citizen is Y-034 (LAPTOP_CITIZEN.md) and the VM
citizen is Y-035.*

## The machine, measured

Mimir is the house's Raspberry Pi: device `purpleviking1`,
read directly on 2026-10-31 (~03:12 EDT) before this slice
was built, and used — labelled as fixture readings, so the
in-tree runs stay SIMULATED — as the shape the acceptance
tests are built from:

| Fact | Measured value |
|---|---|
| Board | Raspberry Pi 5 Model B Rev 1.1 (`/proc/cpuinfo`) |
| Architecture / cores | aarch64, 4 |
| RAM | MemTotal 16,598,576 kB ≈ 15.83 GiB (14,540 MiB available at reading) |
| Thermal | zone0 41.9 °C |
| Disk | 202 G free of 469 G |
| Python | 3.11.2 (jsonschema and pytest present) |
| Samtal | `samtal.service` **active** (the village lives here) |
| Aurelia | listening on :8001 |
| Kiosk | **not** listening on :8099 (off, as it stays while Aesir work runs) |
| Home Assistant / yrsa-voice | inactive (stopped 2026-10-30, operator's ruling) |
| Hailo | **no `/dev/hailo0`** and no Hailo device node at reading; a `/usr/share/hailo-ollama` software tree exists — software on disk is not a device |
| Yggdrasil service | not installed (`yggdrasil-node.service`: no files found) |

## The profile (data, not code)

`profiles/pi_citizen.json` names what a node must measure to
wear the Pi hat, and the terms on which it shares the machine:

- accepted arches `aarch64` / `armv7l`; at least 4 cores and
  3.5 GiB RAM measured;
- the Pi governor policy: thermal shed 75 °C / resume 67 °C /
  hard 85 °C, memory ceiling 0.75, CPU ceiling 0.85 — an
  operator's policy, labelled as such, never a measured fact;
- a **neighbour reserve of 2.0 GiB**: the governor ceiling
  must leave at least that much of the measured RAM outside
  the citizen's own share;
- 2 model slots;
- the neighbour list — Samtal (service), Aurelia (port 8001),
  the kiosk (port 8099), the Hailo device (`/dev/hailo0`) —
  each with the observation key a caller reports its state by;
- `hailo_rule: never_beyond_aesir_ledger`, as a constant the
  schema enforces.

## Evaluation: the citizen report

`evaluate_pi_citizen(census, device_class=…, neighbours=…)`:

- takes a real Y-022 census and a caller-measured neighbour
  observation (this module holds no process or network
  client — deployment wiring reads `systemctl`, listeners,
  and device nodes, and hands the states in; an unshaped or
  unknown observation is refused by name, a missing one reads
  `unknown`, never an invented calm);
- gates on device class, ARM architecture, cores, RAM, and
  the reserve the ceiling actually leaves
  (`ram × (1 − 0.75)`; on Mimir's measured RAM, ≈ 3.96 GiB);
- records every neighbour as **observed** with
  `action_taken: "none"` — the coexistence law in data. The
  citizen never starts, stops, or signals Samtal, Aurelia, or
  the kiosk; it opens no socket (the agent is local-only
  until Part III), so their ports are theirs; Samtal being
  active does not gate readiness and Samtal being quiet does
  not either — coexistence is the reserve plus untouched
  neighbours, not the house rearranging itself;
- answers a validated `citizen_report` v1 whose
  `hailo_claim` is `absent` (Mimir's measured truth at this
  slice), `detected_unproven` when a device node is detected
  with `proven: false`, or `proven_by_aesir_ledger` only when
  the census itself carries `proven: true` — a grade only
  Aesir's ledger feeding (Y-024) produces. The citizen
  repeats a grade; it never raises one.

## The factory: the whole agent, wired

`build_pi_citizen_agent(config, store_root=…)` refuses a
machine whose evaluation is not ready — an x86 or undersized
node is not dressed as a Pi citizen — and then builds the
full Part II stack under the profile: census collector,
the profile's governor, a hash-pinned model store (Y-026),
a model lifecycle (Y-027) whose RAM pool is the measured
census RAM scaled by the governor's memory fraction (the
reserve binds the budgets, not only the report; the VRAM
pool is the census's own — absent on the Pi, read as 0),
the task sandbox (Y-029), the local queue (Y-031), and the
failure monitor (Y-032) — all handed to the node agent
(Y-021 grown): `status()` carries the citizen report under
`citizen`, and an agent built without a citizen carries
`citizen: None`, exactly as before.

The acceptance run exercises the built citizen end to end:
a Pi-class identity starts and joins, a hash-pinned fixture
model loads, a queued task runs through the sandbox behind
the leases, a heartbeat is served, and the failure report
reads zero incidents. The model loader is Y-027's
SIMULATED loader — residency is bookkeeping; no weights move
and no engine on the Pi is claimed.

## The live run on Mimir

Run on 2026-10-31 (~03:40–03:55 EDT) against the pushed
slice commit (`33d5ff4`), cloned from
`origin/yrsa/growing-the-tree` into a scratch directory on
purpleviking1 — scratch only: nothing was installed, no
service was enabled, and the run's identity, store, and keys
lived under `/tmp` and die with it.

**The citizen run itself** (system census probes — nothing
injected, `simulated: false`): the factory evaluated Mimir
**ready**, every gating check passing. The live census read
aarch64, 4 cores, "Raspberry Pi 5 Model B Rev 1.1",
15.83 GiB RAM, thermal 49.0 °C at that moment, accelerators
`[{"kind": "none", "proven": false}]`; the reserve computed
3.958 GiB. The neighbour observation, measured live by the
runner (systemctl / listener / device-node reads, handed in
as data): Samtal **active**, Aurelia **listening** (:8001),
kiosk **not listening** (:8099), Hailo **absent** — every
neighbour's `action_taken` remained `none`, and
`hailo_claim` is `absent`, exactly as the standing rule
requires. The built citizen then started on its freshly
provisioned Pi-class identity, joined, loaded the
hash-pinned fixture model, served a queued task end to end
(`completed`, text "live pi citizen answer"), answered a
heartbeat `present`, and closed with **0 failure incidents**.

**The test suite on ARM**: the whole 404-test suite was run
on the Pi in three groups — 403 passed and **1 failed**:
`test_governor.py::test_system_readings_read_this_machine_honestly`,
a Y-028 test that hard-codes the build VM's truth ("this VM
exposes no thermal sensor, so thermal reads None"). On the
Pi, which has a sensor, `system_readings()` correctly read
46.3 °C and the VM-bound assertion failed. That is a test
defect, not a governor defect — found by this slice's
on-hardware run and fixed in the addendum commit: the test
now takes the census probe for the same machine as its
oracle (None where no sensor exists, a matching real number
where one does), green on the VM and re-verified on Mimir.
The citizen's own 12 tests pass on ARM (6.05 s) as on the VM.

## Not built / not claimed here

- No systemd service is installed on Mimir by this slice:
  enabling `deploy/yggdrasil-node.service` there stays the
  operator's act, with identity provisioning (Y-011) done by
  hand first.
- No Hailo capability of any kind (see the measured absence
  above); the Hailo citizen story is Y-168, bounded by
  Aesir's ledger as always.
- No fabric presence: joining is still the local act of
  Y-021 until Part III.
- Power/thermal telemetry acting on real heat is Y-044;
  this slice's governor reads the seams it is handed.
