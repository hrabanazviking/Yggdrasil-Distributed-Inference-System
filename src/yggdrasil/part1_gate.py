"""Part I gate — Y-020.

The gate Part I must pass before the trunk rises (Part II):
contracts + ADRs + simulator skeleton merged, the concordance
published in-repo, and zero inference code yet — the roots hold
before the trunk rises. This module makes that gate *checkable*,
the way Y-016/Y-018/Y-019 made their paperwork checkable, instead
of leaving it a claim in a commit message:

- every slice Y-001 – Y-019 names the files it was banked on, and
  every one of those files must exist and be non-empty;
- the founding README must be byte-for-byte the document the
  campaign opened on (its md5 is the campaign's standing check);
- the standing checks the earlier slices built must all still
  pass from here: every contract schema valid and versioned
  (Y-009/Y-016), every registered data file valid (Y-016), the
  licence & provenance check green with nothing vendored
  (Y-018), and the campaign file a true mirror of the roadmap
  naming Y-020 as the next unbanked slice after Y-001 – Y-019
  (Y-019);
- the CI workflow must still be manual-dispatch only (Y-009,
  Volmarr's standing default) and no Docker file may exist
  anywhere in the tree (standing no-Docker law);
- the simulator skeleton must actually run: three fake nodes,
  one task end to end, a SIMULATED result with provenance
  (Y-014/Y-015);
- zero inference code: no source module imports a real engine
  SDK, a model-loading library, or a network/process client,
  and the only engine-shaped code in the tree is the fake
  engines, which must still declare themselves simulated.
  Real engine adapters are Part II work (ADR-004: wrapped,
  never re-implemented); a module that quietly grew one in
  Part I fails this gate by name.

Honesty: this is a build-time gate over the repo as it stands.
It proves the roots are present, consistent, and free of
inference code — it proves nothing about hardware, and the
simulator run inside it is SIMULATED, like everything in the
wind tunnel.
"""

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path

from yggdrasil.campaign import first_unbanked, verify_campaign
from yggdrasil.data_discipline import validate_repo, validate_schemas
from yggdrasil.provenance import check_provenance
from yggdrasil.simulator import Simulator

_ROOT = Path(__file__).resolve().parents[2]

README_MD5_AT_OPEN = "89a37a4e18cab9a998e1751c32e7bfd5"

# Each Part I slice and the files it was banked on. A slice whose
# deliverable is missing or empty has not landed, whatever any
# ledger says — the gate reads the tree, not the log.
PART1_DELIVERABLES: dict[str, tuple[str, ...]] = {
    "Y-001": ("docs/SPEC_CONCORDANCE.md",),
    "Y-002": ("docs/adr/ADR-001-language.md",),
    "Y-003": ("docs/adr/ADR-002-transport.md",),
    "Y-004": ("docs/adr/ADR-003-state.md",),
    "Y-005": ("docs/adr/ADR-004-engines.md",),
    "Y-006": (
        "contracts/node_contract.md",
        "contracts/schemas/node_identity.schema.json",
        "contracts/schemas/capability_census.schema.json",
        "contracts/schemas/health_report.schema.json",
    ),
    "Y-007": (
        "contracts/task_contract.md",
        "contracts/schemas/task_envelope.schema.json",
        "contracts/schemas/result_envelope.schema.json",
        "contracts/schemas/stream_event.schema.json",
        "contracts/schemas/error_codes.schema.json",
    ),
    "Y-008": (
        "contracts/registry_contract.md",
        "contracts/schemas/model_entry.schema.json",
        "contracts/schemas/engine_capability.schema.json",
    ),
    "Y-009": (
        "pyproject.toml",
        "deploy/yggdrasil-node.service",
        ".github/workflows/ci.yml",
    ),
    "Y-010": (
        "docs/THREAT_MODEL.md",
        "security/threat_register.json",
        "contracts/schemas/threat_record.schema.json",
    ),
    "Y-011": (
        "docs/IDENTITY.md",
        "src/yggdrasil/identity.py",
        "src/yggdrasil/ed25519.py",
        "contracts/schemas/node_certificate.schema.json",
    ),
    "Y-012": (
        "docs/ROLES.md",
        "roles/role_catalog.json",
        "src/yggdrasil/roles.py",
        "contracts/schemas/role_definition.schema.json",
        "contracts/schemas/role_assignment.schema.json",
    ),
    "Y-013": (
        "contracts/observability_contract.md",
        "docs/OBSERVABILITY.md",
        "src/yggdrasil/observability.py",
        "contracts/schemas/log_record.schema.json",
        "contracts/schemas/meter_sample.schema.json",
        "contracts/schemas/event_envelope.schema.json",
    ),
    "Y-014": ("docs/SIMULATOR.md", "src/yggdrasil/simulator.py"),
    "Y-015": (
        "docs/FAKE_ENGINES.md",
        "src/yggdrasil/fake_engines.py",
        "contracts/schemas/fake_engine.schema.json",
    ),
    "Y-016": (
        "docs/DATA_DISCIPLINE.md",
        "data/data_manifest.json",
        "src/yggdrasil/data_discipline.py",
        "contracts/schemas/data_manifest.schema.json",
        "contracts/schemas/data_file_entry.schema.json",
    ),
    "Y-017": (
        "docs/OPERATORS_DOOR.md",
        "src/yggdrasil/cli.py",
        "src/yggdrasil/__main__.py",
    ),
    "Y-018": (
        "docs/PROVENANCE.md",
        "provenance/provenance_register.json",
        "src/yggdrasil/provenance.py",
        "contracts/schemas/provenance_record.schema.json",
    ),
    "Y-019": (
        "docs/CAMPAIGN_FILE.md",
        "data/campaign.json",
        "src/yggdrasil/campaign.py",
        "contracts/schemas/campaign_file.schema.json",
        "contracts/schemas/campaign_part.schema.json",
        "contracts/schemas/campaign_slice.schema.json",
    ),
}

