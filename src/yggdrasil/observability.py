"""Observability — Y-013.

The contract (``contracts/observability_contract.md``) in code:
every component of the mesh speaks exactly three observable shapes
— a LogRecord for something that happened, a MeterSample for a
measured number, and an EventEnvelope for a lifecycle fact others
may act on. This module validates all three against their schemas
(``contracts/schemas/``), builds records that cannot smuggle
payload content into a log line, keeps the standard meter catalog
as data, and writes records to a local JSONL sink.

The EventEnvelope reuses the Verdandi event shape family (the
Hermes plugin's event fabric): type, source, at, per-source seq,
correlation, payload. The plugin's exact emitted field names are
verified at Y-197 as a mapping against this envelope; this module
does not claim that verification has happened.

Honesty: structured, local, no phone-home. The sink in this slice
is a plain local file (or nothing — validation alone). No
component emits these records yet; emitters arrive with the
components in Part II onward. Nothing here has run on hardware.
"""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

COMPONENTS = (
    "node_agent",
    "fabric",
    "router",
    "memory",
    "verifier",
    "ensemble",
    "registry",
    "identity",
    "operator_cli",
    "simulator",
)

LEVELS = ("debug", "info", "warn", "error")

# Field and label keys that would smuggle payload content or
# credentials into a record. Matched case-insensitively, exactly or
# as a substring for the credential family — a log line carries
# ids, counts, timings, and error names, never the work itself.
_BANNED_EXACT = {
    "prompt",
    "completion",
    "result",
    "result_text",
    "payload_text",
    "document",
    "documents",
}
_BANNED_SUBSTRINGS = ("secret", "password", "private_key", "api_key", "credential")


def _load_schema(name: str) -> dict:
    return json.loads((_SCHEMAS / name).read_text())


def _validator(name: str):
    """A validator for one schema, with every contract schema
    registered so relative $refs resolve — the same registry the
    Y-009 contract harness builds."""
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


_LOG_VALIDATOR = _validator("log_record.schema.json")
_METER_VALIDATOR = _validator("meter_sample.schema.json")
_EVENT_VALIDATOR = _validator("event_envelope.schema.json")
_ERROR_CODES = set(_load_schema("error_codes.schema.json")["$defs"]["error_code"]["enum"])

# The standard meters, as data: name -> (kind, unit, what it means).
# A component may add a meter only by adding a row here, so every
# name a dashboard reads has a known kind and unit (Y-202).
METERS = {
    "tasks.accepted": ("counter", "count", "tasks accepted by a node"),
    "tasks.completed": ("counter", "count", "tasks completed"),
    "tasks.failed": ("counter", "count", "tasks failed (see error_code in logs)"),
    "tasks.refused": ("counter", "count", "tasks refused at the door"),
    "queue.depth": ("gauge", "count", "tasks waiting on this node right now"),
    "model.load_ms": ("histogram_summary", "ms", "measured model load duration"),
    "model.swap_ms": ("histogram_summary", "ms", "measured model swap duration"),
    "inference.tokens": ("counter", "tokens", "tokens generated across engines"),
    "fabric.messages": ("counter", "count", "fabric messages carried"),
    "fabric.bytes": ("counter", "bytes", "fabric bytes carried"),
    "verification.passed": ("counter", "count", "verifications passed"),
    "verification.failed": ("counter", "count", "verifications failed"),
    "node.temperature": ("gauge", "celsius", "hottest measured device temperature"),
}


