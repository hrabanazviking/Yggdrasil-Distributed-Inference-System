# The Laptop Citizen — Y-034

*Part II's second real citizen: the full node agent on
purplelongship-class x86 + CUDA, in
`src/yggdrasil/laptop_citizen.py`, wearing a profile that
lives as data in `profiles/laptop_citizen.json`
(`citizen_profile` v1, registered in the Y-016 manifest;
this slice adds the schema's two optional accelerator
fields, `required_accelerator_kind` and `min_vram_gb`).
Its engine is Project Aesir's GPU line, graded through
the census exactly as Y-024 built it. The Pi citizen is
Y-033; the VM citizen is Y-035.*

## The machine, measured

purplelongship is the house's older x86 laptop, read
directly on 2026-10-31 (~03:40 EDT) before this slice was
built, and used — labelled as fixture readings, so the
in-tree runs stay SIMULATED — as the shape the acceptance
tests are built from:

| Fact | Measured value |
|---|---|
| Host | purplelongship, user volmarr |
| CPU | AMD Ryzen 9 4900HS with Radeon Graphics (`/proc/cpuinfo`) |
| Architecture / threads | x86_64, 16 |
| RAM | MemTotal 40,465,868 kB ≈ 38.59 GiB |
| GPU device | `/dev/nvidia0` present |
| GPU (driver's own listing) | NVIDIA GeForce RTX 2060 with Max-Q Design |
| VRAM (nvidia-smi) | 6144 MiB = 6.0 GiB; driver 595.99.02; GPU 37 °C at reading |
| Thermal zones | 37 °C / 40 °C |
| Disk | 1.1 TB free of 3.6 TB |
| Python | 3.14.4 |
| Aesir on the machine | `~/aesir-gpu` and `~/aesir-vision` workspaces present; **no Aesir service active** at reading (`systemctl is-active aesir` → inactive) |
| Hailo | **no `/dev/hailo0`** — this machine has no Hailo device |
| Yggdrasil service | not installed |

## The profile (data, not code)

`profiles/laptop_citizen.json` names what a node must
measure to wear the laptop hat, and the terms on which it
shares the user's own machine:

- accepted arches `x86_64` / `amd64`; at least 8 cores and
  16 GiB RAM measured;
- **required accelerator `cuda`** and a **VRAM floor of
  4.0 GiB** — both as data in the profile (the schema's new
  optional fields), so the gates below read them, not a
  constant buried in code;
- the laptop governor policy: thermal shed 85 °C / resume
  77 °C / hard 95 °C, memory ceiling 0.80, CPU ceiling
  0.85 — an operator's policy, labelled as such, never a
  measured fact;
- a **user reserve of 4.0 GiB**: the governor ceiling must
  leave at least that much of the measured RAM outside the
  citizen's own share (on purplelongship's measured RAM,
  the 0.80 ceiling leaves ≈ 7.72 GiB);
- 3 model slots;
- the neighbour list — the user's desktop session
  (service) and the NVIDIA device (`/dev/nvidia0`) — each
  with the observation key a caller reports its state by;
- `hailo_rule: never_beyond_aesir_ledger`, as a constant
  the schema enforces (this machine's claim is `absent`).

## Evaluation: the citizen report

`evaluate_laptop_citizen(census, device_class=…, neighbours=…)`:

- takes a real Y-022 census and a caller-measured neighbour
  observation (this module holds no process or network
  client — deployment wiring reads the session and the
  device node, and hands the states in; an unshaped or
  unknown observation is refused by name, a missing one
  reads `unknown`, never an invented calm);
- gates on device class, x86 architecture, cores, RAM, a
  CUDA accelerator actually in the census, a **measured**
  VRAM at or above the floor, and the reserve the ceiling
  actually leaves. The VRAM gate bites on absence in
  particular: Y-022's system probes honestly read no VRAM
  (there is no filesystem source for it), so a census built
  from bare system probes *fails* this gate by name until
  deployment wiring supplies the vendor tool's reading of
  the actual card (`nvidia-smi` on purplelongship:
  6144 MiB) as a probe. An unmeasured VRAM is never rounded
  up to a pass;
- records the **Aesir grade as observed**, read from the
  census's own engine entries — the seam through which an
  `AesirAdapter`'s ledger-graded discovery (Y-024) feeds
  the census: `proven` / `partial` only when the census
  entry itself carries Aesir's own ledger grade,
  `unproven` when an Aesir engine is listed without one,
  `absent` when none was fed in. The grade does not gate
  readiness: Aesir's *service* being live on the laptop is
  deployment state (at this slice's measured reading, no
  Aesir service was active), while the citizen's hardware
  truth is what gates. The citizen repeats a grade; it
  never raises one. CUDA *detection* likewise stays
  `proven: false` in the census (Y-022) — presence is
  measured; proof is the ledger's word;
- records every neighbour as **observed** with
  `action_taken: "none"` — the coexistence law in data.
  The citizen never touches the user's desktop session and
  opens no device by observation; coexistence is the
  reserve plus the governor shedding the citizen's own
  load first.

## The factory: the whole agent, wired

`build_laptop_citizen_agent(config, store_root=…, census_engines=…)`
refuses a machine whose evaluation is not ready — an ARM
board or a GPU-less x86 box is not dressed as a laptop
citizen — and then builds the full Part II stack under the
profile: census collector carrying the caller's engine
truth into every refresh, the profile's governor, a
hash-pinned model store (Y-026), a model lifecycle (Y-027)
whose VRAM pool is the census's measured VRAM (6.0 GiB on
purplelongship) and whose RAM pool is the measured census
RAM scaled by the governor's memory fraction (the reserve
binds the budgets, not only the report), the task sandbox
(Y-029), the local queue (Y-031), and the failure monitor
(Y-032) — all handed to the node agent (Y-021 grown):
`status()` carries the citizen report under `citizen`, the
agent's own census refreshes keep the same Aesir engine
seam, and an agent built without a citizen carries
`citizen: None`, exactly as before.

The acceptance run exercises the built citizen end to end:
a laptop-class identity starts and joins, the agent's
census carries the Aesir engine graded `proven` through
the real adapter over a fake transport speaking Aesir's
wire shapes (ledger AES-SRV-010 verified + a ready
resident — the grade is the ledger's, repeated), a
hash-pinned fixture model loads, a queued task runs
through the sandbox behind the leases, a heartbeat is
served, and the failure report reads zero incidents. The
model loader is Y-027's SIMULATED loader — residency is
bookkeeping; no weights move and no live Aesir service is
claimed.

## Not built / not claimed here

- No systemd service is installed on purplelongship by
  this slice: enabling `deploy/yggdrasil-node.service`
  there stays the operator's act, with identity
  provisioning (Y-011) done by hand first.
- No live Aesir service was spoken to: at the measured
  reading none was active on the laptop. Aesir's GPU line
  joins this citizen through the adapter's census seam
  when its service is wired in deployment; the on-hardware
  inference proof for the card itself is Project Aesir's
  own ledger, not this mesh's claim.
- No Hailo capability of any kind (no device on this
  machine; claim `absent`).
- No fabric presence: joining is still the local act of
  Y-021 until Part III.
- Power/thermal telemetry acting on real heat is Y-044;
  this slice's governor reads the seams it is handed.
  purplelongship as Jotunheim under sustained load is
  Y-169.
