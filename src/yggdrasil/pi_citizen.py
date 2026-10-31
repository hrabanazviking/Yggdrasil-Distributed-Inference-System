"""Pi citizen — Y-033.

Part II's citizens are where the agent stops being a skeleton
exercised on the build VM and becomes a resident of a real
device class. This slice is the first: the full agent on
Mimir-class ARM — a Raspberry Pi 5 (the house's Mimir,
``purpleviking1``) that already carries a household: Samtal's
village server, Aurelia's senses service, the kiosk, and the
Hailo road. The citizen is built to *coexist*, and the
coexistence is code and data, not a hope:

- **The profile is data.** ``profiles/pi_citizen.json``
  (``citizen_profile`` v1, registered in the Y-016 manifest):
  the accepted ARM architectures, the minimum cores and RAM a
  node must measure to wear the Pi hat, the Pi governor policy
  (thermal shed 75 / resume 67 / hard 85 °C, memory ceiling
  0.75, CPU ceiling 0.85 — *policy*, labelled as such, never a
  measured fact), the neighbour memory reserve, the model
  slot count, and the neighbour list — each neighbour named
  with the observation key a caller uses to report its state.
- **Evaluation is measured.** :func:`evaluate_pi_citizen`
  takes a real Y-022 census, the node's declared device class,
  and a *neighbour observation* — a plain dict the caller
  measured (deployment wiring reads ``systemctl`` / listeners /
  device nodes; this module holds no process or network client,
  so the Part I gate's banned-import scan stays green) — and
  answers a validated ``citizen_report`` v1. The gating checks
  (device class, ARM architecture, cores, RAM, and the
  neighbour reserve the governor ceiling actually leaves:
  ``ram × (1 − max_memory_fraction)``) must pass for ``ready``;
  the neighbour checks are *observed* — recorded with
  ``action_taken: "none"`` for every neighbour, because the
  citizen's first coexistence law is that it touches nothing
  it shares the machine with: it never starts, stops, or
  signals Samtal, Aurelia, or the kiosk, and it opens no socket
  of its own (the agent is local-only until Part III), so the
  neighbours' ports are theirs.
- **The Hailo rule is the standing one.** The report's
  ``hailo_claim`` is ``absent`` when the census detected no
  Hailo device, ``detected_unproven`` when a device node was
  detected with ``proven: false``, and
  ``proven_by_aesir_ledger`` only when the census entry itself
  carries ``proven: true`` — a grade only Aesir's own ledger
  feeding (Y-024) can produce. This citizen never raises the
  grade itself: the mesh never promises the Hailo chip beyond
  Project Aesir's own ledger. (At this slice's measured
  reading of Mimir, ``/dev/hailo0`` was *absent* — the claim
  recorded for the real machine is ``absent``, and the
  ``/usr/share/hailo-ollama`` software tree's presence changes
  nothing: software on disk is not a device.)
- **The factory wires the whole agent.**
  :func:`build_pi_citizen_agent` refuses a machine whose
  evaluation is not ``ready`` (a non-ARM or undersized node is
  not dressed as a Pi citizen), then builds the full Part II
  stack under the profile: census collector over the caller's
  probes, the profile's governor, a hash-pinned model store,
  a model lifecycle whose RAM pool is the census RAM scaled
  by the governor's memory fraction (the reserve is real in
  the budgets, not just in the report), the task sandbox, the
  local queue, and the failure monitor — all handed to the
  node agent, whose ``status()`` then carries this citizen
  report under ``citizen``.

Honesty: the evaluation and factory are proven in-tree against
probe sets shaped from Mimir's measured facts (labelled
SIMULATED wherever the probes are injected), and — the
citizen's own acceptance — by a live run of this code on
Mimir itself, recorded in ``docs/PI_CITIZEN.md`` with its
measured census and neighbour states. What no run here
claims: the agent is *installed* as a service on Mimir (the
systemd unit stays the operator's act), any Hailo capability,
or any fabric presence (Part III).
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.census import CensusCollector, validate_census
from yggdrasil.degradation import DegradationTracker
from yggdrasil.failure_shapes import FailureMonitor
from yggdrasil.governor import ResourceGovernor, system_readings
from yggdrasil.governor import validate_policy as validate_governor_policy
from yggdrasil.idle_generosity import IdleGenerosity
from yggdrasil.local_queue import LocalQueue
from yggdrasil.model_fetch import ModelFetcher
from yggdrasil.model_lifecycle import ModelLifecycle, budgets_from_census
from yggdrasil.model_store import ModelStore
from yggdrasil.node_agent import NodeAgent
from yggdrasil.node_journal import NodeJournal
from yggdrasil.power_telemetry import PowerTelemetry
from yggdrasil.quant_fitness import QuantFitness
from yggdrasil.sandbox import TaskSandbox
from yggdrasil.task_security import TaskGuard

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"
PROFILE_PATH = _ROOT / "profiles" / "pi_citizen.json"

# The states each neighbour observation key may carry. A state
# outside its key's vocabulary, an unknown key, or a missing
# profile neighbour is refused: an observation the citizen
# cannot read is not quietly read as calm.
_NEIGHBOUR_STATES = {
    "samtal_service": ("active", "inactive", "unknown"),
    "aurelia_port": ("listening", "not_listening", "unknown"),
    "kiosk_port": ("listening", "not_listening", "unknown"),
    "hailo_device": ("present", "absent", "unknown"),
}
_NEIGHBOUR_DEFAULT = {
    "samtal_service": "unknown",
    "aurelia_port": "unknown",
    "kiosk_port": "unknown",
    "hailo_device": "unknown",
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


class PiCitizenError(Exception):
    """Any Pi-citizen failure: a bad profile, an unshaped
    observation, or a machine that does not meet the profile."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise PiCitizenError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_profile(profile: dict) -> dict:
    """A citizen profile against its schema and its cross-field
    rules: the governor policy must itself validate (resume
    below shed, hard above shed — Y-028), the citizen must be
    the device class it names, and neighbour observation keys
    must be known and unique."""
    _validate(_PROFILE_VALIDATOR, profile, "citizen profile")
    if profile["citizen"] != profile["device_class"]:
        raise PiCitizenError(
            f"citizen {profile['citizen']!r} does not match its device_class "
            f"{profile['device_class']!r}"
        )
    try:
        validate_governor_policy(dict(profile["governor_policy"]))
    except Exception as exc:
        raise PiCitizenError(f"profile governor policy is invalid: {exc}") from exc
    keys = [n["observation_key"] for n in profile["neighbours"]]
    if len(keys) != len(set(keys)):
        raise PiCitizenError("profile neighbours carry duplicate observation keys")
    for key in keys:
        if key not in _NEIGHBOUR_STATES:
            raise PiCitizenError(f"profile names an unknown neighbour observation key: {key}")
    return profile


