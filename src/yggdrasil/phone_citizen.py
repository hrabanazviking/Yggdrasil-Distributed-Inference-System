"""Phone citizen — Y-036.

Part II's fourth citizen, and its most honest about what
has *not* happened: the full agent's shape on a phone —
small models, a battery governor — **built and simulated
only**. No phone has run this code. On-device proof is
**NOT PROVEN** until a real phone runs it, and this module,
its report, and its document all say so. Where the Pi
(Y-033), laptop (Y-034), and VM (Y-035) citizens each carry
a live run on their own named hardware, this citizen
carries none, and claims none.

- **The profile is data.** ``profiles/phone_citizen.json``
  (``citizen_profile`` v1, registered in the Y-016 manifest;
  this slice adds the schema's two optional phone fields):
  the accepted ARM architectures, the modest core and RAM
  floors a phone must measure, the phone governor policy
  (thermal shed 45 / resume 38 / hard 50 °C, memory ceiling
  0.50, CPU ceiling 0.50 — half of each left outside the
  citizen's share on a device whose first job is being the
  user's phone; *policy*, labelled as such), the reserve
  the ceiling must leave for the phone OS and the user's
  own apps, one model slot, the **small-model ceiling**
  (``max_model_size_gb``: 1.5 GiB — a model larger than
  that is not a phone model for this citizen, however well
  it runs elsewhere), the **battery governor policy**
  (``battery_policy``: shed below 20 % unplugged, critical
  below 10 %), and the neighbour list (the phone OS, the
  cellular radio).
- **Evaluation is measured — by the caller.**
  :func:`evaluate_phone_citizen` takes a real Y-022 census
  and a *caller-measured* neighbour/battery observation —
  plain dicts the caller measured (this module holds no
  process or network client, so the Part I gate's
  banned-import scan stays green) — and answers a validated
  ``citizen_report`` v1. The gating checks (device class,
  ARM architecture, cores, RAM, the small-model ceiling
  fitting inside the RAM pool the governor fraction
  leaves, the reserve, and the battery governor's critical
  line) must pass for ``ready``.
- **The battery governor is data and it bites.**
  :func:`battery_state_for` reads a caller-measured battery
  reading (``level_percent`` + ``charging``, either possibly
  unknown) against the profile's battery policy: unplugged
  below the shed line the citizen is *shedding*, unplugged
  below the critical line it is *critical* and the
  evaluation's battery gate fails — a phone about to die is
  not dressed as a ready citizen. Charging relaxes the shed
  line to the critical line (a phone on its charger may
  keep working lower), never below it. **An unknown battery
  level is never read as full**: with no level measured the
  battery state is ``unknown``, the citizen claims no
  battery headroom, and the observation is recorded as the
  absence it is.
- **Small models only, as a rule with a helper.**
  :func:`model_size_allowed` is the discipline in code: a
  model whose pinned size exceeds the profile's ceiling is
  refused for this citizen, whatever pool it might squeeze
  into. On top of that, the lifecycle's own budgets still
  bite — its RAM pool here is the census RAM inside the
  governor's 0.50 fraction.
- **The Hailo rule is the standing one.** The report's
  ``hailo_claim`` comes from Y-033's helper: ``absent``
  unless the census itself carries a Hailo entry, and
  ``proven_by_aesir_ledger`` only when that entry carries
  ``proven: true`` — a grade this citizen never raises.
- **The factory wires the whole agent.**
  :func:`build_phone_citizen_agent` refuses a machine whose
  evaluation is not ``ready`` (an x86 machine, an
  undersized phone shape, or a critically-low unplugged
  battery is not dressed as a phone citizen), then builds
  the full Part II stack under the profile: census
  collector (carrying the caller's engine seam into every
  refresh), the profile's governor, a hash-pinned model
  store, a model lifecycle whose RAM pool is the census
  RAM scaled by the governor's memory fraction (the
  reserve binds the budgets, not just the report), the
  task sandbox, the local queue, and the failure monitor —
  all handed to the node agent, whose ``status()`` then
  carries this citizen report under ``citizen``.

Honesty: every acceptance run in this slice is injected-
probe **SIMULATED** — the test shape is a declared,
representative phone shape (round numbers, chosen, not
measured off any real phone), and no measured fact about
any real phone is stated anywhere in this slice. What no
run here claims: that this code has run on a phone, that
any phone's battery/thermal behaviour matches the policy's
lines, the agent *installed* as an app or service on any
phone, any accelerator capability, or any fabric presence
(Part III).
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
PROFILE_PATH = _ROOT / "profiles" / "phone_citizen.json"

# The states each neighbour observation key may carry. A state
# outside its key's vocabulary, an unknown key, or a missing
# profile neighbour is refused: an observation the citizen
# cannot read is not quietly read as calm.
_NEIGHBOUR_STATES = {
    "phone_os": ("active", "inactive", "unknown"),
    "cellular_radio": ("present", "absent", "unknown"),
}
_NEIGHBOUR_DEFAULT = {
    "phone_os": "unknown",
    "cellular_radio": "unknown",
}

_BATTERY_KEYS = {"level_percent", "charging"}


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


class PhoneCitizenError(Exception):
    """Any phone-citizen failure: a bad profile, an unshaped
    observation, or a machine that does not meet the profile."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise PhoneCitizenError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_profile(profile: dict) -> dict:
    """A citizen profile against its schema and its cross-field
    rules: the governor policy must itself validate (resume
    below shed, hard above shed — Y-028), the citizen must be
    the device class it names, the phone citizen must name
    its small-model ceiling and its battery policy (a phone
    profile without them is a profile that cannot bite — the
    same shape of rule the laptop citizen keeps for its
    accelerator fields, Y-034), the battery policy's critical
    line must sit below its shed line, and neighbour
    observation keys must be known and unique."""
    _validate(_PROFILE_VALIDATOR, profile, "citizen profile")
    if profile["citizen"] != profile["device_class"]:
        raise PhoneCitizenError(
            f"citizen {profile['citizen']!r} does not match its device_class "
            f"{profile['device_class']!r}"
        )
    try:
        validate_governor_policy(dict(profile["governor_policy"]))
    except Exception as exc:
        raise PhoneCitizenError(f"profile governor policy is invalid: {exc}") from exc
    if profile.get("max_model_size_gb") is None:
        raise PhoneCitizenError(
            "the phone citizen profile names no max_model_size_gb; "
            "a phone citizen must name the small-model ceiling it enforces"
        )
    battery = profile.get("battery_policy")
    if battery is None:
        raise PhoneCitizenError(
            "the phone citizen profile names no battery_policy; "
            "a phone citizen must name the battery lines its governor obeys"
        )
    if not battery["critical_below_percent"] < battery["shed_below_percent"]:
        raise PhoneCitizenError(
            "battery_policy critical_below_percent must be below shed_below_percent "
            f"({battery['critical_below_percent']} !< {battery['shed_below_percent']})"
        )
    keys = [n["observation_key"] for n in profile["neighbours"]]
    if len(keys) != len(set(keys)):
        raise PhoneCitizenError("profile neighbours carry duplicate observation keys")
    for key in keys:
        if key not in _NEIGHBOUR_STATES:
            raise PhoneCitizenError(f"profile names an unknown neighbour observation key: {key}")
    return profile


