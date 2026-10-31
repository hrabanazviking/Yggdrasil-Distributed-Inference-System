"""Quantisation fitness — Y-038.

A registry entry (Y-008) says a model *exists* in some quants.
It does not say which of those quants any given device can
actually run — and until this slice, nothing checked: a node
could be handed a quant far too large for its memory, or in a
format whose runtime needs an accelerator the node does not
have, and find out at crash time. This slice is the check,
made *before* placement, in two scopes:

- **Class scope — the registry's ``runnable_on``.** Each quant
  of a model is judged against each device class that carries
  a citizen profile (Y-033–Y-036: pi, laptop, vm, phone),
  using that profile's OWN numbers as the class floor: the
  smallest RAM the profile certifies (``min_ram_gb``) times
  the share its governor allows a model to occupy
  (``max_memory_fraction``); the VRAM floor
  (``min_vram_gb``) and required accelerator where the
  profile names them; and the phone profile's small-model
  ceiling (``max_model_size_gb``) where it names one. The
  floors live in the profiles and are read from them — this
  module keeps no second copy of a class's numbers to drift.
  A class with no citizen profile is judged ``no_class_profile``:
  fitness not evaluated, never assumed. ``runnable_on`` is
  therefore the conservative class guarantee — every machine
  certified under the profile fits the quant — and a bigger
  machine of the class may fit more, which is exactly what
  the node scope decides.
- **Node scope — enforcement at placement.** Each quant is
  judged against ONE node's measured census (Y-022): the
  accelerator kinds actually present, the VRAM actually
  measured (absent VRAM is ``vram_unmeasured_on_node``, never
  read as zero or as plenty), and the RAM actually measured.
  :class:`QuantFitness` is that judge for one node, and the
  model lifecycle (Y-027) accepts it as its ``fitness_checker``
  seam: a load whose quant is unfit for the node is refused
  by name, with every reason, *before* the loader is asked —
  not discovered at crash time.

Two rules do the judging, both in the policy data file
``policies/quant_fitness.json`` (``quant_fitness_policy`` v1,
registered in the Y-016 manifest — a policy, not a fact):

- **Headroom.** Weights are never the whole cost of running a
  model (context, activations, runtime overhead ride along),
  so a quant is fit only when ``size_gb × headroom_factor``
  fits the pool it would run in. The factor is the operator's
  policy number (1.25 as shipped), not a measured constant,
  and reports show the arithmetic so the judgement can be
  re-checked by hand.
- **Format requirements.** The policy names, per format, the
  accelerator its runtime requires (``null`` = runs on CPU
  anywhere): gguf/ggml need none, awq/gptq/exl2 need CUDA,
  mlx needs Metal. A format the policy does not name is
  ``format_unknown`` and NOT runnable — the mesh never
  assumes an unlisted format runs; the operator extends the
  policy when a format is adopted.

Every judgement is a validated ``quant_fitness_verdict`` v1
(``fit`` true only when ``reasons`` is empty — a refusal
always names why), and a model's class-scope judgements are
summarised by the validated ``quant_fitness_report`` v1 whose
``runnable_on`` the store (Y-026) can stamp into registry
entries. No network path exists in this module; fitness is
arithmetic over data already in the tree (registry entries,
profiles, a census), nothing is measured here and no
hardware is claimed — a fit verdict says the numbers allow
the quant, not that an engine has run it.
"""

import json
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"
POLICY_PATH = _ROOT / "policies" / "quant_fitness.json"
PROFILES_DIR = _ROOT / "profiles"

_GIB = 1024**3


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


_POLICY_VALIDATOR = _validator("quant_fitness_policy.schema.json")
_VERDICT_VALIDATOR = _validator("quant_fitness_verdict.schema.json")
_REPORT_VALIDATOR = _validator("quant_fitness_report.schema.json")
_MODEL_ENTRY_VALIDATOR = _validator("model_entry.schema.json")


class QuantFitnessError(Exception):
    """Any fitness failure: a policy or profile that fails its
    schema, a registry entry that fails Y-008's, a quant asked
    about by a name the entry does not carry."""


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise QuantFitnessError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_policy(record: dict) -> dict:
    """One fitness policy against its contract schema."""
    return _validate(_POLICY_VALIDATOR, record, "quant fitness policy")