def load_profile(path: Path | None = None) -> dict:
    """The Pi citizen profile from its data file (Y-016
    discipline: versioned, schema-shaped, registered)."""
    path = Path(path) if path is not None else PROFILE_PATH
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise PiCitizenError(f"cannot load citizen profile {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PiCitizenError(f"citizen profile {path} is not a JSON object")
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
        raise PiCitizenError("a neighbour observation is a dict")
    wanted = {n["observation_key"] for n in profile["neighbours"]}
    unknown = set(observations) - wanted
    if unknown:
        raise PiCitizenError(f"unknown neighbour observation key(s): {sorted(unknown)}")
    cleaned = {}
    for key in sorted(wanted):
        state = observations.get(key, _NEIGHBOUR_DEFAULT[key])
        if state not in _NEIGHBOUR_STATES[key]:
            raise PiCitizenError(
                f"neighbour {key} state {state!r} is not one of {_NEIGHBOUR_STATES[key]}"
            )
        cleaned[key] = state
    return cleaned


def hailo_claim_for(census: dict) -> str:
    """The citizen's Hailo claim, from the census alone: absent,
    detected-but-unproven, or — only when the census itself
    carries ``proven: true``, a grade Aesir's ledger feeding
    (Y-024) produces — proven by that ledger. This function
    never raises a grade the census did not carry."""
    for entry in census.get("accelerators", []):
        if entry.get("kind") == "hailo":
            return "proven_by_aesir_ledger" if entry.get("proven") else "detected_unproven"
    return "absent"


def evaluate_pi_citizen(
    census: dict,
    *,
    device_class: str,
    neighbours: dict | None = None,
    profile: dict | None = None,
    simulated: bool = False,
    now=None,
) -> dict:
    """Evaluate one measured census against the Pi profile and
    answer the validated citizen_report v1.

    Gating checks (``pass``/``fail``): the config's device class
    is the profile's, the measured architecture is an accepted
    ARM arch, cores and RAM meet the profile's minima, and the
    governor ceiling leaves at least the profile's neighbour
    reserve of RAM outside the citizen's own share. Neighbour
    checks are ``observed``: their states are recorded, the
    action taken is ``none`` for each, and no observed state —
    Samtal active or quiet, kiosk lit or dark — gates readiness,
    because coexistence is achieved by the reserve and by
    touching nothing, not by demanding the house rearrange
    itself around the citizen.
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
        f"config device_class {device_class!r}; the Pi citizen requires "
        f"{profile['device_class']!r}",
    )
    arch = census["cpu"]["arch"]
    gate(
        "arm_architecture",
        arch in profile["accepted_arches"],
        f"measured arch {arch!r}; accepted ARM arches: {profile['accepted_arches']}",
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
    reserve_gb = round(census["ram_gb"] * (1 - policy["max_memory_fraction"]), 3)
    gate(
        "neighbour_memory_reserve",
        reserve_gb >= profile["neighbour_reserve_gb"],
        f"governor memory ceiling {policy['max_memory_fraction']} leaves "
        f"{reserve_gb} GiB of the measured RAM outside the citizen's share; "
        f"profile reserve {profile['neighbour_reserve_gb']} GiB for Samtal, "
        f"Aurelia, and the system",
    )

    claim = hailo_claim_for(census)
    observed(
        "hailo_claim",
        {
            "absent": "no Hailo device in the measured census; the citizen claims "
            "no Hailo capability (the standing rule: never beyond Aesir's ledger)",
            "detected_unproven": "a Hailo device node was detected with proven: false; "
            "presence is measured, no inference on the chip is claimed",
            "proven_by_aesir_ledger": "the census carries proven: true for Hailo — "
            "Aesir's own ledger grade (Y-024), repeated here, never raised here",
        }[claim],
    )
    observed(
        "samtal_coexistence",
        f"samtal.service observed {cleaned['samtal_service']!r}; the citizen takes "
        f"no action on it — the memory reserve above is the coexistence mechanism",
    )
    observed(
        "aurelia_coexistence",
        f"port 8001 observed {cleaned['aurelia_port']!r}; the agent opens no socket "
        f"(local-only until Part III), so Aurelia's port is untouched",
    )
    observed(
        "kiosk_coexistence",
        f"port 8099 observed {cleaned['kiosk_port']!r}; the citizen neither starts "
        f"nor stops the kiosk — its state is the operator's and Aesir's business",
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


def build_pi_citizen_agent(
    config,
    *,
    store_root: Path | str,
    profile: dict | None = None,
    probes=None,
    readings=None,
    neighbours: dict | None = None,
    simulated: bool = False,
    now=None,
    key_store=None,
    fetch_transport=None,
    fetch_policy=None,
    journal_path=None,
    task_security_policy=None,
    power_snapshot=None,
) -> tuple[NodeAgent, dict]:
    """The full Pi citizen: evaluate first, and refuse a machine
    that does not meet the profile; then wire the whole Part II
    stack under the profile and hand it to a node agent whose
    status carries the citizen report.

    ``probes`` is the census probe set (the system set by
    default — a real measurement of the machine this runs on;
    tests inject a set shaped from Mimir's measured facts and
    say so with ``simulated=True``). ``readings`` is the
    governor's readings provider (Y-028's seam; the real
    machine by default, injected in tests so admission does
    not depend on the build host's momentary load). The model lifecycle's RAM
    pool is the measured census RAM scaled by the governor's
    memory fraction, so the neighbour reserve binds the budgets
    as well as the report; its VRAM pool is the census's own
    (absent on the Pi — read as 0, never rounded up).
    """
    profile = validate_profile(dict(profile)) if profile is not None else load_profile()
    now = now or (lambda: datetime.now(UTC))
    collector = CensusCollector(config.node_id, probes=probes, now=now)
    census = collector.measure()
    report = evaluate_pi_citizen(
        census,
        device_class=config.device_class,
        neighbours=neighbours,
        profile=profile,
        simulated=simulated,
        now=now,
    )
    if not report["ready"]:
        failed = [c["name"] for c in report["checks"] if c["status"] == "fail"]
        raise PiCitizenError(
            f"this machine does not meet the Pi citizen profile (failed: "
            f"{', '.join(failed)}); it is not dressed as a Pi citizen"
        )
    telemetry = PowerTelemetry(
        config.node_id, snapshot=power_snapshot, simulated=simulated, now=now
    )
    governor_readings = readings
    if governor_readings is None:
        # Y-044: with no readings provider handed in, the
        # governor's thermal reading IS the telemetry's
        # hottest zone, sampled at evaluation time — the
        # verdict and its citation rest on one measurement.
        # Memory and CPU still come from the governor's own
        # system readings (Y-028).
        def governor_readings():
            base = system_readings()
            sample = telemetry.sample()
            if sample["hottest_c"] is not None:
                base["thermal_c"] = sample["hottest_c"]
            return base

    governor = ResourceGovernor(
        config.node_id, dict(profile["governor_policy"]), readings=governor_readings, now=now
    )
    store = ModelStore(store_root, now=now)
    budgets = budgets_from_census(census, max_slots=profile["max_slots"])
    budgets["ram_bytes"] = int(
        census["ram_gb"] * profile["governor_policy"]["max_memory_fraction"] * (1024**3)
    )
    fitness = QuantFitness(
        config.node_id, device_class=config.device_class, census=census
    )
    lifecycle = ModelLifecycle(
        config.node_id, store, now=now, fitness_checker=fitness, **budgets
    )
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
    tracker = DegradationTracker(config.node_id, now=now)
    generosity = IdleGenerosity(config.node_id, now=now)
    guard = TaskGuard(config.node_id, task_security_policy, now=now)
    tracker.observe_census(census)
    journal = NodeJournal(config.node_id, path=journal_path, now=now)
    agent = NodeAgent(
        config,
        key_store=key_store,
        now=now,
        census_probes=probes,
        model_store=store,
        model_lifecycle=lifecycle,
        governor=governor,
        sandbox=sandbox,
        local_queue=queue,
        failure_monitor=monitor,
        model_fetcher=fetcher,
        quant_fitness=fitness,
        node_journal=journal,
        degradation_tracker=tracker,
        idle_generosity=generosity,
        task_guard=guard,
        power_telemetry=telemetry,
        citizen=report,
    )
    return agent, report


__all__ = [
    "PROFILE_PATH",
    "PiCitizenError",
    "build_pi_citizen_agent",
    "clean_neighbours",
    "evaluate_pi_citizen",
    "hailo_claim_for",
    "load_profile",
    "validate_profile",
    "validate_report",
]