PART1_SLICES = tuple(PART1_DELIVERABLES)  # Y-001 .. Y-019, in order

# Real inference / model / network machinery. None of this may be
# imported by src/ in Part I: engine SDKs and model loaders are
# Part II adapters, and a control plane with no fabric yet
# (Part III) has no business holding a network or process client.
BANNED_IMPORTS = {
    "openai",
    "ollama",
    "llama_cpp",
    "transformers",
    "torch",
    "tensorflow",
    "jax",
    "onnxruntime",
    "vllm",
    "requests",
    "httpx",
    "aiohttp",
    "socket",
    "subprocess",
    "urllib",
    "http",
}

_DOCKER_NAMES = {"dockerfile", "docker-compose.yml", "docker-compose.yaml", ".dockerignore"}
_SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}


class GateError(Exception):
    """Any Part I gate failure: a deliverable missing, a standing
    check red, the founding document altered, or inference code
    found where the road says none exists yet."""


def check_deliverables(root: Path | None = None) -> int:
    """Every file every Part I slice was banked on exists and is
    non-empty. Returns the number of files checked."""
    root = Path(root) if root is not None else _ROOT
    checked = 0
    for slice_id, paths in PART1_DELIVERABLES.items():
        for relative in paths:
            path = root / relative
            if not path.is_file():
                raise GateError(f"{slice_id}: deliverable missing: {relative}")
            if path.stat().st_size == 0:
                raise GateError(f"{slice_id}: deliverable is empty: {relative}")
            checked += 1
    return checked


def check_readme_unchanged(root: Path | None = None) -> str:
    """The founding spec README, md5-checked against the value
    recorded when the campaign opened. The spec is Volmarr's
    document; this campaign never touches it."""
    root = Path(root) if root is not None else _ROOT
    try:
        digest = hashlib.md5((root / "README.md").read_bytes()).hexdigest()
    except OSError as exc:
        raise GateError(f"cannot read README.md: {exc}") from exc
    if digest != README_MD5_AT_OPEN:
        raise GateError(
            f"README.md md5 {digest} != campaign-open {README_MD5_AT_OPEN} "
            f"— the founding document must not change"
        )
    return digest


def check_concordance(root: Path | None = None) -> int:
    """The concordance is published in-repo (Y-001): it maps the
    spec and names its deferred items with reasons, in so many
    words. Returns the number of mapping rows counted."""
    root = Path(root) if root is not None else _ROOT
    try:
        text = (root / "docs" / "SPEC_CONCORDANCE.md").read_text()
    except OSError as exc:
        raise GateError(f"cannot read the concordance: {exc}") from exc
    if "## Deferred, with reasons" not in text:
        raise GateError("concordance names no deferred section — deferrals must be explicit")
    rows = sum(1 for line in text.splitlines() if line.startswith("| ") and "---" not in line)
    if rows < 30:
        raise GateError(f"concordance carries only {rows} mapping rows — the spec is unmapped")
    return rows


