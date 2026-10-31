"""Contract tests — Y-009's harness proving Y-006/Y-007/Y-008.

Every schema in contracts/schemas must (a) itself be a valid JSON
Schema, and (b) accept its valid fixture and reject its invalid one.
Fixtures are SIMULATED example data — they prove contract shapes,
not any hardware behaviour.
"""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, validators
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "contracts" / "schemas"

UUID = "3f6f1b2c-9d4e-4c5a-8b7d-2e1a0f9c8b7a"


def load(name):
    return json.loads((SCHEMAS / name).read_text())


def registry():
    resources = []
    for path in sorted(SCHEMAS.glob("*.json")):
        schema = json.loads(path.read_text())
        resource = Resource.from_contents(schema)
        resources.append((path.name, resource))
        resources.append((schema["$id"], resource))
        # Relative $refs resolve against the referring schema's $id directory.
        resources.append((schema["$id"].rsplit("/", 1)[0] + "/" + path.name, resource))
    return Registry().with_resources(resources)


def make(name):
    schema = load(name)
    Draft202012Validator.check_schema(schema)
    return validators.validator_for(schema)(schema, registry=registry())


CASES = {
    "node_identity.schema.json": (
        {
            "node_id": "forge-pi-01",
            "public_key": "x" * 44,
            "device_class": "pi",
            "agent_version": "0.0.1",
            "roles": ["svartalfheim"],
        },
        {"node_id": "BAD ID!", "device_class": "pi"},
    ),
    "capability_census.schema.json": (
        {
            "node_id": "forge-pi-01",
            "measured_at": "2026-10-30T20:00:00-04:00",
            "cpu": {"arch": "aarch64", "cores": 4},
            "ram_gb": 16.0,
            "accelerators": [{"kind": "hailo", "name": "Hailo-10", "proven": False}],
            "engines": [
                {
                    "engine": "aesir",
                    "reachable": True,
                    "capabilities": {"generate": True, "provenance_grade": "partial"},
                }
            ],
            "models_present": ["coder-3b-q8_0"],
        },
        {"node_id": "x", "cpu": {"arch": "aarch64"}},  # missing required fields
    ),
    "health_report.schema.json": (
        {
            "node_id": "forge-pi-01",
            "at": "2026-10-30T20:00:00-04:00",
            "liveness": "present",
            "queue_depth": 0,
            "models_loaded": [],
        },
        {"node_id": "x", "liveness": "undead"},
    ),
    "task_envelope.schema.json": (
        {
            "id": UUID,
            "kind": "chat.generate",
            "payload": {"prompt": "hello"},
            "caller": "yrsa",
            "criticality": "standard",
        },
        {"id": "not-a-uuid", "kind": "Chat Generate", "payload": {}},
    ),
    "result_envelope.schema.json": (
        {
            "task_id": UUID,
            "status": "completed",
            "provenance": {"node_id": "forge-pi-01", "engine": "aesir", "model": "coder-3b"},
            "timings_ms": {"total": 12.5},
            "result": {"text": "hi"},
        },
        {"task_id": UUID, "status": "vanished"},
    ),
    "stream_event.schema.json": (
        {"task_id": UUID, "seq": 0, "event": "accepted"},
        {"task_id": UUID, "seq": -1, "event": "exploded"},
    ),
    "model_entry.schema.json": (
        {
            "name": "coder-3b",
            "family": "coder",
            "quants": [{"name": "Q8_0", "format": "gguf", "size_gb": 3.4}],
            "capabilities": ["generate"],
            "runnable_on": ["pi", "laptop"],
        },
        {"name": "coder-3b", "quants": []},
    ),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_schema_accepts_valid_and_rejects_invalid(name):
    v = make(name)
    good, bad = CASES[name]
    assert list(v.iter_errors(good)) == [], f"{name} should accept its fixture"
    assert list(v.iter_errors(bad)) != [], f"{name} should reject its bad fixture"


def test_all_schemas_are_valid_schemas():
    for path in sorted(SCHEMAS.glob("*.json")):
        Draft202012Validator.check_schema(load(path.name))


def test_error_taxonomy_has_sixteen_named_failures():
    codes = load("error_codes.schema.json")["$defs"]["error_code"]["enum"]
    assert len(codes) == 16 and len(set(codes)) == 16
