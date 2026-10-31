"""Licence & provenance — Y-018.

The repo's licence, read and honoured, and every third-party piece
noticed in one place — nothing vendored silently. The register
(``provenance/provenance_register.json``, shaped per item by
``contracts/schemas/provenance_record.schema.json`` and registered
in the Y-016 data manifest like every other standing data file)
names: the project licence itself, the founding documents that were
in the repo before this campaign opened, every dependency
``pyproject.toml`` declares (build, runtime, dev), the full runtime
transitive closure, the language runtime, the standards the code
implements or references, and the one hand-written implementation
(Ed25519) that exists precisely so nothing had to be vendored.

This module makes the register *checkable*, not just readable:

- the project licence in ``LICENSE``, in ``pyproject.toml`` and in
  the register must agree (name and copyright holder);
- every declared dependency must be noticed in the register, with
  the same version constraint;
- every third-party module the code imports directly must be a
  *declared* dependency — importing a package that merely arrives
  transitively is a provenance gap, and this check refuses it.
  (Y-018 found exactly one: ``referencing``, imported by five
  modules since Y-009 but declared nowhere. It is declared now.)
- every path a register entry points at must exist;
- a vendored entry (there are none) must name an existing
  ``vendored_location`` — vendoring is possible, silence is not;
- no ``vendor``/``vendors``/``third_party``/``_vendor`` directory
  may appear in the tree unregistered-by-construction.

Honesty: this is build/test-time paperwork, like Y-016. Licences in
the register were read at Y-018 from the installed distributions'
own metadata, and the checks prove the register agrees with the
repo as it stands — they do not audit the whole internet's
licensing, and no entry claims a legal opinion.
"""

import ast
import json
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

_ROOT = Path(__file__).resolve().parents[2]
_REGISTER_PATH = _ROOT / "provenance" / "provenance_register.json"

# Import name -> distribution name, where they differ. Everything
# imported today maps 1:1; the table exists so a future difference
# is a deliberate, reviewed line, not a silent assumption.
IMPORT_TO_PACKAGE: dict[str, str] = {}

_VENDOR_DIR_NAMES = {"vendor", "vendors", "third_party", "_vendor"}
_DEP_KINDS = {
    "runtime_dependency",
    "build_dependency",
    "dev_dependency",
    "transitive_dependency",
}
_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")
_CONSTRAINT_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(.*)$")


class ProvenanceError(Exception):
    """Any licence/provenance failure: disagreement between the
    licence, the declarations and the register, an unnoticed
    dependency, an undeclared direct import, or silent vendoring."""


def _validator(root: Path, name: str):
    """A validator for one schema, with every contract schema
    registered so relative $refs resolve — the registry pattern
    the earlier slices build."""
    schema_dir = root / "contracts" / "schemas"
    schema = json.loads((schema_dir / name).read_text())
    resources = []
    for path in sorted(schema_dir.glob("*.json")):
        other = json.loads(path.read_text())
        resource = Resource.from_contents(other)
        resources.append((path.name, resource))
        resources.append((other["$id"], resource))
        resources.append((other["$id"].rsplit("/", 1)[0] + "/" + path.name, resource))
    registry = Registry().with_resources(resources)
    return validators.validator_for(schema)(schema, registry=registry)


def validate_items(items: list[dict], root: Path | None = None) -> None:
    """Every register item validates against the provenance-record
    schema, and item ids and names are unique."""
    root = Path(root) if root is not None else _ROOT
    validator = _validator(root, "provenance_record.schema.json")
    for item in items:
        errors = sorted(validator.iter_errors(item), key=str)
        if errors:
            raise ProvenanceError(
                f"provenance item {item.get('item_id', '?')} fails its schema: "
                f"{errors[0].message}"
            )
    ids = [item["item_id"] for item in items]
    if len(ids) != len(set(ids)):
        raise ProvenanceError("provenance register has duplicate item ids")


def load_register(root: Path | None = None) -> dict:
    """Load and item-validate the provenance register."""
    root = Path(root) if root is not None else _ROOT
    path = root / "provenance" / "provenance_register.json"
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvenanceError(f"cannot load provenance register {path}: {exc}") from exc
    if raw.get("register") != "yggdrasil-provenance-register-v1":
        raise ProvenanceError(f"unexpected register version: {raw.get('register')!r}")
    validate_items(raw["items"], root)
    return raw


def _split_requirement(requirement: str) -> tuple[str, str]:
    """'jsonschema>=4.10' -> ('jsonschema', '>=4.10'); extras and
    environment markers are not used in this project's declarations
    and are refused loudly rather than parsed half-way."""
    if ";" in requirement or "[" in requirement:
        raise ProvenanceError(f"requirement shape not supported here: {requirement!r}")
    match = _CONSTRAINT_RE.match(requirement.strip())
    if not match:
        raise ProvenanceError(f"cannot parse requirement: {requirement!r}")
    return match.group(1).lower().replace("_", "-"), match.group(2).strip()


def declared_dependencies(root: Path | None = None) -> dict[str, str]:
    """Every dependency pyproject.toml declares — build-system
    requires, project dependencies, and all optional-dependency
    groups — as {normalised name: constraint}."""
    root = Path(root) if root is not None else _ROOT
    try:
        pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProvenanceError(f"cannot load pyproject.toml: {exc}") from exc
    declared: dict[str, str] = {}
    requirements = list(pyproject.get("build-system", {}).get("requires", []))
    requirements += pyproject.get("project", {}).get("dependencies", [])
    for group in pyproject.get("project", {}).get("optional-dependencies", {}).values():
        requirements += group
    for requirement in requirements:
        name, constraint = _split_requirement(requirement)
        if name in declared and declared[name] != constraint:
            raise ProvenanceError(
                f"dependency {name} declared twice with different constraints"
            )
        declared[name] = constraint
    return declared


