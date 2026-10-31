"""VM citizen — Y-035.

Part II's third real citizen, and its most deliberately
unimpressive: the full agent on this VM (``htch-runtime``)
as a **CPU-only node** — honest speeds, no pretence. Where
the Pi citizen (Y-033) is built to coexist and the laptop
citizen (Y-034) to prove an accelerator honestly, the VM
citizen is built to *claim nothing it does not have*:

- **The profile is data.** ``profiles/vm_citizen.json``
  (``citizen_profile`` v1, registered in the Y-016 manifest):
  the accepted x86 architectures, the minimum cores and RAM
  a node must measure to wear the VM hat, the VM governor
  policy (thermal shed 80 / resume 72 / hard 90 °C — the
  Y-028 default policy's lines; memory ceiling 0.75, CPU
  ceiling 0.75, a full quarter of each left outside the
  citizen's share on a VM it shares — *policy*, labelled as
  such), the host reserve, the model slot count, and the
  neighbour list (the host daemon this VM's work runs
  under, the thermal sensor device). A CPU-only profile
  that names a required accelerator or a VRAM floor
  contradicts itself: :func:`validate_profile` refuses one
  by name — the inverse of the laptop citizen's rule.
- **CPU-only is a gate, not a mood.**
  :func:`evaluate_vm_citizen` takes a real Y-022 census, the
  node's declared device class, and a *neighbour
  observation* — a plain dict the caller measured (this
  module holds no process or network client, so the Part I
  gate's banned-import scan stays green) — and answers a
  validated ``citizen_report`` v1. The gating checks
  (device class, x86 architecture, cores, RAM, the reserve
  the governor ceiling leaves, and **cpu_only**) must pass
  for ``ready``. The cpu_only gate passes only when the
  census's accelerator list is exactly the single
  ``none`` entry Y-022 reports for a device-less machine
  *and* the census carries no VRAM measurement: a machine
  with a real accelerator in its census is not dressed as
  a CPU-only VM citizen (it belongs to another citizen),
  and a VRAM figure on a supposedly accelerator-less node
  is a contradiction refused by name, never smoothed over.
- **Honest speeds, stated as checks.** The report records
  as *observed*: ``honest_speeds`` — this citizen claims no
  tokens-per-second figure for any model; a speed for
  device class ``vm`` enters a registry entry (Y-008) only
  when a run on a VM measured it, never copied from the Pi
  or laptop readings; ``thermal_reading`` — this VM exposes
  no thermal sensor, so its census carries no ``thermal_c``
  and the governor's thermal check simply has no reading
  to act on (Y-028: a missing sensor is never read as a
  cool machine); and the ``hailo_claim``, ``absent`` on
  this machine as on the other two citizens (no
  ``/dev/hailo0`` measured; and the standing rule holds
  regardless — never beyond Aesir's ledger).
- **The factory wires the whole agent.**
  :func:`build_vm_citizen_agent` refuses a machine whose
  evaluation is not ``ready`` (an accelerator-bearing or
  undersized machine is not dressed as a VM citizen), then
  builds the full Part II stack under the profile: census
  collector (carrying the caller's engine seam into every
  refresh), the profile's governor, a hash-pinned model
  store, a model lifecycle whose VRAM pool is the census's
  own — zero on this citizen, absence read as absence —
  and whose RAM pool is the census RAM scaled by the
  governor's memory fraction (the reserve binds the
  budgets, not just the report), the task sandbox, the
  local queue, and the failure monitor — all handed to the
  node agent, whose ``status()`` then carries this citizen
  report under ``citizen``.

Honesty: the evaluation and factory are proven in-tree
against probe sets shaped from this VM's measured facts
(labelled SIMULATED wherever the probes are injected), and
by a live run of this code on this VM itself, recorded in
``docs/VM_CITIZEN.md`` with its measured census. This VM
is a virtual machine on shared host hardware: its census
is a fact about the VM at the moment of reading, its CPU
is a shared host CPU, and no bare-metal performance claim
of any kind is made. What no run here claims: the agent is
*installed* as a service on this VM, any accelerator
capability, or any fabric presence (Part III).
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.census import CensusCollector, validate_census
from yggdrasil.failure_shapes import FailureMonitor
from yggdrasil.governor import ResourceGovernor
from yggdrasil.governor import validate_policy as validate_governor_policy
from yggdrasil.local_queue import LocalQueue
from yggdrasil.model_fetch import ModelFetcher
from yggdrasil.model_lifecycle import ModelLifecycle, budgets_from_census
from yggdrasil.model_store import ModelStore
from yggdrasil.node_agent import NodeAgent
from yggdrasil.pi_citizen import hailo_claim_for
from yggdrasil.sandbox import TaskSandbox

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"
PROFILE_PATH = _ROOT / "profiles" / "vm_citizen.json"

# The states each neighbour observation key may carry. A state
# outside its key's vocabulary, an unknown key, or a missing
# profile neighbour is refused: an observation the citizen
# cannot read is not quietly read as calm.
_NEIGHBOUR_STATES = {
    "host_daemon": ("active", "inactive", "unknown"),
    "thermal_sensor": ("present", "absent", "unknown"),
}
_NEIGHBOUR_DEFAULT = {
    "host_daemon": "unknown",
    "thermal_sensor": "unknown",
}


def _load_schema(name: str) -> dict:
    return json.loads((_SCHEMAS / name).read_text())


def _validator(name: str):
    """A validator for one schema, with every contract schema
    registered so relative $refs resolve — the registry pattern
    the earlier slices build."""
    schema = _load_schema(name)
    resources = []
    for path in sorted(_SCHEMAS.glob("*.json")):
        other = json.loads(path.read_text())
        resource = Resource.from_contents(other)
        resources.append((path.name, resource))
        resources.append((other["$id"], resource))
        resources.append((other["$id"].rsplit("/", 1)[0] + "/" + path.name, resource))
    registry = Registry().with_resources(resources)
    return validators.validator_for(schema)(schema, registry=registry)


_PROFILE_VALIDATOR = _validator("citizen_profile.schema.json")
_REPORT_VALIDATOR = _validator("citizen_report.schema.json")


class VmCitizenError(Exception):
    """Any VM-citizen failure: a bad profile, an unshaped
    observation, or a machine that does not meet the profile."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise VmCitizenError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_profile(profile: dict) -> dict:
    """A citizen profile against its schema and its cross-field
    rules: the governor policy must itself validate (resume
    below shed, hard above shed — Y-028), the citizen must be
    the device class it names, a CPU-only citizen must name
    NO required accelerator and NO VRAM floor (either one
    contradicts the citizen it describes — the inverse of
    the laptop citizen's rule, Y-034), and neighbour
    observation keys must be known and unique."""
    _validate(_PROFILE_VALIDATOR, profile, "citizen profile")
    if profile["citizen"] != profile["device_class"]:
        raise VmCitizenError(
            f"citizen {profile['citizen']!r} does not match its device_class "
            f"{profile['device_class']!r}"
        )
    try:
        validate_governor_policy(dict(profile["governor_policy"]))
    except Exception as exc:
        raise VmCitizenError(f"profile governor policy is invalid: {exc}") from exc
    if profile.get("required_accelerator_kind"):
        raise VmCitizenError(
            "the VM citizen profile names a required_accelerator_kind; "
            "a CPU-only citizen requires no accelerator"
        )
    if profile.get("min_vram_gb") is not None:
        raise VmCitizenError(
            "the VM citizen profile names a min_vram_gb; "
            "a CPU-only citizen has no VRAM floor"
        )
    keys = [n["observation_key"] for n in profile["neighbours"]]
    if len(keys) != len(set(keys)):
        raise VmCitizenError("profile neighbours carry duplicate observation keys")
    for key in keys:
        if key not in _NEIGHBOUR_STATES:
            raise VmCitizenError(f"profile names an unknown neighbour observation key: {key}")
    return profile