class ObservabilityError(Exception):
    """Any observability-contract failure."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_keys(record_kind: str, mapping: dict) -> None:
    for key in mapping:
        lowered = str(key).lower()
        if lowered in _BANNED_EXACT or any(b in lowered for b in _BANNED_SUBSTRINGS):
            raise ObservabilityError(
                f"{record_kind} may not carry payload/credential key: {key}"
            )


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise ObservabilityError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_log_record(record: dict) -> dict:
    if isinstance(record.get("fields"), dict):
        _check_keys("log record", record["fields"])
    return _validate(_LOG_VALIDATOR, record, "log record")


def validate_meter_sample(sample: dict) -> dict:
    if isinstance(sample.get("labels"), dict):
        _check_keys("meter sample", sample["labels"])
    return _validate(_METER_VALIDATOR, sample, "meter sample")


def validate_event(record: dict) -> dict:
    if isinstance(record.get("payload"), dict):
        _check_keys("event", record["payload"])
    return _validate(_EVENT_VALIDATOR, record, "event envelope")


def make_log_record(
    *,
    level: str,
    component: str,
    node_id: str,
    event: str,
    message: str,
    fields: dict | None = None,
    error_code: str | None = None,
    task_id: str | None = None,
    correlation_id: str | None = None,
    at: datetime | None = None,
) -> dict:
    """Build and validate one LogRecord. An error-level record
    about a failure must name its Y-007 error_code — pass it."""
    if level not in LEVELS:
        raise ObservabilityError(f"unknown level: {level}")
    if component not in COMPONENTS:
        raise ObservabilityError(f"unknown component: {component}")
    if error_code is not None and error_code not in _ERROR_CODES:
        raise ObservabilityError(f"unknown error_code: {error_code}")
    record: dict = {
        "at": _fmt(at or datetime.now(UTC)),
        "level": level,
        "component": component,
        "node_id": node_id,
        "event": event,
        "message": message,
        "payload_logged": False,
    }
    if fields:
        record["fields"] = fields
    if error_code is not None:
        record["error_code"] = error_code
    if task_id is not None:
        record["task_id"] = task_id
    if correlation_id is not None:
        record["correlation_id"] = correlation_id
    return validate_log_record(record)


def make_event(
    *,
    type: str,
    node_id: str,
    component: str,
    payload: dict,
    seq: int | None = None,
    task_id: str | None = None,
    correlation_id: str | None = None,
    at: datetime | None = None,
) -> dict:
    """Build and validate one EventEnvelope in the Verdandi shape."""
    if component not in COMPONENTS:
        raise ObservabilityError(f"unknown component: {component}")
    record: dict = {
        "event_id": str(uuid.uuid4()),
        "version": "1",
        "type": type,
        "source": {"node_id": node_id, "component": component},
        "at": _fmt(at or datetime.now(UTC)),
        "payload": payload,
    }
    if seq is not None:
        record["seq"] = seq
    if task_id is not None:
        record["task_id"] = task_id
    if correlation_id is not None:
        record["correlation_id"] = correlation_id
    return validate_event(record)


class MeterRegistry:
    """The standard meters for one node, kept honestly.

    Counters only rise; gauges report the present; histogram
    summaries keep the last measured value (a full distribution is
    the dashboard's aggregation job, Y-202 — this registry claims
    no more than it holds). Snapshots are validated MeterSamples.
    """

    def __init__(self, node_id: str, component: str = "node_agent"):
        if component not in COMPONENTS:
            raise ObservabilityError(f"unknown component: {component}")
        self.node_id = node_id
        self.component = component
        self._values: dict[tuple[str, tuple], float] = {}

    def _spec(self, name: str) -> tuple[str, str]:
        try:
            kind, unit, _meaning = METERS[name]
        except KeyError:
            raise ObservabilityError(
                f"unknown meter {name!r}: add it to the METERS catalog as data first"
            ) from None
        return kind, unit

    def _key(self, name: str, labels: dict | None) -> tuple:
        labels = labels or {}
        _check_keys("meter sample", labels)
        return (name, tuple(sorted(labels.items())))

    def increment(self, name: str, amount: float = 1, *, labels: dict | None = None) -> None:
        kind, _unit = self._spec(name)
        if kind != "counter":
            raise ObservabilityError(f"meter {name} is a {kind}, not a counter")
        if amount < 0:
            raise ObservabilityError("counters only rise")
        key = self._key(name, labels)
        self._values[key] = self._values.get(key, 0) + amount

    def set_gauge(self, name: str, value: float, *, labels: dict | None = None) -> None:
        kind, _unit = self._spec(name)
        if kind != "gauge":
            raise ObservabilityError(f"meter {name} is a {kind}, not a gauge")
        self._values[self._key(name, labels)] = value

    def observe(self, name: str, value: float, *, labels: dict | None = None) -> None:
        kind, _unit = self._spec(name)
        if kind != "histogram_summary":
            raise ObservabilityError(f"meter {name} is a {kind}, not a histogram_summary")
        if value < 0:
            raise ObservabilityError("a measured duration cannot be negative")
        self._values[self._key(name, labels)] = value

    def snapshot(self, *, at: datetime | None = None) -> list[dict]:
        moment = _fmt(at or datetime.now(UTC))
        samples = []
        for (name, label_items), value in sorted(self._values.items()):
            kind, unit = self._spec(name)
            sample: dict = {
                "at": moment,
                "node_id": self.node_id,
                "component": self.component,
                "name": name,
                "kind": kind,
                "value": value,
                "unit": unit,
            }
            if label_items:
                sample["labels"] = dict(label_items)
            samples.append(validate_meter_sample(sample))
        return samples


class LocalJsonlSink:
    """Records appended as JSON Lines to one local file.

    This is the whole transport: a file on the node's own disk.
    There is deliberately no network path in this class — no
    endpoint, no collector, no phone-home (the contract's rule).
    """

    transport = "local-file"

    def __init__(self, path: Path):
        self.path = Path(path)

    def write(self, record: dict) -> dict:
        if "level" in record:
            validate_log_record(record)
        elif "value" in record:
            validate_meter_sample(record)
        elif "type" in record:
            validate_event(record)
        else:
            raise ObservabilityError("record is none of the three observability shapes")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        return record