def load_profile(path: Path | None = None) -> dict:
    """The phone citizen profile from its data file (Y-016
    discipline: versioned, schema-shaped, registered)."""
    path = Path(path) if path is not None else PROFILE_PATH
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise PhoneCitizenError(f"cannot load citizen profile {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PhoneCitizenError(f"citizen profile {path} is not a JSON object")
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
        raise PhoneCitizenError("a neighbour observation is a dict")
    wanted = {n["observation_key"] for n in profile["neighbours"]}
    unknown = set(observations) - wanted
    if unknown:
        raise PhoneCitizenError(f"unknown neighbour observation key(s): {sorted(unknown)}")
    cleaned = {}
    for key in sorted(wanted):
        state = observations.get(key, _NEIGHBOUR_DEFAULT[key])
        if state not in _NEIGHBOUR_STATES[key]:
            raise PhoneCitizenError(
                f"neighbour {key} state {state!r} is not one of {_NEIGHBOUR_STATES[key]}"
            )
        cleaned[key] = state
    return cleaned


def clean_battery(battery: dict | None) -> dict:
    """A caller's battery reading, cleaned: ``level_percent``
    an int in 0–100 or None, ``charging`` a bool or None.
    Missing keys read None — **unknown**, and unknown is
    never quietly read as full (or as charging). Unknown keys
    and unshaped values are refused by name."""
    if battery is None:
        battery = {}
    if not isinstance(battery, dict):
        raise PhoneCitizenError("a battery reading is a dict")
    unknown = set(battery) - _BATTERY_KEYS
    if unknown:
        raise PhoneCitizenError(f"unknown battery reading key(s): {sorted(unknown)}")
    level = battery.get("level_percent")
    if level is not None and (
        not isinstance(level, int) or isinstance(level, bool) or not 0 <= level <= 100
    ):
        raise PhoneCitizenError(
            f"battery level_percent must be an int in 0-100 or None, got {level!r}"
        )
    charging = battery.get("charging")
    if charging is not None and not isinstance(charging, bool):
        raise PhoneCitizenError(f"battery charging must be a bool or None, got {charging!r}")
    return {"level_percent": level, "charging": charging}


def battery_state_for(battery: dict, profile: dict) -> dict:
    """The battery governor's verdict on one cleaned battery
    reading, against the profile's battery policy as data.

    - no level measured → ``unknown``: the citizen claims no
      battery headroom; unknown is never read as full;
    - unplugged (charging false *or unknown* — the
      conservative reading) below the critical line →
      ``critical``, shedding;
    - below the shed line → ``shedding``; while charging, the
      shed line relaxes to the critical line, never below it;
    - otherwise → ``within_ceilings``.
    """
    policy = profile["battery_policy"]
    shed = policy["shed_below_percent"]
    critical = policy["critical_below_percent"]
    level = battery["level_percent"]
    charging = battery["charging"]
    if level is None:
        return {
            "state": "unknown",
            "shedding": False,
            "reasons": ["battery_unknown"],
            "shed_line_percent": shed,
            "critical_line_percent": critical,
        }
    shed_line = critical if charging else shed
    if charging is not True and level < critical:
        return {
            "state": "critical",
            "shedding": True,
            "reasons": ["battery_critical"],
            "shed_line_percent": shed_line,
            "critical_line_percent": critical,
        }
    if level < shed_line:
        return {
            "state": "shedding",
            "shedding": True,
            "reasons": ["battery_low"],
            "shed_line_percent": shed_line,
            "critical_line_percent": critical,
        }
    return {
        "state": "within_ceilings",
        "shedding": False,
        "reasons": [],
        "shed_line_percent": shed_line,
        "critical_line_percent": critical,
    }


def model_size_allowed(size_bytes: int, profile: dict | None = None) -> bool:
    """The small-models-only discipline in code: a model is a
    phone model for this citizen only when its pinned size is
    positive and at or under the profile's ceiling. A larger
    model is refused for this citizen however well it runs on
    a bigger device class."""
    profile = validate_profile(dict(profile)) if profile is not None else load_profile()
    if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes <= 0:
        return False
    return size_bytes <= int(profile["max_model_size_gb"] * (1024**3))


def evaluate_phone_citizen(
    census: dict,
    *,
    device_class: str,
    neighbours: dict | None = None,
    battery: dict | None = None,
    profile: dict | None = None,
    simulated: bool = False,
    now=None,
) -> dict:
    """Evaluate one measured census against the phone profile
    and answer the validated citizen_report v1.

    Gating checks (``pass``/``fail``): the config's device
    class is the profile's, the measured architecture is an
    accepted ARM arch, cores and RAM meet the profile's
    minima, the profile's small-model ceiling fits inside
    the RAM pool the governor fraction leaves, the governor
    ceiling leaves at least the profile's reserve of RAM
    outside the citizen's own share for the phone OS and the
    user's apps, and the battery governor's critical line is
    not crossed. Neighbour and battery-state checks are
    ``observed``: their states are recorded, the action
    taken is ``none`` for each neighbour, and no observed
    state gates readiness — the phone OS being active is the
    device's normal life, and an unknown battery is an
    absence reported as absence (never read as full), while
    a *measured critical* battery is the one battery fact
    that does gate.
    """
    profile = validate_profile(dict(profile)) if profile is not None else load_profile()
    census = validate_census(census)
    cleaned = clean_neighbours(neighbours, profile)
    battery_clean = clean_battery(battery)
    now = now or (lambda: datetime.now(UTC))
    policy = profile["governor_policy"]
    battery_policy = profile["battery_policy"]
    checks: list[dict] = []

    def gate(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "status": "pass" if ok else "fail", "detail": detail})

    def observed(name: str, detail: str) -> None:
        checks.append({"name": name, "status": "observed", "detail": detail})

    gate(
        "device_class",
        device_class == profile["device_class"],
        f"config device_class {device_class!r}; the phone citizen requires "
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
    pool_gb = round(census["ram_gb"] * policy["max_memory_fraction"], 3)
    gate(
        "small_model_ceiling",
        profile["max_model_size_gb"] <= pool_gb,
        f"profile small-model ceiling {profile['max_model_size_gb']} GiB; the RAM "
        f"pool inside the governor memory fraction {policy['max_memory_fraction']} "
        f"is {pool_gb} GiB of the measured RAM — the largest model this citizen "
        f"may carry must fit inside the pool it may actually use",
    )
    reserve_gb = round(census["ram_gb"] * (1 - policy["max_memory_fraction"]), 3)
    gate(
        "phone_memory_reserve",
        reserve_gb >= profile["neighbour_reserve_gb"],
        f"governor memory ceiling {policy['max_memory_fraction']} leaves "
        f"{reserve_gb} GiB of the measured RAM outside the citizen's share; "
        f"profile reserve {profile['neighbour_reserve_gb']} GiB for the phone "
        f"OS and the user's own apps — the device's first job is being a phone",
    )
    battery_state = battery_state_for(battery_clean, profile)
    gate(
        "battery_governor",
        battery_state["state"] != "critical",
        (
            f"battery level {battery_clean['level_percent']}% with charging "
            f"{battery_clean['charging']} is at or above the policy's critical "
            f"line {battery_policy['critical_below_percent']}% (shed line "
            f"{battery_policy['shed_below_percent']}% unplugged)"
            if battery_state["state"] != "critical"
            else f"battery level {battery_clean['level_percent']}% unplugged is "
            f"below the policy's critical line "
            f"{battery_policy['critical_below_percent']}% — a phone about to "
            f"die is not dressed as a ready citizen"
        ),
    )

    observed(
        "battery_state",
        {
            "unknown": "battery level unknown — never read as full: the "
            "battery governor has no reading to act on, the citizen claims "
            "no battery headroom, and the absence is recorded as absence",
            "within_ceilings": f"battery level {battery_clean['level_percent']}% "
            f"(charging: {battery_clean['charging']}) is within the battery "
            f"policy's ceilings — shed line {battery_state['shed_line_percent']}% "
            f"for this charging state, critical line "
            f"{battery_policy['critical_below_percent']}%",
            "shedding": f"battery level {battery_clean['level_percent']}% "
            f"(charging: {battery_clean['charging']}) is below the battery "
            f"policy's shed line {battery_state['shed_line_percent']}% for "
            f"this charging state — the battery governor sheds: new work is "
            f"refused by policy until the level recovers or the charger lands",
            "critical": f"battery level {battery_clean['level_percent']}% "
            f"unplugged is below the battery policy's critical line "
            f"{battery_policy['critical_below_percent']}% — the battery "
            f"governor's verdict is critical",
        }[battery_state["state"]],
    )
    observed(
        "small_models_only",
        f"this citizen carries small models only: a model is a phone model "
        f"here only at or under the profile's ceiling of "
        f"{profile['max_model_size_gb']} GiB pinned size "
        f"(model_size_allowed); a larger model is refused for this citizen "
        f"however well it runs on the Pi, laptop, or VM citizens",
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
        "phone_os_coexistence",
        f"the phone OS observed {cleaned['phone_os']!r}; the citizen takes "
        f"no action on it — the memory reserve above is the coexistence "
        f"mechanism, and calls and the user's own apps outrank the citizen",
    )
    observed(
        "cellular_radio_coexistence",
        f"the cellular radio observed {cleaned['cellular_radio']!r}; the "
        f"citizen opens no socket and touches no radio state by this "
        f"observation (the agent is local-only until Part III)",
    )
    observed(
        "on_device_proof",
        "NOT PROVEN: no phone has run this citizen — every acceptance in "
        "this slice is injected-probe SIMULATED against a declared, "
        "representative phone shape; on-device proof is recorded as NOT "
        "PROVEN until a real phone runs it",
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


def build_phone_citizen_agent(
    config,
    *,
    store_root: Path | str,
    profile: dict | None = None,
    probes=None,
    readings=None,
    neighbours: dict | None = None,
    battery: dict | None = None,
    census_engines=None,
    simulated: bool = False,
    now=None,
    key_store=None,
    fetch_transport=None,
    fetch_policy=None,
) -> tuple[NodeAgent, dict]:
    """The full phone citizen: evaluate first, and refuse a
    machine that does not meet the profile; then wire the
    whole Part II stack under the profile and hand it to a
    node agent whose status carries the citizen report.

    ``probes`` is the census probe set (the system set by
    default — a real measurement of the machine this runs on;
    in this slice's acceptance the probes are injected, shaped
    as a declared representative phone shape, and the runs say
    so with ``simulated=True``: no phone has run this factory,
    and the factory claims no phone run). ``neighbours`` and
    ``battery`` are the caller's measured observations (see
    :func:`evaluate_phone_citizen`). ``census_engines`` is the
    collector's engine seam (Y-022), carried into both this
    evaluation and the agent's own census refreshes.
    ``readings`` is the governor's readings provider (Y-028's
    seam; injected in tests so admission does not depend on
    the build host's momentary load). The model lifecycle's
    RAM pool is the measured census RAM scaled by the
    governor's memory fraction, so the reserve binds the
    budgets as well as the report; its VRAM pool is the
    census's own (no phone census here carries one — read as
    0, never rounded up).
    """
    profile = validate_profile(dict(profile)) if profile is not None else load_profile()
    now = now or (lambda: datetime.now(UTC))
    collector = CensusCollector(
        config.node_id, probes=probes, engines=census_engines, now=now
    )
    census = collector.measure()
    report = evaluate_phone_citizen(
        census,
        device_class=config.device_class,
        neighbours=neighbours,
        battery=battery,
        profile=profile,
        simulated=simulated,
        now=now,
    )
    if not report["ready"]:
        failed = [c["name"] for c in report["checks"] if c["status"] == "fail"]
        raise PhoneCitizenError(
            f"this machine does not meet the phone citizen profile (failed: "
            f"{', '.join(failed)}); it is not dressed as a phone citizen"
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
    if fetch_policy is None:
        # The phone's small-models-only law (Y-036) binds its
        # fetch duty too: the default fetch policy caps a
        # fetch at the profile's own model ceiling.
        fetch_policy = {
            "version": 1,
            "allow_peer_fetch": True,
            "named_sources": [],
            "max_model_bytes": int(profile["max_model_size_gb"] * (1024**3)),
            "min_free_disk_bytes": 0,
        }
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
    "PhoneCitizenError",
    "battery_state_for",
    "build_phone_citizen_agent",
    "clean_battery",
    "clean_neighbours",
    "evaluate_phone_citizen",
    "load_profile",
    "model_size_allowed",
    "validate_profile",
    "validate_report",
]
