"""Laptop citizen — Y-034.

Part II's second real citizen: the full agent on
purplelongship-class x86 — a gaming laptop (the house's
purplelongship: Ryzen 9 4900HS, RTX 2060 Max-Q) whose reason
to be in the mesh is its GPU, and whose engine is Project
Aesir's GPU line. Where the Pi citizen (Y-033) is built to
*coexist* on a shared Pi, the laptop citizen is built to
*prove the accelerator honestly*:

- **The profile is data.** ``profiles/laptop_citizen.json``
  (``citizen_profile`` v1, registered in the Y-016 manifest;
  this slice adds the schema's two optional accelerator
  fields): the accepted x86 architectures, the minimum cores
  and RAM a node must measure to wear the laptop hat, the
  *required accelerator kind* (``cuda``) and the *minimum
  VRAM* (4.0 GiB — a floor the named 6 GiB card clears, with
  Aesir's spill road above it a Part VIII question, not a
  claim here), the laptop governor policy (thermal shed 85 /
  resume 77 / hard 95 °C, memory ceiling 0.80, CPU ceiling
  0.85 — *policy*, labelled as such), the reserve the ceiling
  must leave for the user's own work on his own machine, the
  model slot count, and the neighbour list (the user's
  desktop session, the NVIDIA device node).
- **Evaluation is measured.** :func:`evaluate_laptop_citizen`
  takes a real Y-022 census, the node's declared device
  class, and a *neighbour observation* — a plain dict the
  caller measured (deployment wiring reads the desktop
  session and the device node; this module holds no process
  or network client, so the Part I gate's banned-import scan
  stays green) — and answers a validated ``citizen_report``
  v1. The gating checks (device class, x86 architecture,
  cores, RAM, a CUDA accelerator actually in the census, a
  *measured* VRAM at or above the profile floor, and the
  reserve the governor ceiling leaves) must pass for
  ``ready``. A census whose VRAM no probe could read fails
  the VRAM gate by name: Y-022's system probes honestly
  return no VRAM (there is no filesystem source for it), so
  deployment wiring supplies the vendor tool's reading of
  the actual card (``nvidia-smi`` on purplelongship) as a
  probe — measured on that card, never guessed, and absence
  is never rounded up to a pass.
- **The Aesir engine is graded, never dressed.**
  :func:`aesir_grade_for` reads the census's own engine
  entries — the seam (Y-022/Y-024) through which an
  ``AesirAdapter``'s ledger-graded discovery feeds the
  census. The evaluation records that grade as an *observed*
  check: ``proven`` / ``partial`` only when the census entry
  itself carries Aesir's own ledger grade, ``unproven`` when
  an Aesir engine is listed without one, ``absent`` when no
  Aesir engine was fed into the census at all — in which case
  this citizen claims no Aesir capability. The grade never
  gates readiness: whether Aesir's *service* is live on the
  laptop at a given moment is deployment state (on the
  measured reading for this slice no Aesir service was
  active), while the citizen's hardware truth is what gates.
  The citizen repeats a grade; it never raises one — the
  same standing rule the Pi citizen keeps for Hailo, and the
  report's ``hailo_claim`` on this machine is ``absent``
  (purplelongship has no Hailo device, measured).
- **The factory wires the whole agent.**
  :func:`build_laptop_citizen_agent` refuses a machine whose
  evaluation is not ``ready`` (an ARM board or a GPU-less
  x86 box is not dressed as a laptop citizen), then builds
  the full Part II stack under the profile: census collector
  (carrying the caller's engine truth — the Aesir adapter's
  seam — into every refresh), the profile's governor, a
  hash-pinned model store, a model lifecycle whose VRAM pool
  is the census's measured VRAM and whose RAM pool is the
  census RAM scaled by the governor's memory fraction (the
  reserve binds the budgets, not just the report), the task
  sandbox, the local queue, and the failure monitor — all
  handed to the node agent, whose ``status()`` then carries
  this citizen report under ``citizen``, and whose own census
  refreshes keep the same engine seam.

Honesty: the evaluation and factory are proven in-tree
against probe sets shaped from purplelongship's measured
facts (labelled SIMULATED wherever the probes are injected),
and by a live run of this code on purplelongship itself,
recorded in ``docs/LAPTOP_CITIZEN.md`` with its measured
census. What no run here claims: the agent is *installed*
as a service on the laptop, any Aesir service liveness, or
any fabric presence (Part III).
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.census import CensusCollector, validate_census
from yggdrasil.degradation import DegradationTracker
from yggdrasil.failure_shapes import FailureMonitor
from yggdrasil.governor import ResourceGovernor
from yggdrasil.governor import validate_policy as validate_governor_policy
from yggdrasil.idle_generosity import IdleGenerosity
from yggdrasil.local_queue import LocalQueue
from yggdrasil.model_fetch import ModelFetcher
from yggdrasil.model_lifecycle import ModelLifecycle, budgets_from_census
from yggdrasil.model_store import ModelStore
from yggdrasil.node_agent import NodeAgent
from yggdrasil.node_journal import NodeJournal
from yggdrasil.pi_citizen import hailo_claim_for
from yggdrasil.quant_fitness import QuantFitness
from yggdrasil.sandbox import TaskSandbox
from yggdrasil.task_security import TaskGuard

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"
PROFILE_PATH = _ROOT / "profiles" / "laptop_citizen.json"

# The states each neighbour observation key may carry. A state
# outside its key's vocabulary, an unknown key, or a missing
# profile neighbour is refused: an observation the citizen
# cannot read is not quietly read as calm.
_NEIGHBOUR_STATES = {
    "desktop_session": ("active", "inactive", "unknown"),
    "cuda_device": ("present", "absent", "unknown"),
}
_NEIGHBOUR_DEFAULT = {
    "desktop_session": "unknown",
    "cuda_device": "unknown",
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


class LaptopCitizenError(Exception):
    """Any laptop-citizen failure: a bad profile, an unshaped
    observation, or a machine that does not meet the profile."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise LaptopCitizenError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_profile(profile: dict) -> dict:
    """A citizen profile against its schema and its cross-field
    rules: the governor policy must itself validate (resume
    below shed, hard above shed — Y-028), the citizen must be
    the device class it names, the laptop citizen must name
    its required accelerator and its VRAM floor (an accelerator
    citizen without them is a profile that cannot bite), and
    neighbour observation keys must be known and unique."""
    _validate(_PROFILE_VALIDATOR, profile, "citizen profile")
    if profile["citizen"] != profile["device_class"]:
        raise LaptopCitizenError(
            f"citizen {profile['citizen']!r} does not match its device_class "
            f"{profile['device_class']!r}"
        )
    try:
        validate_governor_policy(dict(profile["governor_policy"]))
    except Exception as exc:
        raise LaptopCitizenError(f"profile governor policy is invalid: {exc}") from exc
    if not profile.get("required_accelerator_kind"):
        raise LaptopCitizenError(
            "the laptop citizen profile names no required_accelerator_kind; "
            "an accelerator citizen must name the accelerator it requires"
        )
    if profile.get("min_vram_gb") is None:
        raise LaptopCitizenError(
            "the laptop citizen profile names no min_vram_gb; "
            "an accelerator citizen must name the VRAM floor it requires"
        )
    keys = [n["observation_key"] for n in profile["neighbours"]]
    if len(keys) != len(set(keys)):
        raise LaptopCitizenError("profile neighbours carry duplicate observation keys")
    for key in keys:
        if key not in _NEIGHBOUR_STATES:
            raise LaptopCitizenError(f"profile names an unknown neighbour observation key: {key}")
    return profile


