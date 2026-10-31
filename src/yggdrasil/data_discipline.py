"""Data discipline — Y-016.

The rule of this slice: *everything the mesh treats as standing
knowledge — registries, catalogs, profiles, policies — is a
versioned data file with a schema and a validator, never a constant
buried in code.* Until Y-016 the rule held by habit: Y-010's threat
register and Y-012's role catalog were already shaped that way.
This slice makes the habit enforceable:

- ``data/data_manifest.json`` registers every versioned data file
  in the repo — its kind, its path, the field that carries its
  version, the version the manifest expects, the roadmap slice that
  owns it, and the schema(s) it and its items must validate
  against. The manifest is itself a registered, versioned,
  schema-shaped data file (it validates like everything else).
- :class:`DataManifest` validates the whole repo: every registered
  file exists, parses, carries the expected version and owning
  slice, validates against its whole-file schema and/or per-item
  schema, and — the discipline's teeth — *no unregistered data file
  may sit in a data directory* (``roles/``, ``security/``,
  ``data/``, and the future ``profiles/`` / ``policies/`` homes).
  A new registry, profile or policy lands only by registering here
  in the same slice that creates it.
- Versions are ``<stem>-v<N>`` strings. A content change that
  alters meaning is a new version, never a silent in-place edit of
  a version other slices cite: :func:`require_version_bump` refuses
  anything that is not the same stem at a strictly higher ``N``.
  Contract schemas version the same way, in their ``$id``
  (``yggdrasil/contracts/v<N>/...``); :func:`validate_schemas`
  checks every schema in ``contracts/schemas/`` is itself a valid
  JSON Schema carrying such a version.

Honesty: Y-016 validates paperwork at build/test time. No running
mesh loads these files yet — the node agent (Part II) is their
first runtime reader, and until then the manifest is a discipline
the test suite enforces, not a live registry service.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator, validators
from referencing import Registry, Resource

_ROOT = Path(__file__).resolve().parents[2]
_MANIFEST_PATH = _ROOT / "data" / "data_manifest.json"
_SCHEMAS = _ROOT / "contracts" / "schemas"

# Directories whose *.json files are mesh data and must be registered.
# (contracts/schemas/ holds schemas, not data; fixtures in code are code.)
# "provenance" joined the list at Y-018, when its register landed.
DATA_DIRECTORIES = ("roles", "security", "data", "profiles", "policies", "provenance")

_VERSION_RE = re.compile(r"^(?P<stem>[a-z0-9][a-z0-9-]*)-v(?P<n>[0-9]+)$")
_SCHEMA_ID_RE = re.compile(r"/v(?P<n>[0-9]+)(?:/|$)")


class DataError(Exception):
    """Any data-discipline failure: unregistered, unversioned,
    mismatched, or schema-invalid data."""


def _load_schema(root: Path, name: str) -> dict:
    try:
        return json.loads((root / "contracts" / "schemas" / name).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise DataError(f"cannot load schema {name}: {exc}") from exc


def _validator(root: Path, name: str):
    """A validator for one schema, with every contract schema
    registered so relative $refs resolve — the same registry the
    Y-009 contract harness and Y-012 build."""
    schema = _load_schema(root, name)
    resources = []
    schema_dir = root / "contracts" / "schemas"
    for path in sorted(schema_dir.glob("*.json")):
        other = json.loads(path.read_text())
        resource = Resource.from_contents(other)
        resources.append((path.name, resource))
        resources.append((other["$id"], resource))
        resources.append((other["$id"].rsplit("/", 1)[0] + "/" + path.name, resource))
    registry = Registry().with_resources(resources)
    return validators.validator_for(schema)(schema, registry=registry)


def parse_version(value: str) -> tuple[str, int]:
    """Split a ``<stem>-v<N>`` version; anything else is not a
    version under this discipline."""
    match = _VERSION_RE.match(value or "")
    if not match:
        raise DataError(f"not a versioned name (want '<stem>-v<N>'): {value!r}")
    return match.group("stem"), int(match.group("n"))


def require_version_bump(old: str, new: str) -> None:
    """A successor version keeps the stem and strictly raises N.
    Reusing a version, lowering it, or renaming the stem under the
    same file are all refused."""
    old_stem, old_n = parse_version(old)
    new_stem, new_n = parse_version(new)
    if new_stem != old_stem:
        raise DataError(f"version stem changed: {old!r} -> {new!r}")
    if new_n <= old_n:
        raise DataError(f"not a version bump: {old!r} -> {new!r}")


def schema_version(schema: dict) -> int:
    """The version a contract schema carries in its $id."""
    match = _SCHEMA_ID_RE.search(schema.get("$id", ""))
    if not match:
        raise DataError(f"schema $id carries no /v<N>/ version: {schema.get('$id')!r}")
    return int(match.group("n"))


def validate_schemas(root: Path | None = None) -> int:
    """Every file in contracts/schemas/ is itself a valid JSON
    Schema and carries a /v<N>/ version in its $id. Returns the
    number of schemas checked."""
    root = Path(root) if root is not None else _ROOT
    schema_dir = root / "contracts" / "schemas"
    count = 0
    for path in sorted(schema_dir.glob("*.json")):
        try:
            schema = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise DataError(f"cannot load schema {path.name}: {exc}") from exc
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:  # jsonschema's SchemaError, kept broad for message
            raise DataError(f"schema {path.name} is not a valid schema: {exc}") from exc
        schema_version(schema)
        count += 1
    if count == 0:
        raise DataError(f"no schemas found under {schema_dir}")
    return count


@dataclass(frozen=True)
class DataFileReport:
    """One registered data file, validated: what it is, the version
    it actually carries, its content fingerprint, and how many items
    were checked against the item schema (0 for whole-file data)."""

    id: str
    kind: str
    path: str
    version: str
    sha256: str
    items_checked: int


class DataManifest:
    """The register of every versioned data file in the repo."""

    def __init__(self, raw: dict, root: Path):
        self._root = root
        validator = _validator(root, "data_manifest.schema.json")
        errors = sorted(validator.iter_errors(raw), key=str)
        if errors:
            raise DataError(f"data manifest fails its schema: {errors[0].message}")
        self._raw = raw
        self.entries: tuple[dict, ...] = tuple(raw["entries"])
        ids = [entry["id"] for entry in self.entries]
        paths = [entry["path"] for entry in self.entries]
        if len(ids) != len(set(ids)):
            raise DataError("data manifest has duplicate entry ids")
        if len(paths) != len(set(paths)):
            raise DataError("data manifest has duplicate entry paths")

    @classmethod
    def load(cls, root: Path | None = None) -> "DataManifest":
        root = Path(root) if root is not None else _ROOT
        path = root / "data" / "data_manifest.json"
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise DataError(f"cannot load data manifest {path}: {exc}") from exc
        return cls(raw, root)

    def _check_unregistered(self) -> None:
        registered = {entry["path"] for entry in self.entries}
        for dirname in DATA_DIRECTORIES:
            directory = self._root / dirname
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.json")):
                relative = path.relative_to(self._root).as_posix()
                if relative not in registered:
                    raise DataError(
                        f"unregistered data file: {relative} — a registry, profile "
                        f"or policy lands only with a manifest entry (Y-016)"
                    )

    def _validate_entry(self, entry: dict) -> DataFileReport:
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise DataError(f"data file path escapes the repo: {entry['path']}")
        path = self._root / relative
        try:
            text = path.read_text()
            raw = json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            raise DataError(f"cannot load data file {entry['path']}: {exc}") from exc
        if not isinstance(raw, dict):
            raise DataError(f"data file {entry['path']} is not a JSON object")
        parse_version(entry["version"])  # the manifest itself must obey the shape
        actual = raw.get(entry["version_field"])
        if actual != entry["version"]:
            raise DataError(
                f"{entry['path']}: version mismatch — manifest expects "
                f"{entry['version']!r}, file carries {actual!r}"
            )
        if raw.get("roadmap_slice") != entry["roadmap_slice"]:
            raise DataError(
                f"{entry['path']}: roadmap_slice mismatch — manifest says "
                f"{entry['roadmap_slice']}, file says {raw.get('roadmap_slice')!r}"
            )
        if entry["schema"] is not None:
            validator = _validator(self._root, entry["schema"])
            errors = sorted(validator.iter_errors(raw), key=str)
            if errors:
                raise DataError(
                    f"{entry['path']} fails {entry['schema']}: {errors[0].message}"
                )
        items_checked = 0
        if entry["items_field"] is not None:
            items = raw.get(entry["items_field"])
            if not isinstance(items, list) or not items:
                raise DataError(
                    f"{entry['path']}: items field {entry['items_field']!r} "
                    f"is missing or empty"
                )
            item_validator = _validator(self._root, entry["item_schema"])
            for item in items:
                errors = sorted(item_validator.iter_errors(item), key=str)
                if errors:
                    raise DataError(
                        f"{entry['path']}: an item fails {entry['item_schema']}: "
                        f"{errors[0].message}"
                    )
                items_checked += 1
        elif entry["item_schema"] is not None:
            raise DataError(
                f"manifest entry {entry['id']}: item_schema without items_field"
            )
        return DataFileReport(
            id=entry["id"],
            kind=entry["kind"],
            path=entry["path"],
            version=actual,
            sha256=hashlib.sha256(text.encode()).hexdigest(),
            items_checked=items_checked,
        )

    def validate_all(self) -> tuple[DataFileReport, ...]:
        """Validate every registered data file, refuse any
        unregistered one, and return one report per file."""
        self._check_unregistered()
        return tuple(self._validate_entry(entry) for entry in self.entries)


def validate_repo(root: Path | None = None) -> tuple[DataFileReport, ...]:
    """The whole discipline in one call: all contract schemas valid
    and versioned, plus every data file registered and valid."""
    root = Path(root) if root is not None else _ROOT
    validate_schemas(root)
    return DataManifest.load(root).validate_all()