def load_policy(path: Path | str = POLICY_PATH) -> dict:
    """The shipped fitness policy, validated. Policy is data:
    a deployment may hand the module another file, but it is
    validated the same way."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise QuantFitnessError(f"cannot read quant fitness policy: {exc}") from exc
    return validate_policy(data)


def load_class_profiles(profiles_dir: Path | str = PROFILES_DIR) -> dict:
    """The citizen profiles as the class floors, keyed by
    device class: ``{device_class: profile}`` read from the
    profile files themselves (Y-033–Y-036), so a class's
    numbers live in exactly one place. A directory with no
    profiles yields an empty mapping — every class then
    judges ``no_class_profile``, honestly."""
    profiles: dict[str, dict] = {}
    for path in sorted(Path(profiles_dir).glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise QuantFitnessError(
                f"cannot read citizen profile {path.name}: {exc}"
            ) from exc
        device_class = data.get("device_class")
        if not device_class:
            raise QuantFitnessError(
                f"citizen profile {path.name} names no device_class"
            )
        if device_class in profiles:
            raise QuantFitnessError(
                f"two citizen profiles claim device class {device_class!r}"
            )
        profiles[device_class] = data
    return profiles


def _format_requirement(policy: dict, format_name: str) -> dict | None:
    """The policy's row for one format (matched without case —
    a format is a format), or None when the policy does not
    name it: unknown, never assumed runnable."""
    for row in policy["format_requirements"]:
        if row["format"].lower() == format_name.lower():
            return row
    return None


def _verdict(
    *,
    model: str,
    quant: dict,
    scope: str,
    device_class: str,
    node_id: str | None,
    reasons: list[dict],
) -> dict:
    return _validate(
        _VERDICT_VALIDATOR,
        {
            "version": 1,
            "model": model,
            "quant": {
                "name": quant["name"],
                "format": quant["format"],
                "size_gb": quant["size_gb"],
            },
            "scope": scope,
            "device_class": device_class,
            "node_id": node_id,
            "fit": not reasons,
            "reasons": reasons,
        },
        "quant fitness verdict",
    )


def _reason(code: str, detail: str) -> dict:
    return {"code": code, "detail": detail}


def evaluate_quant_for_class(
    model: str,
    quant: dict,
    device_class: str,
    *,
    policy: dict,
    profiles: dict,
) -> dict:
    """Judge one quant for one device class against that
    class's citizen-profile floor. Fit here is the class
    guarantee: the *smallest* machine the profile certifies
    runs the quant with the policy's headroom to spare."""
    profile = profiles.get(device_class)
    if profile is None:
        return _verdict(
            model=model,
            quant=quant,
            scope="class",
            device_class=device_class,
            node_id=None,
            reasons=[
                _reason(
                    "no_class_profile",
                    f"no citizen profile certifies device class {device_class!r} — "
                    "fitness for this class is not evaluated, never assumed",
                )
            ],
        )
    reasons: list[dict] = []
    requirement = _format_requirement(policy, quant["format"])
    if requirement is None:
        reasons.append(
            _reason(
                "format_unknown",
                f"format {quant['format']!r} is not named in the fitness policy — "
                "an unlisted format is never assumed runnable",
            )
        )
        return _verdict(
            model=model,
            quant=quant,
            scope="class",
            device_class=device_class,
            node_id=None,
            reasons=reasons,
        )
    headroom = policy["headroom_factor"]
    needed = quant["size_gb"] * headroom
    ceiling = profile.get("max_model_size_gb")
    if ceiling is not None and quant["size_gb"] > ceiling:
        reasons.append(
            _reason(
                "exceeds_class_model_ceiling",
                f"quant size {quant['size_gb']} GiB exceeds the {device_class} "
                f"profile's small-model ceiling of {ceiling} GiB",
            )
        )
    required = requirement["requires_accelerator"]
    if required is not None:
        class_accelerators = (
            {profile["required_accelerator_kind"]}
            if profile.get("required_accelerator_kind")
            else set()
        )
        if required not in class_accelerators:
            reasons.append(
                _reason(
                    "accelerator_not_in_class",
                    f"format {quant['format']!r} requires a {required} runtime, "
                    f"but the {device_class} citizen profile certifies no "
                    f"{required} accelerator for its class",
                )
            )
        else:
            vram_floor = profile.get("min_vram_gb")
            if vram_floor is not None and needed > vram_floor:
                reasons.append(
                    _reason(
                        "exceeds_class_vram_floor",
                        f"quant needs {needed:.3f} GiB with headroom "
                        f"({quant['size_gb']} × {headroom}) but the "
                        f"{device_class} profile's VRAM floor is {vram_floor} GiB",
                    )
                )
    else:
        ram_share = profile["min_ram_gb"] * profile["governor_policy"][
            "max_memory_fraction"
        ]
        if needed > ram_share:
            reasons.append(
                _reason(
                    "exceeds_class_ram_share",
                    f"quant needs {needed:.3f} GiB with headroom "
                    f"({quant['size_gb']} × {headroom}) but the smallest "
                    f"certified {device_class} machine gives a model "
                    f"{ram_share:.3f} GiB (min_ram_gb "
                    f"{profile['min_ram_gb']} × governor share "
                    f"{profile['governor_policy']['max_memory_fraction']})",
                )
            )
    return _verdict(
        model=model,
        quant=quant,
        scope="class",
        device_class=device_class,
        node_id=None,
        reasons=reasons,
    )