def load_profile(path: Path | None = None) -> dict:
    """The VM citizen profile from its data file (Y-016
    discipline: versioned, schema-shaped, registered)."""
    path = Path(path) if path is not None else PROFILE_PATH
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise VmCitizenError(f"cannot load citizen profile {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise VmCitizenError(f"citizen profile {path} is not a JSON object")
    return validate_profile(raw)


def validate_report(record: dict) -> dict:
    """A citizen report against the citizen_report v1 schema."""
    return _validate(_REPORT_VALIDATOR, record, "citizen report")


def clean_neighbours(observations: dict | None, profile: dict) -> dict:
    """A caller's neighbour observation, cleaned to exactly the
    profile's keys: missing keys read ``unknown`` (never an
    invented state), unknown keys and out-of-vocabulary states
    are refused by name."""
    if observations is None:
        observations = {}
    if not isinstance(observations, dict):
        raise VmCitizenError("a neighbour observation is a dict")
    wanted = {n["observation_key"] for n in profile["neighbours"]}
    unknown = set(observations) - wanted
    if unknown:
        raise VmCitizenError(f"unknown neighbour observation key(s): {sorted(unknown)}")
    cleaned = {}
    for key in sorted(wanted):
        state = observations.get(key, _NEIGHBOUR_DEFAULT[key])
        if state not in _NEIGHBOUR_STATES[key]:
            raise VmCitizenError(
                f"neighbour {key} state {state!r} is not one of {_NEIGHBOUR_STATES[key]}"
            )
        cleaned[key] = state
    return cleaned


def is_cpu_only(census: dict) -> bool:
    """The census's own CPU-only truth: its accelerator list is
    exactly the single ``none`` entry and it carries no VRAM
    measurement. Anything else — a real accelerator, or a
    VRAM figure with no accelerator to hold it — is not a
    CPU-only reading."""
    accelerators = census.get("accelerators", [])
    return (
        len(accelerators) == 1
        and accelerators[0].get("kind") == "none"
        and census.get("vram_gb") is None
    )


def evaluate_vm_citizen(
    census: dict,
    *,
    device_class: str,
    neighbours: dict | None = None,
    profile: dict | None = None,
    simulated: bool = False,
    now=None,
) -> dict:
    """Evaluate one measured census against the VM profile and
    answer the validated citizen_report v1.

    Gating checks (``pass``/``fail``): the config's device
    class is the profile's, the measured architecture is an
    accepted x86 arch, cores and RAM meet the profile's
    minima, the machine is CPU-only by its own census (the
    slice's defining gate — see :func:`is_cpu_only`), and the
    governor ceiling leaves at least the profile's reserve
    of RAM outside the citizen's own share for the host and
    the VM's other work. Neighbour and honesty checks are
    ``observed``: their states are recorded, the action taken
    is ``none`` for each neighbour, and no observed state
    gates readiness — the host daemon being active is this
    VM's normal life, and the missing thermal sensor is a
    reported absence, not a failure of the machine.
    """
    profile = validate_profile(dict(profile)) if profile is not None else load_profile()
    census = validate_census(census)
    cleaned = clean_neighbours(neighbours, profile)
    now = now or (lambda: datetime.now(UTC))
    policy = profile["governor_policy"]
    checks: list[dict] = []

    def gate(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "status": "pass" if ok else "fail", "detail": detail})

    def observed(name: str, detail: str) -> None:
        checks.append({"name": name, "status": "observed", "detail": detail})

    gate(
        "device_class",
        device_class == profile["device_class"],
        f"config device_class {device_class!r}; the VM citizen requires "
        f"{profile['device_class']!r}",
    )
    arch = census["cpu"]["arch"]
    gate(
        "x86_architecture",
        arch in profile["accepted_arches"],
        f"measured arch {arch!r}; accepted x86 arches: {profile['accepted_arches']}",
    )
    gate(
        "cores",
        census["cpu"]["cores"] >= profile["min_cores"],
        f"measured {census['cpu']['cores']} cores; profile minimum {profile['min_cores']}",
    )
    gate(
        "ram",
        census["ram_gb"] >= profile["min_ram_gb"],
        f"measured {census['ram_gb']} GiB RAM; profile minimum {profile['min_ram_gb']} GiB",
    )
    kinds = [entry.get("kind") for entry in census.get("accelerators", [])]
    gate(
        "cpu_only",
        is_cpu_only(census),
        (
            "the census reports exactly one accelerator entry, kind 'none', "
            "and no VRAM measurement — this node is CPU-only and says so"
            if is_cpu_only(census)
            else f"accelerator kinds in the census: {kinds}, "
            f"vram_gb: {census.get('vram_gb')}; a CPU-only citizen's census "
            f"must be exactly the single 'none' entry with no VRAM — a "
            f"machine with an accelerator belongs to another citizen, and "
            f"a VRAM figure with no accelerator is a contradiction"
        ),
    )
    reserve_gb = round(census["ram_gb"] * (1 - policy["max_memory_fraction"]), 3)
    gate(
        "host_memory_reserve",
        reserve_gb >= profile["neighbour_reserve_gb"],
        f"governor memory ceiling {policy['max_memory_fraction']} leaves "
        f"{reserve_gb} GiB of the measured RAM outside the citizen's share; "
        f"profile reserve {profile['neighbour_reserve_gb']} GiB for the "
        f"host and this VM's other work",
    )

    observed(
        "honest_speeds",
        "this citizen claims no tokens-per-second figure for any model: "
        "a speed for device class 'vm' enters a registry entry (Y-008) "
        "only when a run on a VM measured it with its grade — never "
        "copied from the Pi or laptop citizens' readings, and never "
        "from the host CPU's name (a shared AMD EPYC the VM rents a "
        "slice of, not a speed)",
    )
    thermal = census.get("thermal_c")
    observed(
        "thermal_reading",
        (
            f"the census measured thermal_c {thermal} °C; the governor's "
            f"thermal check acts on that reading (Y-028)"
            if thermal is not None
            else "the census carries no thermal_c — this VM exposes no "
            "thermal sensor, so the governor's thermal check has no "
            "reading to act on; the absence is reported as absence, "
            "never read as a cool machine (Y-028)"
        ),
    )
    claim = hailo_claim_for(census)
    observed(
        "hailo_claim",
        {
            "absent": "no Hailo device in the measured census; the citizen "
            "claims no Hailo capability (the standing rule: never beyond "
            "Aesir's ledger)",
            "detected_unproven": "a Hailo device node was detected with "
            "proven: false; presence is measured, no inference on the "
            "chip is claimed",
            "proven_by_aesir_ledger": "the census carries proven: true for "
            "Hailo — Aesir's own ledger grade (Y-024), repeated here, "
            "never raised here",
        }[claim],
    )
    observed(
        "host_daemon_coexistence",
        f"the host daemon observed {cleaned['host_daemon']!r}; the "
        f"citizen takes no action on it — the memory and CPU reserves "
        f"above are the coexistence mechanism on a shared VM",
    )
    observed(
        "thermal_sensor_coexistence",
        f"thermal sensor observed {cleaned['thermal_sensor']!r}; the "
        f"citizen reads sensors only through the census probes (Y-022) "
        f"and installs nothing of its own",
    )

    neighbour_rows = [
        {
            "name": n["name"],
            "state": cleaned[n["observation_key"]],
            "action_taken": "none",
        }
        for n in profile["neighbours"]
    ]
    ready = all(check["status"] != "fail" for check in checks)
    return validate_report(
        {
            "version": 1,
            "node_id": census["node_id"],
            "citizen": profile["citizen"],
            "at": _fmt(now()),
            "simulated": bool(simulated),
            "ready": ready,
            "census": census,
            "checks": checks,
            "neighbours": neighbour_rows,
            "hailo_claim": claim,
            "memory_reserve_gb": reserve_gb,
        }
    )


