"""Data-discipline tests — Y-016's acceptance.

The discipline must prove: (a) the manifest itself is versioned,
schema-valid data; (b) every data file on disk is registered, and
every registered file carries the version and owning slice the
manifest expects; (c) every item in every registered collection
validates against its item schema; (d) tampering is caught —
version drift, a wrong owning slice, an item that fails its schema,
and above all an *unregistered* data file dropped into a data
directory; (e) version bumps keep the stem and strictly raise N;
and (f) every contract schema is itself valid and versioned.
The repo's data is paperwork — it proves the discipline's shape,
not a running mesh (no runtime reader exists until Part II).
"""

import json
import shutil
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from yggdrasil.data_discipline import (
    DataError,
    DataManifest,
    parse_version,
    require_version_bump,
    schema_version,
    validate_repo,
    validate_schemas,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "contracts" / "schemas"
MANIFEST_RAW = json.loads((ROOT / "data" / "data_manifest.json").read_text())


def test_schemas_are_valid():
    for name in ("data_manifest.schema.json", "data_file_entry.schema.json"):
        schema = json.loads((SCHEMAS / name).read_text())
        Draft202012Validator.check_schema(schema)
        assert schema_version(schema) == 1


def test_all_contract_schemas_are_valid_and_versioned():
    count = validate_schemas()
    assert count == len(list(SCHEMAS.glob("*.json"))) >= 10


def test_manifest_validates_and_covers_the_repo():
    reports = {r.id: r for r in validate_repo()}
    assert set(reports) == {
        "data_manifest",
        "role_catalog",
        "threat_register",
        "provenance_register",  # added by Y-018, registered in its own slice
        "campaign_file",  # added by Y-019, registered in its own slice
    }
    assert reports["role_catalog"].version == "yggdrasil-role-catalog-v1"
    assert reports["role_catalog"].items_checked == 9  # the nine worlds
    assert reports["threat_register"].items_checked >= 8
    assert all(len(r.sha256) == 64 for r in reports.values())


def test_manifest_registers_itself_first_and_agrees_with_itself():
    entry = MANIFEST_RAW["entries"][0]
    assert entry["id"] == "data_manifest"
    assert entry["path"] == "data/data_manifest.json"
    assert entry["version"] == MANIFEST_RAW["manifest"]
    assert entry["roadmap_slice"] == MANIFEST_RAW["roadmap_slice"] == "Y-016"


def test_manifest_rejects_duplicate_entries():
    raw = json.loads(json.dumps(MANIFEST_RAW))
    raw["entries"] = raw["entries"] + [dict(raw["entries"][1])]
    with pytest.raises(DataError):
        DataManifest(raw, ROOT)


def test_parse_version_and_bumps():
    assert parse_version("yggdrasil-role-catalog-v3") == ("yggdrasil-role-catalog", 3)
    for bad in ("v1", "catalog-1", "catalog-v", "", "Catalog-V1"):
        with pytest.raises(DataError):
            parse_version(bad)
    require_version_bump("yggdrasil-role-catalog-v1", "yggdrasil-role-catalog-v2")
    with pytest.raises(DataError):  # same version reused
        require_version_bump("yggdrasil-role-catalog-v1", "yggdrasil-role-catalog-v1")
    with pytest.raises(DataError):  # stem renamed under the same file
        require_version_bump("yggdrasil-role-catalog-v1", "yggdrasil-hats-v2")


def _mini_repo(tmp_path):
    """A minimal repo-shaped tree holding one tiny registered data
    file, so tamper cases run against copies — never the real tree."""
    root = tmp_path / "repo"
    (root / "contracts" / "schemas").mkdir(parents=True)
    (root / "data").mkdir()
    (root / "profiles").mkdir()
    for name in ("data_manifest.schema.json", "data_file_entry.schema.json"):
        shutil.copy(SCHEMAS / name, root / "contracts" / "schemas" / name)
    # A one-field item schema for the mini registry's items.
    (root / "contracts" / "schemas" / "mini_item.schema.json").write_text(
        json.dumps(
            {
                "$id": "yggdrasil/contracts/v1/mini_item",
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "properties": {"name": {"type": "string", "minLength": 2}},
                "required": ["name"],
            }
        )
    )
    data_file = {
        "registry": "mini-registry-v1",
        "roadmap_slice": "Y-016",
        "items": [{"name": "alpha"}, {"name": "beta"}],
    }
    (root / "data" / "mini_registry.json").write_text(json.dumps(data_file))
    manifest = {
        "manifest": "yggdrasil-data-manifest-v1",
        "roadmap_slice": "Y-016",
        "entries": [
            {
                "id": "data_manifest",
                "kind": "manifest",
                "path": "data/data_manifest.json",
                "version_field": "manifest",
                "version": "yggdrasil-data-manifest-v1",
                "roadmap_slice": "Y-016",
                "schema": "data_manifest.schema.json",
                "items_field": None,
                "item_schema": None,
            },
            {
                "id": "mini_registry",
                "kind": "registry",
                "path": "data/mini_registry.json",
                "version_field": "registry",
                "version": "mini-registry-v1",
                "roadmap_slice": "Y-016",
                "schema": None,
                "items_field": "items",
                "item_schema": "mini_item.schema.json",
            },
        ],
    }
    (root / "data" / "data_manifest.json").write_text(json.dumps(manifest))
    return root


def test_mini_repo_validates(tmp_path):
    reports = DataManifest.load(_mini_repo(tmp_path)).validate_all()
    assert {r.id: r.items_checked for r in reports} == {
        "data_manifest": 0,
        "mini_registry": 2,
    }


def test_unregistered_data_file_is_refused(tmp_path):
    root = _mini_repo(tmp_path)
    (root / "profiles" / "sneaky.json").write_text(json.dumps({"profile": "x-v1"}))
    with pytest.raises(DataError, match="unregistered"):
        DataManifest.load(root).validate_all()


def test_version_drift_is_refused(tmp_path):
    root = _mini_repo(tmp_path)
    path = root / "data" / "mini_registry.json"
    raw = json.loads(path.read_text())
    raw["registry"] = "mini-registry-v2"  # bumped on disk, not in the manifest
    path.write_text(json.dumps(raw))
    with pytest.raises(DataError, match="version mismatch"):
        DataManifest.load(root).validate_all()


def test_wrong_owning_slice_is_refused(tmp_path):
    root = _mini_repo(tmp_path)
    path = root / "data" / "mini_registry.json"
    raw = json.loads(path.read_text())
    raw["roadmap_slice"] = "Y-999"
    path.write_text(json.dumps(raw))
    with pytest.raises(DataError, match="roadmap_slice mismatch"):
        DataManifest.load(root).validate_all()


def test_item_failing_its_schema_is_refused(tmp_path):
    root = _mini_repo(tmp_path)
    path = root / "data" / "mini_registry.json"
    raw = json.loads(path.read_text())
    raw["items"] = [{"name": "ok"}, {"name": 7}]
    path.write_text(json.dumps(raw))
    with pytest.raises(DataError, match="mini_item"):
        DataManifest.load(root).validate_all()


def test_missing_and_malformed_files_are_refused(tmp_path):
    root = _mini_repo(tmp_path)
    (root / "data" / "mini_registry.json").write_text("{not json")
    with pytest.raises(DataError, match="cannot load"):
        DataManifest.load(root).validate_all()
    (root / "data" / "mini_registry.json").unlink()
    with pytest.raises(DataError, match="cannot load"):
        DataManifest.load(root).validate_all()


def test_path_escape_is_refused(tmp_path):
    root = _mini_repo(tmp_path)
    manifest_path = root / "data" / "data_manifest.json"
    raw = json.loads(manifest_path.read_text())
    raw["entries"][1]["path"] = "../outside.json"
    manifest_path.write_text(json.dumps(raw))
    # Refused twice over: the entry schema's path pattern rejects it,
    # and the loader's own escape check stands behind that.
    with pytest.raises(DataError):
        DataManifest.load(root).validate_all()