def evaluate_quant_for_node(
    model: str,
    quant: dict,
    *,
    device_class: str,
    census: dict,
    policy: dict,
    profiles: dict,
) -> dict:
    """Judge one quant for one real node against its measured
    census. This is the placement-time enforcement scope: the
    node's own accelerators and memory, as measured — a bigger
    machine of a class fits more than the class floor promises,
    and a smaller one is refused here however the class judged."""
    reasons: list[dict] = []
    requirement = _format_requirement(policy, quant["format"])
    if requirement is None:
        reasons.append(
            _reason(
                "format_unknown",
                f"format {quant['format']!r} is not named in the fitness policy — "
                "an unlisted format is never assumed runnable",
            )
        )
        return _verdict(
            model=model,
            quant=quant,
            scope="node",
            device_class=device_class,
            node_id=census["node_id"],
            reasons=reasons,
        )
    profile = profiles.get(device_class)
    if profile is not None:
        ceiling = profile.get("max_model_size_gb")
        if ceiling is not None and quant["size_gb"] > ceiling:
            reasons.append(
                _reason(
                    "exceeds_class_model_ceiling",
                    f"quant size {quant['size_gb']} GiB exceeds the "
                    f"{device_class} profile's small-model ceiling of "
                    f"{ceiling} GiB — the ceiling binds every node of the class",
                )
            )
    headroom = policy["headroom_factor"]
    needed = quant["size_gb"] * headroom
    required = requirement["requires_accelerator"]
    if required is not None:
        present = {
            acc["kind"] for acc in census.get("accelerators", []) if acc["kind"] != "none"
        }
        if required not in present:
            reasons.append(
                _reason(
                    "accelerator_missing_on_node",
                    f"format {quant['format']!r} requires a {required} runtime, "
                    f"but node {census['node_id']!r} measured accelerators: "
                    f"{sorted(present) or 'none'}",
                )
            )
        elif "vram_gb" not in census:
            reasons.append(
                _reason(
                    "vram_unmeasured_on_node",
                    f"node {census['node_id']!r} measured no VRAM figure — "
                    "an accelerator quant is not placed on an unmeasured pool",
                )
            )
        elif needed > census["vram_gb"]:
            reasons.append(
                _reason(
                    "exceeds_node_vram",
                    f"quant needs {needed:.3f} GiB with headroom "
                    f"({quant['size_gb']} × {headroom}) but node "
                    f"{census['node_id']!r} measured {census['vram_gb']} GiB VRAM",
                )
            )
    elif needed > census["ram_gb"]:
        reasons.append(
            _reason(
                "exceeds_node_ram",
                f"quant needs {needed:.3f} GiB with headroom "
                f"({quant['size_gb']} × {headroom}) but node "
                f"{census['node_id']!r} measured {census['ram_gb']} GiB RAM",
            )
        )
    return _verdict(
        model=model,
        quant=quant,
        scope="node",
        device_class=device_class,
        node_id=census["node_id"],
        reasons=reasons,
    )


def evaluate_entry(
    entry: dict,
    *,
    policy: dict | None = None,
    profiles: dict | None = None,
) -> dict:
    """The class-scope report for one validated Y-008 registry
    entry: every quant judged for every class that carries a
    citizen profile (plus the entry's own ``runnable_on``
    classes re-judged — a class named without a profile is
    reported ``no_class_profile``, never quietly dropped),
    and the ``runnable_on`` those verdicts earn: a class is
    runnable when at least one of the model's quants is fit
    for it."""
    entry = _validate(_MODEL_ENTRY_VALIDATOR, dict(entry), "model entry")
    policy = validate_policy(dict(policy)) if policy is not None else load_policy()
    profiles = profiles if profiles is not None else load_class_profiles()
    classes = sorted(set(profiles) | set(entry.get("runnable_on", [])))
    verdicts = [
        evaluate_quant_for_class(
            entry["name"], quant, device_class, policy=policy, profiles=profiles
        )
        for device_class in classes
        for quant in entry["quants"]
    ]
    runnable_on = sorted(
        {v["device_class"] for v in verdicts if v["fit"]}
    )
    return _validate(
        _REPORT_VALIDATOR,
        {
            "version": 1,
            "model": entry["name"],
            "verdicts": verdicts,
            "runnable_on": runnable_on,
        },
        "quant fitness report",
    )