def build_vm_citizen_agent(
    config,
    *,
    store_root: Path | str,
    profile: dict | None = None,
    probes=None,
    readings=None,
    neighbours: dict | None = None,
    census_engines=None,
    simulated: bool = False,
    now=None,
    key_store=None,
    fetch_transport=None,
    fetch_policy=None,
) -> tuple[NodeAgent, dict]:
    """The full VM citizen: evaluate first, and refuse a machine
    that does not meet the profile; then wire the whole Part II
    stack under the profile and hand it to a node agent whose
    status carries the citizen report.

    ``probes`` is the census probe set (the system set by
    default — a real measurement of the machine this runs on;
    tests inject a set shaped from this VM's measured facts
    and say so with ``simulated=True``). ``census_engines`` is
    the collector's engine seam (Y-022), carried into both
    this evaluation and the agent's own census refreshes.
    ``readings`` is the governor's readings provider (Y-028's
    seam; the real machine by default, injected in tests so
    admission does not depend on the build host's momentary
    load). The model lifecycle's VRAM pool is the census's
    own — zero for a CPU-only census, absence read as
    absence — and its RAM pool is the measured census RAM
    scaled by the governor's memory fraction, so the reserve
    binds the budgets as well as the report.
    """
    profile = validate_profile(dict(profile)) if profile is not None else load_profile()
    now = now or (lambda: datetime.now(UTC))
    collector = CensusCollector(
        config.node_id, probes=probes, engines=census_engines, now=now
    )
    census = collector.measure()
    report = evaluate_vm_citizen(
        census,
        device_class=config.device_class,
        neighbours=neighbours,
        profile=profile,
        simulated=simulated,
        now=now,
    )
    if not report["ready"]:
        failed = [c["name"] for c in report["checks"] if c["status"] == "fail"]
        raise VmCitizenError(
            f"this machine does not meet the VM citizen profile (failed: "
            f"{', '.join(failed)}); it is not dressed as a VM citizen"
        )
    governor = ResourceGovernor(
        config.node_id, dict(profile["governor_policy"]), readings=readings, now=now
    )
    store = ModelStore(store_root, now=now)
    budgets = budgets_from_census(census, max_slots=profile["max_slots"])
    budgets["ram_bytes"] = int(
        census["ram_gb"] * profile["governor_policy"]["max_memory_fraction"] * (1024**3)
    )
    lifecycle = ModelLifecycle(config.node_id, store, now=now, **budgets)
    sandbox = TaskSandbox(config.node_id, now=now)
    queue = LocalQueue(config.node_id, now=now)
    monitor = FailureMonitor(
        config.node_id,
        model_store=store,
        governor=governor,
        model_lifecycle=lifecycle,
        now=now,
    )
    fetcher = ModelFetcher(
        config.node_id,
        store,
        policy=fetch_policy,
        transport=fetch_transport,
        now=now,
        simulated=simulated,
    )
    agent = NodeAgent(
        config,
        key_store=key_store,
        now=now,
        census_probes=probes,
        census_engines=census_engines,
        model_store=store,
        model_lifecycle=lifecycle,
        governor=governor,
        sandbox=sandbox,
        local_queue=queue,
        failure_monitor=monitor,
        model_fetcher=fetcher,
        citizen=report,
    )
    return agent, report


__all__ = [
    "PROFILE_PATH",
    "VmCitizenError",
    "build_vm_citizen_agent",
    "clean_neighbours",
    "evaluate_vm_citizen",
    "is_cpu_only",
    "load_profile",
    "validate_profile",
    "validate_report",
]
