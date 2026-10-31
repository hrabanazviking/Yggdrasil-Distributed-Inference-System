# The VM Citizen — Y-035

*Part II's third real citizen: the full node agent on this
VM as a CPU-only node, in `src/yggdrasil/vm_citizen.py`,
wearing a profile that lives as data in
`profiles/vm_citizen.json` (`citizen_profile` v1,
registered in the Y-016 manifest). Its whole character is
in the roadmap's own words: honest speeds, no pretence.
The Pi citizen is Y-033 and the laptop citizen is Y-034;
with this slice all three citizens named by the Part II
gate (Y-048) exist. The phone citizen followed at Y-036
(PHONE_CITIZEN.md), built and SIMULATED only — on-device
proof NOT PROVEN until a phone runs it.*

## The machine, measured

This VM is Yrsa's own runtime host, read directly on
2026-10-31 (~04:26 EDT) before this slice was built, and
used — labelled as fixture readings, so the in-tree runs
stay SIMULATED — as the shape the acceptance tests are
built from:

| Fact | Measured value |
|---|---|
| Host | `htch-runtime` (a virtual machine on shared host hardware) |
| CPU | AMD EPYC 9D64 88-Core Processor (`/proc/cpuinfo`) — the host's CPU, of which the VM holds a slice |
| Architecture / cores | x86_64, 4 |
| RAM | MemTotal 8,124,908 kB ≈ 7.749 GiB |
| Accelerators | **none** — no `/dev/nvidia0`, no `/dev/hailo0`; the census reports exactly one entry, `{"kind": "none", "proven": false}` |
| VRAM | no reading exists (no accelerator to hold one) |
| Thermal | **no thermal zone** — the census carries no `thermal_c` field at all |
| Disk | 7.346 GiB free on the root filesystem the census probe measures (the workspace filesystem is a separate, larger mount and is not what the probe reads) |
| Host daemon | the hatch daemon is **active** — this VM's work, this citizen included, runs under it alongside many other campaigns |
| Python | 3.12.3 |
| Yggdrasil service | not installed |

## The profile (data, not code)

`profiles/vm_citizen.json` names what a node must measure
to wear the VM hat, and the terms on which it shares a
small, busy, virtual machine:

- accepted arches `x86_64` / `amd64`; at least 2 cores and
  4.0 GiB RAM measured;
- **no required accelerator and no VRAM floor** — and this
  is enforced, not just written: `validate_profile`
  refuses a VM profile that names either one, by name. A
  CPU-only profile that requires an accelerator
  contradicts itself (the inverse of the laptop citizen's
  rule at Y-034, where an accelerator profile *without*
  them is refused);
- the VM governor policy: thermal shed 80 °C / resume
  72 °C / hard 90 °C (the Y-028 default policy's lines),
  memory ceiling 0.75, CPU ceiling 0.75 — a full quarter
  of memory and of CPU left outside the citizen's share
  on a VM it shares; an operator's policy, labelled as
  such, never a measured fact;
- a **host reserve of 1.5 GiB**: the governor ceiling must
  leave at least that much of the measured RAM outside
  the citizen's own share (on this VM's measured RAM, the
  0.75 ceiling leaves 1.937 GiB);
- 2 model slots;
- the neighbour list — the host daemon (service) and the
  thermal sensor device (`/sys/class/thermal/thermal_zone*`)
  — each with the observation key a caller reports its
  state by;
