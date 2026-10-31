"""Observability tests — Y-013's acceptance.

The observability contract must (a) validate all three shapes as
data, (b) refuse payload content and credentials in any record,
(c) keep meters honest (catalogued names only, counters only
rise, measured durations never negative), (d) speak the Verdandi
event envelope, and (e) stay local — the sink is a file, and its
lines round-trip as valid records. Records here are SIMULATED
paperwork; they prove the contract's shape, not a running mesh.
"""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, validators
from referencing import Registry, Resource

from yggdrasil.observability import (
    COMPONENTS,
    METERS,
    LocalJsonlSink,
    MeterRegistry,
    ObservabilityError,
    make_event,
    make_log_record,
    validate_event,
    validate_log_record,
    validate_meter_sample,
)

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
        resources.append((schema["$id"].rsplit("/", 1)[0] + "/" + path.name, resource))
    return Registry().with_resources(resources)


def make(name):
    schema = load(name)
    Draft202012Validator.check_schema(schema)
    return validators.validator_for(schema)(schema, registry=registry())


def test_schemas_are_valid():
    for name in (
        "log_record.schema.json",
        "meter_sample.schema.json",
        "event_envelope.schema.json",
    ):
        Draft202012Validator.check_schema(load(name))


def test_log_record_accepts_and_rejects():
    v = make("log_record.schema.json")
    good = {
        "at": "2026-10-30T21:55:00Z",
        "level": "info",
        "component": "router",
        "node_id": "forge-pi-01",
        "event": "task.completed",
        "message": "task completed",
        "payload_logged": False,
        "task_id": UUID,
        "fields": {"duration_ms": 12.5, "engine": "aesir"},
    }
    assert list(v.iter_errors(good)) == []
    # payload_logged is a constant, not a promise.
    assert list(v.iter_errors(dict(good, payload_logged=True))) != []
    # The component vocabulary is closed.
    assert list(v.iter_errors(dict(good, component="telemetry_cloud"))) != []
    # Fields are scalars only.
    nested = dict(good, fields={"doc": {"text": "x"}})
    assert list(v.iter_errors(nested)) != []


def test_make_log_record_round_trips_and_names_errors():
    record = make_log_record(
        level="error",
        component="node_agent",
        node_id="forge-pi-01",
        event="task.failed",
        message="engine unreachable",
        error_code="engine_unreachable",
        task_id=UUID,
    )
    assert validate_log_record(record) == record
    assert record["payload_logged"] is False
    with pytest.raises(ObservabilityError):
        make_log_record(
            level="error",
            component="node_agent",
            node_id="forge-pi-01",
            event="task.failed",
            message="x",
            error_code="something_broke",
        )
    with pytest.raises(ObservabilityError):
        make_log_record(
            level="shouty",
            component="node_agent",
            node_id="forge-pi-01",
            event="task.failed",
            message="x",
        )


@pytest.mark.parametrize(
    "banned", ["prompt", "result_text", "api_key", "node_secret", "db_password"]
)
def test_records_refuse_payload_and_credential_keys(banned):
    with pytest.raises(ObservabilityError):
        make_log_record(
            level="info",
            component="node_agent",
            node_id="forge-pi-01",
            event="task.completed",
            message="done",
            fields={banned: "should never be logged"},
        )
    with pytest.raises(ObservabilityError):
        make_event(
            type="task.completed",
            node_id="forge-pi-01",
            component="router",
            payload={banned: "should never ride an event"},
        )


def test_event_envelope_shape_and_validation():
    event = make_event(
        type="node.joined",
        node_id="forge-pi-01",
        component="fabric",
        payload={"device_class": "pi"},
        seq=0,
        correlation_id="boot-1",
    )
    assert validate_event(event) == event
    assert event["version"] == "1"
    assert event["source"] == {"node_id": "forge-pi-01", "component": "fabric"}
    v = make("event_envelope.schema.json")
    assert list(v.iter_errors(dict(event, version="2"))) != []
    assert list(v.iter_errors(dict(event, seq=-1))) != []


def test_meter_catalog_is_data_with_kinds_and_units():
    for name, (kind, unit, _meaning) in METERS.items():
        assert kind in ("counter", "gauge", "histogram_summary")
        sample = {
            "at": "2026-10-30T21:55:00Z",
            "node_id": "forge-pi-01",
            "component": "node_agent",
            "name": name,
            "kind": kind,
            "value": 1,
            "unit": unit,
        }
        assert validate_meter_sample(sample) == sample


def test_meter_registry_rules():
    meters = MeterRegistry("forge-pi-01")
    meters.increment("tasks.completed")
    meters.increment("tasks.completed", 2)
    meters.set_gauge("queue.depth", 3)
    meters.observe("model.load_ms", 420.0)
    snapshot = {s["name"]: s for s in meters.snapshot()}
    assert snapshot["tasks.completed"]["value"] == 3
    assert snapshot["queue.depth"]["value"] == 3
    assert snapshot["model.load_ms"]["unit"] == "ms"
    with pytest.raises(ObservabilityError):
        meters.increment("tasks.completed", -1)  # counters only rise
    with pytest.raises(ObservabilityError):
        meters.increment("queue.depth")  # a gauge is not a counter
    with pytest.raises(ObservabilityError):
        meters.observe("model.load_ms", -5)  # durations are measured, never negative
    with pytest.raises(ObservabilityError):
        meters.increment("made.up.meter")  # not in the catalog as data


def test_components_vocabulary_matches_schema():
    schema_components = set(load("log_record.schema.json")["properties"]["component"]["enum"])
    assert schema_components == set(COMPONENTS)


def test_sink_is_a_local_file_and_round_trips(tmp_path):
    sink = LocalJsonlSink(tmp_path / "state" / "observability.jsonl")
    assert sink.transport == "local-file"
    record = make_log_record(
        level="info",
        component="simulator",
        node_id="forge-pi-01",
        event="node.simulated",
        message="SIMULATED node joined",
    )
    event = make_event(
        type="node.joined",
        node_id="forge-pi-01",
        component="simulator",
        payload={"simulated": True},
    )
    sink.write(record)
    sink.write(event)
    lines = sink.path.read_text().splitlines()
    assert len(lines) == 2
    assert validate_log_record(json.loads(lines[0])) == record
    assert validate_event(json.loads(lines[1])) == event
    with pytest.raises(ObservabilityError):
        sink.write({"what": "is this"})