def load_profile(path: Path | None = None) -> dict:
    """The laptop citizen profile from its data file (Y-016
    discipline: versioned, schema-shaped, registered)."""
    path = Path(path) if path is not None else PROFILE_PATH
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise LaptopCitizenError(f"cannot load citizen profile {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise LaptopCitizenError(f"citizen profile {path} is not a JSON object")
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
        raise LaptopCitizenError("a neighbour observation is a dict")
    wanted = {n["observation_key"] for n in profile["neighbours"]}
    unknown = set(observations) - wanted
    if unknown:
        raise LaptopCitizenError(f"unknown neighbour observation key(s): {sorted(unknown)}")
    cleaned = {}
    for key in sorted(wanted):
        state = observations.get(key, _NEIGHBOUR_DEFAULT[key])
        if state not in _NEIGHBOUR_STATES[key]:
            raise LaptopCitizenError(
                f"neighbour {key} state {state!r} is not one of {_NEIGHBOUR_STATES[key]}"
            )
        cleaned[key] = state
    return cleaned


def cuda_entry_for(census: dict, kind: str = "cuda") -> dict | None:
    """The census's accelerator entry of the required kind, or
    None when the census measured no such accelerator."""
    for entry in census.get("accelerators", []):
        if entry.get("kind") == kind:
            return entry
    return None


def aesir_grade_for(census: dict) -> str:
    """The Aesir grade the census itself carries, from its
    engine entries alone: the ``provenance_grade`` of an
    ``aesir`` engine entry (a grade only the adapter's
    ledger feeding, Y-024, produces), ``unproven`` when an
    Aesir engine is listed carrying no grade, and ``absent``
    when no Aesir engine was fed into this census. This
    function never raises a grade the census did not carry."""
    for entry in census.get("engines", []):
        if entry.get("engine") == "aesir":
            capabilities = entry.get("capabilities") or {}
            return capabilities.get("provenance_grade", "unproven")
    return "absent"


def evaluate_laptop_citizen(
    census: dict,
    *,
    device_class: str,
    neighbours: dict | None = None,
    profile: dict | None = None,
    simulated: bool = False,
    now=None,
) -> dict:
    """Evaluate one measured census against the laptop profile
    and answer the validated citizen_report v1.

    Gating checks (``pass``/``fail``): the config's device
    class is the profile's, the measured architecture is an
    accepted x86 arch, cores and RAM meet the profile's
    minima, the profile's required accelerator is actually in
    the census, the VRAM a probe *measured* meets the
    profile's floor (an unmeasured VRAM fails by name — the
    citizen does not round an absence up), and the governor
    ceiling leaves at least the profile's reserve of RAM
    outside the citizen's own share for the user's own work.
    Neighbour and engine checks are ``observed``: their states
    are recorded, the action taken is ``none`` for each
    neighbour, and no observed state gates readiness — the
    desktop being active is the machine's normal life, and
    Aesir's service liveness is deployment state; coexistence
    is achieved by the reserve and by touching nothing.
    """
    profile = validate_profile(dict(profile)) if profile is not None else load_profile()
    census = validate_census(census)
    cleaned = clean_neighbours(neighbours, profile)
    now = now or (lambda: datetime.now(UTC))
    policy = profile["governor_policy"]
    required_kind = profile["required_accelerator_kind"]
    checks: list[dict] = []

    def gate(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "status": "pass" if ok else "fail", "detail": detail})

    def observed(name: str, detail: str) -> None:
        checks.append({"name": name, "status": "observed", "detail": detail})

    gate(
        "device_class",
        device_class == profile["device_class"],
        f"config device_class {device_class!r}; the laptop citizen requires "
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
    cuda_entry = cuda_entry_for(census, required_kind)
    gate(
        "cuda_accelerator",
        cuda_entry is not None,
        (
            f"measured accelerator {cuda_entry.get('name', required_kind)!r} "
            f"with proven: {bool(cuda_entry.get('proven'))} — detection is "
            f"measured presence; the proof grade belongs to the engine "
            f"ledger (see the aesir_engine check)"
            if cuda_entry is not None
            else f"no {required_kind} accelerator in the measured census; "
            f"a GPU-less machine is not a laptop citizen"
        ),
    )
    vram = census.get("vram_gb")
    gate(
        "vram",
        vram is not None and vram >= profile["min_vram_gb"],
        (
            f"measured {vram} GiB VRAM; profile minimum {profile['min_vram_gb']} GiB"
            if vram is not None
            else "no VRAM measurement in the census; Y-022's system probes "
            "read no VRAM honestly, so deployment wiring must supply the "
            "vendor tool's reading of the actual card as a probe — an "
            "unmeasured VRAM is never rounded up to a pass"
        ),
    )
    reserve_gb = round(census["ram_gb"] * (1 - policy["max_memory_fraction"]), 3)
    gate(
        "user_memory_reserve",
        reserve_gb >= profile["neighbour_reserve_gb"],
        f"governor memory ceiling {policy['max_memory_fraction']} leaves "
        f"{reserve_gb} GiB of the measured RAM outside the citizen's share; "
        f"profile reserve {profile['neighbour_reserve_gb']} GiB for the "
        f"user's own desktop and work on his own machine",
    )

    grade = aesir_grade_for(census)
    observed(
        "aesir_engine",
        {
            "proven": "the census carries an Aesir engine graded proven — "
            "Aesir's own ledger grade (Y-024, AES-SRV-010 verified with a "
            "ready resident), repeated here, never raised here",
            "partial": "the census carries an Aesir engine graded partial — "
            "Aesir's own ledger grade (Y-024), repeated here exactly",
            "unproven": "an Aesir engine is listed in the census with no "
            "ledger grade to repeat; no Aesir capability is claimed "
            "beyond its listing",
            "absent": "no Aesir engine was fed into this census; this "
            "citizen claims no Aesir capability — Aesir's GPU line joins "
            "through the adapter's census seam when its service is wired",
        }[grade],
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
        "desktop_coexistence",
        f"the user's desktop session observed {cleaned['desktop_session']!r}; "
        f"the citizen takes no action on it — the memory reserve above is "
        f"the coexistence mechanism, and the governor sheds the citizen's "
        f"own load before it cooks the user's machine",
    )
    observed(
        "cuda_device_coexistence",
        f"/dev/nvidia0 observed {cleaned['cuda_device']!r}; the citizen "
        f"opens no device and starts no engine by this observation — "
        f"engines join through their adapters (Y-023 – Y-025)",
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


def build_laptop_citizen_agent(
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
    journal_path=None,
    task_security_policy=None,
) -> tuple[NodeAgent, dict]:
    """The full laptop citizen: evaluate first, and refuse a
    machine that does not meet the profile; then wire the
    whole Part II stack under the profile and hand it to a
    node agent whose status carries the citizen report.

    ``probes`` is the census probe set (the system set by
    default — a real measurement of the machine this runs on,
    whose VRAM honestly reads None until deployment wiring
    supplies the vendor tool's reading of the card; tests
    inject a set shaped from purplelongship's measured facts
    and say so with ``simulated=True``). ``census_engines`` is
    the collector's engine seam (Y-022): a list or callable of
    validated engine capabilities — in deployment, an
    ``AesirAdapter``'s ``capability_for_census`` — carried into
    both this evaluation and the agent's own census refreshes,
    so the ledger's grade reaches the citizen exactly once,
    through the census. ``readings`` is the governor's readings
    provider (Y-028's seam; the real machine by default,
    injected in tests so admission does not depend on the
    build host's momentary load). The model lifecycle's VRAM
    pool is the census's measured VRAM and its RAM pool is the
    measured census RAM scaled by the governor's memory
    fraction, so the reserve binds the budgets as well as the
    report.
    """
    profile = validate_profile(dict(profile)) if profile is not None else load_profile()
    now = now or (lambda: datetime.now(UTC))
    collector = CensusCollector(
        config.node_id, probes=probes, engines=census_engines, now=now
    )
    census = collector.measure()
    report = evaluate_laptop_citizen(
        census,
        device_class=config.device_class,
        neighbours=neighbours,
        profile=profile,
        simulated=simulated,
        now=now,
    )
    if not report["ready"]:
        failed = [c["name"] for c in report["checks"] if c["status"] == "fail"]
        raise LaptopCitizenError(
            f"this machine does not meet the laptop citizen profile (failed: "
            f"{', '.join(failed)}); it is not dressed as a laptop citizen"
        )
    governor = ResourceGovernor(
        config.node_id, dict(profile["governor_policy"]), readings=readings, now=now
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
        census_engines=census_engines,
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
        citizen=report,
    )
    return agent, report


__all__ = [
    "PROFILE_PATH",
    "LaptopCitizenError",
    "aesir_grade_for",
    "build_laptop_citizen_agent",
    "clean_neighbours",
    "cuda_entry_for",
    "evaluate_laptop_citizen",
    "load_profile",
    "validate_profile",
    "validate_report",
]