- `hailo_rule: never_beyond_aesir_ledger`, as a constant
  the schema enforces (this machine's claim is `absent`).

## Evaluation: the citizen report

`evaluate_vm_citizen(census, device_class=…, neighbours=…)`:

- takes a real Y-022 census and a caller-measured neighbour
  observation (this module holds no process or network
  client — deployment wiring reads the daemon and the
  sensor tree, and hands the states in; an unshaped or
  unknown observation is refused by name, a missing one
  reads `unknown`, never an invented calm);
- gates on device class, x86 architecture, cores, RAM,
  the reserve the ceiling actually leaves, and
  **cpu_only** — the slice's defining gate. It passes only
  when the census's accelerator list is exactly the single
  `none` entry *and* the census carries no VRAM
  measurement. A machine with a real accelerator in its
  census fails by name (it belongs to another citizen,
  not this one), and a VRAM figure on a supposedly
  accelerator-less node is a contradiction the same gate
  refuses — never smoothed over;
- records the honesty checks as **observed**:
  `honest_speeds` (this citizen claims no
  tokens-per-second figure for any model — a speed for
  device class `vm` enters a registry entry, Y-008, only
  when a run on a VM measured it with its grade, never
  copied from the Pi or laptop readings and never inferred
  from the host CPU's name); `thermal_reading` (this VM
  exposes no sensor, so the governor's thermal check has
  no reading to act on — the absence is reported as
  absence, never read as a cool machine, per Y-028; a VM
  that *does* expose a reading reports the measured
  number, proven in tests); and the `hailo_claim`
  (`absent` here, by measurement and by the standing rule);
- records every neighbour as **observed** with
  `action_taken: "none"` — the coexistence law in data.
  The citizen never signals the host daemon it runs under;
  coexistence is the reserves plus the governor shedding
  the citizen's own load first.

## The factory: the whole agent, wired

`build_vm_citizen_agent(config, store_root=…)` refuses a
machine whose evaluation is not ready — an
accelerator-bearing or undersized machine is not dressed
as a VM citizen — and then builds the full Part II stack
under the profile: census collector carrying the caller's
engine seam into every refresh, the profile's governor, a
hash-pinned model store (Y-026), a model lifecycle (Y-027)
whose VRAM pool is the census's own — **zero** on this
citizen, absence read as absence — and whose RAM pool is
the measured census RAM scaled by the governor's memory
fraction (the reserve binds the budgets, not only the
report), the task sandbox (Y-029), the local queue
(Y-031), and the failure monitor (Y-032) — all handed to
the node agent (Y-021 grown): `status()` carries the
citizen report under `citizen`, and an agent built
without a citizen carries `citizen: None`, exactly as
before.

The acceptance run exercises the built citizen end to
end: a vm-class identity starts and joins, a hash-pinned
fixture model loads, a queued task runs through the
sandbox behind the leases (under injected calm readings,
SIMULATED — the live run below tells the load story
plainly), a heartbeat is served, and the failure report
reads zero incidents. The model loader is Y-027's
SIMULATED loader — residency is bookkeeping; no weights
move.

**One defect found by the full suite, fixed in the same
slice** (the Y-035 fix commit): the hard-coded manifest
census in `tests/test_data_discipline.py` still named the
pre-Y-035 set of registered data-file ids, so the first
full-suite run failed that one test (1 failed, 430
passed, read from the run's log file). The census now
includes `vm_citizen_profile` — the same update every
registering slice before this one made — and the re-run
full suite is green: **431 passed**.

## The live run on this VM

Run on 2026-10-31 (~04:35–04:37 EDT) against this slice's
code on `htch-runtime` itself — scratch only: nothing was
installed, no service was enabled, and the run's identity,
store, and keys lived under a scratch directory and die
with it.

**The citizen run itself** (system census probes and the
governor's real readings — nothing injected,
`simulated: false`): the factory evaluated this VM
**ready**, every gating check passing. The live census
read x86_64, 4 cores, "AMD EPYC 9D64 88-Core Processor",
7.749 GiB RAM, disk 7.319 GiB free, and accelerators
exactly `[{"kind": "none", "proven": false}]` — CPU-only,
measured. No `thermal_c`, no `vram_gb`: both absences
stand in the census as absences. The reserve computed
1.937 GiB. The neighbour observation, measured live by
the runner (process-table and device-tree reads, handed
in as data): the host daemon **active**, the thermal
sensor **absent** — every neighbour's `action_taken`
remained `none`. `hailo_claim` is `absent`. The built
citizen then started on its freshly provisioned vm-class
identity, joined, loaded the hash-pinned fixture model,
and answered a heartbeat `present` with **0 failure
incidents**.

**The governor's honest refusal.** The queued task was
**not admitted** in the live run, and this document does
not dress that up: at the moment of asking, this VM was
genuinely loaded by its other work — the governor's real
readings were memory fraction **0.91** and CPU share
**3.15** against the profile's 0.75 ceilings — so the
governor refused the task by name (`memory, cpu`;
projected memory fraction 0.9099, projected CPU share
3.15), and the task stayed held in the local queue
(depth 1, exactly Y-031's proven behaviour for a refused
run), visible in the heartbeat's queue depth. That
refusal *is* the VM citizen working as designed on a
shared machine: it sheds its own new load before it
presses the host, and it says why in numbers. Task
completion end to end is proven in-tree (above) and was
proven live for the Pi and laptop citizens on their
machines; for this citizen's live run, no completed task
is claimed — the truthful live result is *ready, joined,
and refusing under real load*.

## Not built / not claimed here

- Model fetch duty (Y-037) is wired by this factory: the
  agent carries a `model_fetcher=` over its store (policy
  and transport are the factory's `fetch_policy=` /
  `fetch_transport=` seams; with no transport handed in, a
  fetch fails honestly as `no_transport_wired` — no real
  peer has carried a model for this tree yet; see
  MODEL_FETCH.md).

- No systemd service is installed on this VM by this
  slice: enabling `deploy/yggdrasil-node.service` here
  stays the operator's act, with identity provisioning
  (Y-011) done by hand first.
- No speed figure for any model on any device class: no
  inference ran in this slice, on this VM or anywhere.
  VM-measured speeds, when a real engine runs here, are
  registry data (Y-008) recorded by the slice that runs
  them.
- No accelerator capability of any kind (none measured;
  the cpu_only gate exists to keep it that way).
- No bare-metal performance claim: this is a virtual
  machine on a shared host; its census is a fact about
  the VM at the moment of reading, and its CPU is a
  shared host CPU of which the VM rents a slice.
- No fabric presence: joining is still the local act of
  Y-021 until Part III.
- Power/thermal telemetry acting on real heat is Y-044;
  this VM in any case exposes no thermal sensor for the
  governor to cite.
