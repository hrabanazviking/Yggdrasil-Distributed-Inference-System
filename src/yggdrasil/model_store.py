"""Model store — Y-026.

The Aesir wave discipline, as a store: a model is *present* on
a node only when its hash verifies. A filename, a directory
listing, an engine's shelf, a registry entry — none of those is
presence. Presence is a SHA-256, recomputed from the file's own
bytes at the moment presence is asked about, matching the hash
pinned when the file entered the store. A file tampered with,
truncated, swapped, or deleted after registration is *not
present*, however convincing its name — it is reported by its
true status (``corrupt`` or ``missing``), never silently
dropped and never quietly still counted.

The store is a directory on the node (the *root*) holding the
model files plus one index file, ``model_store_index.json``,
shaped by ``contracts/schemas/model_store_index.schema.json``:
every entry (``model_store_entry.schema.json``) pins a model's
name, its path relative to the root, the SHA-256 and byte size
measured at registration, and the registry facts the operator
declared (family, quant, format, capabilities). Those declared
facts describe the file; they are never evidence about it —
the hash is the evidence, and it is re-measured, never trusted
from the index.

Operations:

- ``add(source, ...)`` copies a file into the store (under
  ``files/``, named from the model name + quant + format so two
  quants of one family never collide), hashes the copy, and
  registers it. Registering the same name with the same bytes
  is idempotent; the same name with *different* bytes is
  refused by name — a name may not be quietly re-pointed at
  other weights. Empty files are refused: zero bytes is not a
  model.
- ``register_existing(relative_path, ...)`` registers a file
  already under the root in place, hashing it where it lies.
- ``verify(name)`` / ``verify_all()`` re-hash and report each
  model's status honestly: ``verified``, ``corrupt`` (bytes or
  size differ from the pin), or ``missing``.
- ``present_models()`` — and its census alias
  ``model_names_for_census()`` — returns the names that verify
  *right now*, in registration order. Every call re-hashes:
  presence is a measurement, not a cached claim. This is the
  callable the census collector (Y-022) and the node agent
  accept for ``models_present``, so a census never lists a
  model whose bytes no longer match its pin.
- ``model_entries()`` builds validated Y-008 registry entries
  for verified models only (``runnable_on`` stays empty —
  quantisation fitness is Y-038's word, not the store's).
- ``unregister(name)`` removes the index entry only; the file
  stays on disk, stated plainly, until an operator removes it.

Paths in the index must stay under the root: an entry whose
path escapes (``..``, an absolute path, a symlink out) is
refused at load and at registration. The index is rewritten
atomically (temp file + replace) so a crash mid-write cannot
leave a half index claiming half a store.

Honesty: hashing proves the bytes are the bytes that were
registered. It does not prove the weights are good, safe, or
the model their name claims — provenance of the source is the
operator's and Y-037's (fetch duty) concern. No file is fetched
here, no engine loads anything here (load/unload is Y-027),
and nothing in this module touches the network.
"""

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

STORE_VERSION = "yggdrasil-model-store-v1"
INDEX_NAME = "model_store_index.json"
_CHUNK = 1024 * 1024  # hash in 1 MiB reads — a model is never loaded whole


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


_ENTRY_VALIDATOR = _validator("model_store_entry.schema.json")
_INDEX_VALIDATOR = _validator("model_store_index.schema.json")
_MODEL_ENTRY_VALIDATOR = _validator("model_entry.schema.json")


class ModelStoreError(Exception):
    """Any store failure: a bad registration, an index that
    fails its schema, a path escaping the store, a name
    re-pointed at different bytes."""


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise ModelStoreError(f"{kind} fails its schema: {errors[0].message}")
    return record


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def hash_file(path: Path) -> tuple[str, int]:
    """SHA-256 + byte size of a file, streamed in chunks — the
    measurement every presence claim in this module rests on."""
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def validate_store_entry(record: dict) -> dict:
    """One store entry against its contract schema."""
    return _validate(_ENTRY_VALIDATOR, record, "model store entry")