def third_party_imports(root: Path | None = None) -> set[str]:
    """The top-level third-party modules imported anywhere in
    src/ or tests/ — standard library (per this interpreter) and
    the project's own package excluded."""
    root = Path(root) if root is not None else _ROOT
    found: set[str] = set()
    for base in ("src", "tests"):
        directory = root / base
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    found.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    found.add(node.module.split(".")[0])
    return {
        name
        for name in found
        if name not in sys.stdlib_module_names and name != "yggdrasil"
    }


def assert_declared_noticed(declared: dict[str, str], items: list[dict]) -> None:
    """Every declared dependency is noticed in the register, under
    a dependency kind, unvendored, with the same constraint."""
    by_name = {item["name"].lower().replace("_", "-"): item for item in items}
    for name, constraint in sorted(declared.items()):
        item = by_name.get(name)
        if item is None:
            raise ProvenanceError(
                f"declared dependency {name!r} is not noticed in the "
                f"provenance register — a dependency lands only with "
                f"its register entry (Y-018)"
            )
        if item["kind"] not in _DEP_KINDS:
            raise ProvenanceError(
                f"declared dependency {name!r} is registered as "
                f"{item['kind']!r}, not a dependency kind"
            )
        if item["vendored"]:
            raise ProvenanceError(f"declared dependency {name!r} claims vendored")
        if item.get("version_constraint") != constraint:
            raise ProvenanceError(
                f"dependency {name!r}: register constraint "
                f"{item.get('version_constraint')!r} != pyproject {constraint!r}"
            )


def assert_imports_declared(imports: set[str], declared: dict[str, str]) -> None:
    """Every directly imported third-party module is a declared
    dependency in its own right — never a transitive hitchhiker."""
    for module in sorted(imports):
        package = IMPORT_TO_PACKAGE.get(module, module).lower().replace("_", "-")
        if package not in declared:
            raise ProvenanceError(
                f"module {module!r} is imported directly but package "
                f"{package!r} is not a declared dependency — declare "
                f"what you import (Y-018)"
            )


@dataclass(frozen=True)
class ProvenanceReport:
    """The check, passed: the licence everything agrees on, who
    holds it, how many pieces are noticed, and how many of them
    are vendored (zero, and checked)."""

    project_licence: str
    copyright_holder: str
    items_noticed: int
    dependencies_declared: int
    vendored_items: int


def check_provenance(root: Path | None = None) -> ProvenanceReport:
    """The whole Y-018 check in one call: register valid, licence
    in agreement everywhere, all declared dependencies noticed,
    all direct imports declared, all named paths present, and no
    silent vendoring."""
    root = Path(root) if root is not None else _ROOT
    register = load_register(root)
    items = register["items"]

    # The project's own licence: LICENSE, pyproject and register agree.
    try:
        licence_text = (root / "LICENSE").read_text()
    except OSError as exc:
        raise ProvenanceError(f"cannot read LICENSE: {exc}") from exc
    first_line = licence_text.splitlines()[0].strip() if licence_text.splitlines() else ""
    holder_match = re.search(r"Copyright \(c\) \d{4} (.+)", licence_text)
    if not first_line.endswith("License") or holder_match is None:
        raise ProvenanceError("LICENSE does not carry a licence title and holder")
    project_licence = first_line.removesuffix(" License")
    holder = holder_match.group(1).strip()
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    declared_licence = (pyproject.get("project", {}).get("license") or {}).get("text")
    if declared_licence != project_licence:
        raise ProvenanceError(
            f"pyproject licence {declared_licence!r} != LICENSE {project_licence!r}"
        )
    licence_entries = [i for i in items if i["kind"] == "project_licence"]
    if len(licence_entries) != 1 or licence_entries[0]["licence"] != project_licence:
        raise ProvenanceError("register must carry exactly one project-licence entry")
    if holder not in licence_entries[0]["origin"]:
        raise ProvenanceError("register's licence entry does not name the holder")

    declared = declared_dependencies(root)
    assert_declared_noticed(declared, items)
    assert_imports_declared(third_party_imports(root), declared)

    vendored = 0
    for item in items:
        for relative in item.get("paths", []):
            if not (root / relative).exists():
                raise ProvenanceError(
                    f"{item['item_id']}: named path does not exist: {relative}"
                )
        if item["vendored"]:
            vendored += 1
            location = item.get("vendored_location")
            if not location or not (root / location).exists():
                raise ProvenanceError(
                    f"{item['item_id']}: vendored piece names no existing "
                    f"vendored_location — vendoring is possible, silence is not"
                )
    for base in (root, root / "src"):
        if not base.is_dir():
            continue
        for path in base.iterdir():
            if path.is_dir() and path.name in _VENDOR_DIR_NAMES:
                raise ProvenanceError(
                    f"unregistered vendor directory in the tree: {path.name}"
                )

    return ProvenanceReport(
        project_licence=project_licence,
        copyright_holder=holder,
        items_noticed=len(items),
        dependencies_declared=len(declared),
        vendored_items=vendored,
    )