def check_ci_and_no_docker(root: Path | None = None) -> None:
    """CI stays manual-dispatch only (Y-009) and the tree holds
    no Docker file at all (standing no-Docker law)."""
    root = Path(root) if root is not None else _ROOT
    try:
        ci = (root / ".github" / "workflows" / "ci.yml").read_text()
    except OSError as exc:
        raise GateError(f"cannot read CI workflow: {exc}") from exc
    if "workflow_dispatch" not in ci:
        raise GateError("CI workflow is not manual-dispatch — Volmarr's standing default")
    for trigger in ("push:", "pull_request:", "schedule:"):
        if trigger in ci:
            raise GateError(f"CI workflow carries an automatic trigger: {trigger}")
    for path in root.rglob("*"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.is_file() and path.name.lower() in _DOCKER_NAMES:
            raise GateError(f"Docker file in the tree (standing no-Docker law): {path}")


def source_imports(root: Path | None = None) -> dict[str, set[str]]:
    """Top-level imports per source module, read from the AST —
    names in comments, docstrings, or data do not count."""
    root = Path(root) if root is not None else _ROOT
    found: dict[str, set[str]] = {}
    for path in sorted((root / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module.split(".")[0])
        found[path.name] = modules
    return found


def check_no_inference_code(root: Path | None = None) -> int:
    """Zero inference code yet: no source module imports banned
    engine/model/network machinery, and the one engine-shaped
    module — the fake engines — declares itself simulated.
    Returns the number of source modules scanned."""
    root = Path(root) if root is not None else _ROOT
    imports = source_imports(root)
    for module, names in sorted(imports.items()):
        banned = sorted(names & BANNED_IMPORTS)
        if banned:
            raise GateError(
                f"{module} imports {banned} — real inference/network code "
                f"is Part II/III work; Part I holds contracts and stubs only"
            )
    try:
        fake = (root / "src" / "yggdrasil" / "fake_engines.py").read_text()
    except OSError as exc:
        raise GateError(f"cannot read fake engines: {exc}") from exc
    if "is_simulated = True" not in fake or '"simulated": True' not in fake:
        raise GateError("fake engines no longer declare themselves SIMULATED")
    return len(imports)


def check_simulator_skeleton(root: Path | None = None) -> int:
    """The wind tunnel actually turns: three fake nodes, one task
    end to end, a SIMULATED result naming its node. Purely
    in-process; `root` is accepted for symmetry and unused —
    the simulator holds no files."""
    del root
    sim = Simulator()
    sim.spawn(3, models=("gate-model",))
    task = {
        "id": "3f6f1b2c-9d4e-4c5a-8b7d-000000000020",
        "kind": "chat.generate",
        "payload": {"prompt": "gate check"},
        "caller": "part1-gate",
        "criticality": "standard",
        "requirements": {"model": "gate-model", "capabilities": ["generate"]},
    }
    result, stream = sim.submit(task)
    if result["status"] != "completed" or result["result"].get("simulated") is not True:
        raise GateError(f"simulator skeleton did not complete a SIMULATED task: {result}")
    if [event["event"] for event in stream] != ["accepted", "started", "completed"]:
        raise GateError(f"simulator stream shape broke at the gate: {stream}")
    if sim.transport != "loopback-simulated" or sim.network_access is not False:
        raise GateError("simulator no longer confines itself to the loopback wind tunnel")
    return len(sim.node_ids())


@dataclass(frozen=True)
class GateReport:
    """The gate, passed: what was re-measured, in numbers a
    reader can re-measure from the tree."""

    slices_checked: int
    deliverable_files: int
    concordance_rows: int
    schemas_checked: int
    data_files: int
    provenance_items: int
    vendored_items: int
    campaign_parts: int
    campaign_slices: int
    next_slice: str
    source_modules: int
    simulator_nodes: int
    readme_md5: str


def check_part1_gate(root: Path | None = None) -> GateReport:
    """The whole Y-020 gate in one call: deliverables present,
    founding document untouched, concordance published, every
    standing check from Y-009/Y-016/Y-018/Y-019 still green, CI
    manual-only, no Docker, the simulator skeleton running, and
    zero inference code in the tree."""
    root = Path(root) if root is not None else _ROOT
    files = check_deliverables(root)
    readme_md5 = check_readme_unchanged(root)
    rows = check_concordance(root)
    schemas = validate_schemas(root)
    data_files = len(validate_repo(root))
    provenance = check_provenance(root)
    if provenance.vendored_items != 0:
        raise GateError(f"provenance reports {provenance.vendored_items} vendored items")
    campaign = verify_campaign(root)
    nxt = first_unbanked(campaign, PART1_SLICES)
    if nxt is None or nxt["id"] != "Y-020":
        raise GateError(
            f"campaign mirror names next slice {nxt and nxt['id']!r}, not Y-020 — "
            f"the gate is being run from the wrong point on the road"
        )
    check_ci_and_no_docker(root)
    modules = check_no_inference_code(root)
    nodes = check_simulator_skeleton(root)
    return GateReport(
        slices_checked=len(PART1_DELIVERABLES),
        deliverable_files=files,
        concordance_rows=rows,
        schemas_checked=schemas,
        data_files=data_files,
        provenance_items=provenance.items_noticed,
        vendored_items=provenance.vendored_items,
        campaign_parts=campaign["total_parts"],
        campaign_slices=campaign["total_slices"],
        next_slice=nxt["id"],
        source_modules=modules,
        simulator_nodes=nodes,
        readme_md5=readme_md5,
    )