class ModelStore:
    """One node's model store: hash-pinned files under a root.

    The clock is injectable (``now``) so registration timestamps
    are testable; hashing never depends on it.
    """

    def __init__(self, root: Path | str, *, now=None):
        self._root = Path(root)
        self._now = now or (lambda: datetime.now(UTC))
        self._entries: list[dict] = []
        self._load()

    # -- index ------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    @property
    def index_path(self) -> Path:
        return self._root / INDEX_NAME

    def _load(self) -> None:
        if not self.index_path.exists():
            self._entries = []
            return
        try:
            data = json.loads(self.index_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ModelStoreError(f"cannot read model store index: {exc}") from exc
        if not isinstance(data, dict):
            raise ModelStoreError("model store index is not a JSON object")
        _validate(_INDEX_VALIDATOR, data, "model store index")
        seen: set[str] = set()
        for entry in data["models"]:
            if entry["name"] in seen:
                raise ModelStoreError(
                    f"model store index names {entry['name']!r} twice"
                )
            seen.add(entry["name"])
            self._resolve(entry["path"])  # an escaping path refuses the load
        self._entries = list(data["models"])

    def _save(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        index = _validate(
            _INDEX_VALIDATOR,
            {"store": STORE_VERSION, "models": self._entries},
            "model store index",
        )
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.index_path)

    def _resolve(self, relative: str) -> Path:
        """An entry's path, proven to stay under the root."""
        if Path(relative).is_absolute():
            raise ModelStoreError(f"model path {relative!r} is absolute — refused")
        resolved_root = self._root.resolve()
        resolved = (resolved_root / relative).resolve()
        if resolved != resolved_root and resolved_root not in resolved.parents:
            raise ModelStoreError(
                f"model path {relative!r} escapes the store root — refused"
            )
        return resolved

    # -- registration -------------------------------------------------

    def _register(
        self,
        relative: str,
        *,
        name: str,
        family: str,
        quant: str,
        format: str,
        capabilities,
    ) -> dict:
        path = self._resolve(relative)
        if not path.is_file():
            raise ModelStoreError(f"model file {relative!r} does not exist in the store")
        sha256, size = hash_file(path)
        if size == 0:
            raise ModelStoreError(f"model file {relative!r} is empty — not a model")
        entry = _validate(
            _ENTRY_VALIDATOR,
            {
                "name": name,
                "path": relative,
                "sha256": sha256,
                "size_bytes": size,
                "family": family,
                "format": format,
                "quant": quant,
                "capabilities": list(capabilities),
                "registered_at": _fmt(self._now()),
            },
            "model store entry",
        )
        for existing in self._entries:
            if existing["name"] == name:
                if existing["sha256"] == sha256:
                    return dict(existing)  # same name, same bytes: idempotent
                raise ModelStoreError(
                    f"model {name!r} is already registered with different bytes "
                    f"(pinned sha256 {existing['sha256']}) — a name may not be "
                    "re-pointed at other weights; unregister it first"
                )
        self._entries.append(entry)
        self._save()
        return dict(entry)

    def add(
        self,
        source: Path | str,
        *,
        name: str,
        family: str,
        quant: str,
        format: str,
        capabilities=("generate",),
    ) -> dict:
        """Copy a model file into the store and register it,
        hash-pinned. The copy lands under ``files/`` with a name
        built from model + quant + format, so quants never
        collide. The registered hash is measured on the *copy* —
        the bytes the node actually holds."""
        source = Path(source)
        if not source.is_file():
            raise ModelStoreError(f"model source {source} does not exist")
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)
        relative = f"files/{safe}--{quant}.{format}"
        dest = self._resolve(relative)
        existing = next((e for e in self._entries if e["name"] == name), None)
        if existing is not None and existing["path"] != relative:
            raise ModelStoreError(
                f"model {name!r} is already registered at {existing['path']!r}"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        if existing is not None:
            # A registered name is never re-pointed: refuse BEFORE
            # any copy can touch the pinned file.
            source_sha, _ = hash_file(source)
            if existing["sha256"] != source_sha:
                raise ModelStoreError(
                    f"model {name!r} is already registered with different bytes "
                    f"(pinned sha256 {existing['sha256']}) — a name may not be "
                    "re-pointed at other weights; unregister it first"
                )
        if not dest.exists() or hash_file(dest) != hash_file(source):
            shutil.copyfile(source, dest)
        return self._register(
            relative,
            name=name,
            family=family,
            quant=quant,
            format=format,
            capabilities=capabilities,
        )

    def register_existing(
        self,
        relative_path: str,
        *,
        name: str,
        family: str,
        quant: str,
        format: str,
        capabilities=("generate",),
    ) -> dict:
        """Register a file already lying under the store root,
        hashing it in place."""
        return self._register(
            relative_path,
            name=name,
            family=family,
            quant=quant,
            format=format,
            capabilities=capabilities,
        )

    def unregister(self, name: str) -> dict:
        """Remove a name from the index. The file itself stays
        on disk — unregistering is a bookkeeping act, not a
        deletion, and this method does not pretend otherwise."""
        for i, entry in enumerate(self._entries):
            if entry["name"] == name:
                removed = self._entries.pop(i)
                self._save()
                return dict(removed)
        raise ModelStoreError(f"model {name!r} is not registered in this store")

    # -- verification: presence is a measurement ----------------------

    def entries(self) -> list[dict]:
        """Every registered entry, in registration order —
        registered, not necessarily present (see verify)."""
        return [dict(entry) for entry in self._entries]

    def entry(self, name: str) -> dict:
        for entry in self._entries:
            if entry["name"] == name:
                return dict(entry)
        raise ModelStoreError(f"model {name!r} is not registered in this store")

    def verify(self, name: str) -> dict:
        """Re-hash one model's file and report its true status:
        ``verified`` (bytes match the pin), ``corrupt`` (the
        file is there but its bytes or size differ), or
        ``missing`` (no file at the pinned path)."""
        entry = self.entry(name)
        path = self._resolve(entry["path"])
        report: dict = {
            "name": name,
            "path": entry["path"],
            "expected_sha256": entry["sha256"],
            "expected_size_bytes": entry["size_bytes"],
            "actual_sha256": None,
            "actual_size_bytes": None,
        }
        if not path.is_file():
            report["status"] = "missing"
            return report
        actual_sha, actual_size = hash_file(path)
        report["actual_sha256"] = actual_sha
        report["actual_size_bytes"] = actual_size
        report["status"] = (
            "verified"
            if actual_sha == entry["sha256"] and actual_size == entry["size_bytes"]
            else "corrupt"
        )
        return report

    def verify_all(self) -> list[dict]:
        """Every registered model's verification report, in
        registration order — corrupt and missing models are
        reported, never silently dropped."""
        return [self.verify(entry["name"]) for entry in self._entries]

    def is_present(self, name: str) -> bool:
        """Present means: registered *and* hash-verified now."""
        try:
            return self.verify(name)["status"] == "verified"
        except ModelStoreError:
            return False

    def present_models(self) -> list[str]:
        """The names present right now — each one re-hashed for
        this call. A model whose bytes changed since the last
        call falls off this list the moment it is asked about."""
        return [
            report["name"]
            for report in self.verify_all()
            if report["status"] == "verified"
        ]

    def model_names_for_census(self) -> list[str]:
        """``present_models()`` under the name the census
        collector's models callable wants (Y-022's seam): a
        census lists only hash-verified models."""
        return self.present_models()

    # -- registry shape -------------------------------------------------

    def model_entries(self) -> list[dict]:
        """Validated Y-008 registry entries for the models that
        are present (hash-verified) right now. A corrupt or
        missing model gets no entry — the registry never dresses
        unverified bytes as a runnable model. ``runnable_on``
        stays empty: which device classes a quant fits is
        Y-038's enforcement, not the store's claim."""
        present = set(self.present_models())
        entries: list[dict] = []
        for entry in self._entries:
            if entry["name"] not in present:
                continue
            record = {
                "name": entry["name"],
                "family": entry["family"],
                "quants": [
                    {
                        "name": entry["quant"],
                        "format": entry["format"],
                        # Full precision: rounding could turn a
                        # small-but-real file into 0.0, which the
                        # registry schema rightly refuses.
                        "size_gb": entry["size_bytes"] / (1024**3),
                    }
                ],
                "capabilities": list(entry["capabilities"]),
                "runnable_on": [],
            }
            entries.append(
                _validate(_MODEL_ENTRY_VALIDATOR, record, "model entry")
            )
        return entries