def with_runnable_on(
    entry: dict,
    *,
    policy: dict | None = None,
    profiles: dict | None = None,
) -> dict:
    """A copy of a registry entry with ``runnable_on`` filled
    from the class-scope verdicts — the registry data Y-038
    exists to produce, replacing the empty list the adapters
    and the store honestly left blank until now."""
    record = dict(entry)
    record["runnable_on"] = evaluate_entry(
        record, policy=policy, profiles=profiles
    )["runnable_on"]
    return _validate(_MODEL_ENTRY_VALIDATOR, record, "model entry")


def validate_verdict(record: dict) -> dict:
    """One fitness verdict against its contract schema."""
    return _validate(_VERDICT_VALIDATOR, record, "quant fitness verdict")


def validate_report(record: dict) -> dict:
    """One class-scope fitness report against its schema."""
    return _validate(_REPORT_VALIDATOR, record, "quant fitness report")


class QuantFitness:
    """One node's placement-time judge: the policy, the class
    profiles, this node's device class, and its census (a
    measured dict, or a callable returning the current one —
    the lifecycle asks at load time, so a callable lets a
    refreshed census judge the next load).

    The lifecycle's ``fitness_checker`` seam calls this object
    with a Y-026 store entry; :meth:`check_store_entry` is
    that call. A store entry carries exactly one quant — the
    one its file measures — so enforcement judges the quant
    the node would actually load, never a sibling quant the
    registry happens to list.
    """

    def __init__(
        self,
        node_id: str,
        *,
        device_class: str,
        census,
        policy: dict | None = None,
        profiles: dict | None = None,
    ):
        self.node_id = node_id
        self.device_class = device_class
        self._census = census
        self.policy = (
            validate_policy(dict(policy)) if policy is not None else load_policy()
        )
        self.profiles = profiles if profiles is not None else load_class_profiles()

    def _current_census(self) -> dict:
        return self._census() if callable(self._census) else self._census

    def verdict_for_quant(self, model: str, quant: dict) -> dict:
        """This node's verdict on one quant of one model."""
        return evaluate_quant_for_node(
            model,
            quant,
            device_class=self.device_class,
            census=self._current_census(),
            policy=self.policy,
            profiles=self.profiles,
        )

    def verdicts_for_entry(self, entry: dict) -> list[dict]:
        """This node's verdicts on every quant a registry
        entry lists — the operator's pre-flight view before
        anyone tries to place the model here."""
        entry = _validate(_MODEL_ENTRY_VALIDATOR, dict(entry), "model entry")
        return [self.verdict_for_quant(entry["name"], q) for q in entry["quants"]]

    def check_store_entry(self, store_entry: dict) -> dict:
        """The lifecycle seam: judge the quant a store entry
        pins (name / quant / format / size_bytes), as this
        node's node-scope verdict."""
        quant = {
            "name": store_entry["quant"],
            "format": store_entry["format"],
            "size_gb": store_entry["size_bytes"] / _GIB,
        }
        return self.verdict_for_quant(store_entry["name"], quant)

    __call__ = check_store_entry

    def runnable_on_for(self, entry: dict) -> list[str]:
        """The class-scope ``runnable_on`` for one registry
        entry, under this judge's policy and profiles — the
        store's stamping helper (Y-026)."""
        return evaluate_entry(entry, policy=self.policy, profiles=self.profiles)[
            "runnable_on"
        ]

    def summary(self) -> dict:
        """The judge in one summary for an agent's status: the
        policy it enforces, the class it judges for, and the
        formats it knows. Not a contract shape."""
        return {
            "policy": self.policy["policy"],
            "device_class": self.device_class,
            "headroom_factor": self.policy["headroom_factor"],
            "formats_known": sorted(
                row["format"] for row in self.policy["format_requirements"]
            ),
            "classes_profiled": sorted(self.profiles),
        }


__all__ = [
    "POLICY_PATH",
    "PROFILES_DIR",
    "QuantFitness",
    "QuantFitnessError",
    "evaluate_entry",
    "evaluate_quant_for_class",
    "evaluate_quant_for_node",
    "load_class_profiles",
    "load_policy",
    "validate_policy",
    "validate_report",
    "validate_verdict",
    "with_runnable_on",
]
