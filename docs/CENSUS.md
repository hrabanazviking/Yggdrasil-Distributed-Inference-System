# The Capability Census — Y-022

*Part II, second slice: what a node truly has, measured. The
census is the Y-006 `CapabilityCensus` contract made real in
`src/yggdrasil/census.py` — the simulator (Y-014) has carried
the shape with SIMULATED fixtures since the wind tunnel
opened; this is the measurer its schema title has pointed at
all along. The node agent (Y-021) answers it: `agent.census()`
and `agent.refresh_census()`.*

## Measured, not assumed

Every value in a census comes from a probe — a small function
that reads the machine:

| Field | Probe reads |
|---|---|
| `cpu.arch` / `cpu.cores` | `platform.machine()` / `os.cpu_count()` |
| `cpu.model` | first `model name` line of `/proc/cpuinfo`, when present |
| `ram_gb` | `MemTotal` in `/proc/meminfo` (stdlib sysconf fallback) |
| `disk_free_gb` | `shutil.disk_usage()` at the census root |
| `thermal_c` | hottest `/sys/class/thermal/thermal_zone*/temp`, when a sensor exists |
| `accelerators` | device presence: `/dev/nvidia0` or `/proc/driver/nvidia` (CUDA, name from the driver's GPU `information` when it publishes one), `/dev/hailo0` (Hailo), `/dev/rknpu` or `/dev/npu*` (NPU), Apple-silicon macOS (Metal) |
| `vram_gb` | no honest stdlib/filesystem source — absent unless an injected probe supplies it |

Probes read files and stdlib counters only. Nothing shells
out to a vendor tool whose output would have to be trusted
instead of measured — there is no process client in the
module at all.

A value no probe could read is **absent**, never guessed:
this VM exposes no thermal sensor, so its census carries no
`thermal_c` field at all; a machine with no accelerator
device reports exactly one accelerator, `{"kind": "none"}`.
A failing *required* probe (CPU, RAM, accelerators) refuses
the census by name; a failing *optional* probe (disk,
thermal, VRAM) omits its field.

## Detection is not proof

Every detected accelerator carries `proven: false`. A device
node existing is a measured fact about presence; it is not a
proven inference run on that device. Proof belongs to the
engines' own ledgers — Aesir's capability ledger feeds the
census at Y-024 — and to runs on named hardware. The census
never promises it early, the same honesty the mesh applies to
the Hailo chip everywhere: nothing beyond Aesir's own ledger.

Engines and models are likewise reported only as far as they
exist. No engine adapter lands until Y-023 and no model store
until Y-026, so a bare census carries `engines: []` and
`models_present: []`. A caller may hand in engine entries it
measured itself; each is validated against the Y-008
engine-capability schema before it enters the census, and the
census does not soften what an adapter reports.

## Refreshed on change

`CensusCollector` holds a node's current census:

- `measure()` — measure now, hold the result.
- `refresh()` — re-measure and return `(census, changed)`,
  where `changed` names the top-level fields that actually
  differ. The first refresh reports `("initial",)` — the
  baseline against nothing. A refresh over an unchanged
  machine reports `()`: `measured_at` is excluded from the
  comparison, because time passing is not a hardware change.

`census_fingerprint()` hashes a census without its timestamp,
so two censuses of an unchanged machine fingerprint alike
however far apart they were measured, and `diff_censuses()`
gives the same comparison as field names.

The collector's engine/model inputs may be lists or callables
returning lists, so the adapters (Y-023 – Y-025) and the model
store (Y-026) can feed their live truth into the same refresh
when they land, without the collector changing shape.

## The agent's census

The node agent measures nothing until asked — constructing an
agent never touches the machine. `agent.census()` measures on
first asking and holds the result; `status()` carries the
census once measured and `null` before. `agent.refresh_census()`
re-measures, returns `{census, changed_fields}`, and — when a
real change occurred — records a `node.census_changed` Y-013
event naming the changed fields, so the node's own ledger
learns the machine changed. Probes are injectable through the
agent's constructor (`census_probes=`) for tests and odd
hardware; the default is the system probe set at root `/`.

## Probes are injectable

The system probe set reads through a filesystem root
(`system_probe_set(root)`), which is what makes "measured, not
assumed" testable: the acceptance tests point it at a tmp tree
shaped like a machine with a GPU, a Hailo, and a 61.3 °C
thermal zone, and read the census back exactly. Whole probe
sets can also be replaced with lambdas (`ProbeSet(cpu=…,
ram_gb=…, accelerators=…)`), which is how change-over-time is
tested deterministically.

## Not built here

Engine discovery now lands slice by slice (the Ollama
adapter, Y-023, and the Aesir adapter, Y-024 — its discovery
carries Aesir's own capability-ledger grade into the census,
never laundered — report their own reachability into the
census through the collector's callable seams; llama.cpp
follows at Y-025), no model presence (Y-026, and
only with hash proof), no governor acting on these numbers
(Y-028 — the governor's thermal decisions cite this telemetry
at Y-044), no fabric carrying the census to peers (Part III;
the registry service, Y-053, is where censuses travel), and no
hardware citizen proof: a census measured on this VM is a fact
about this VM. The Pi, laptop, and VM citizens are measured on
their own hardware at Y-033 – Y-035.
