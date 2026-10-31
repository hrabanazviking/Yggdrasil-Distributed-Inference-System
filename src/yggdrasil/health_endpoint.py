"""Node health endpoint — Y-030.

The node agent (Y-021) has answered its own ``health()`` since
the skeleton: a Y-006 HealthReport told from lifecycle truth.
This slice grows that answer into the node's *endpoint* — the
one shape a peer, an operator, or (from Part III) a heartbeat
on the fabric asks for:

- **liveness** — the health report's verdict, present while
  started/joined/draining, gone otherwise. A node only ever
  reports *itself* present or gone: ``suspect`` exists in the
  shape because it is a *peer's* ageing verdict on a silent
  node (heartbeats and liveness, Y-054), and a self-report
  claiming it is refused here by name.
- **load** — the numbers a router will one day place by, all
  counted or measured, none estimated: in-flight leases,
  queue depth (the local queue's true depth since Y-031
  when one is wired; 0 before and unwired — the report
  claims no queue early), sandbox active tasks and
  finished executions, and the governor's latest readings
  (thermal / memory fraction / CPU share) when a governor is
  wired. Without a governor those readings are null —
  unknown, never a calm zero invented for the occasion.
- **models** — resident models from the model lifecycle
  (Y-027), and the models *present* on the node: re-hashed
  by the model store (Y-026) at ask time when a store is
  wired, else the already-measured census's list, else null
  (no source has measured — unknown, not empty).
- **governor state** — the governor's full validated verdict
  (Y-028) when one is wired, else null; draining remains a
  lifecycle fact in this shape, not dressed as a governor
  reason the governor never gave.
- **census** — whether a census has been measured at all,
  when, and its timestamp-free fingerprint (Y-022). A
  heartbeat never *forces* a measurement: it reports the
  census the node already holds, so a cheap heartbeat stays
  cheap and a fingerprint change still names a real change.
- **uptime** — seconds since the last successful start,
  measured on the agent's clock while the node is present;
  null before the first start and after a stop.

**The shape Part III's heartbeats carry.** Every heartbeat
is a validated ``node_health`` v1 record with a per-endpoint
monotonic ``seq`` (a receiver can spot a missed or replayed
beat by number) and the honesty fields the tree has carried
since Y-021: ``fabric_connected: false``,
``transport: "local-only"``. That is literal: this endpoint
is a *local handler*, not a network server — ``handle()``
answers ``(method, path)`` pairs in-process and returns
``(status, body)``, the exact seam a Part III transport
binds to later. No socket, HTTP client, or network import
exists in this module, and the Part I gate's banned-import
scan stays green untouched.

Status codes are the handler's local convention, stated
plainly: ``GET /health`` and ``GET /health/heartbeat``
answer 200 while the node is present and 503 while it is
gone (the body is a full heartbeat either way — gone is a
report, not a silence); an unknown path answers 404 and a
non-GET method 405, both with a small error body naming the
refusal. Each heartbeat the endpoint serves is also passed
to an optional ``on_heartbeat`` callback (the node agent
uses it to record a Y-013 ``node.heartbeat`` event in its
own ledger, counts and names only).

No hardware claim: a heartbeat reports what this process
knows about its own node, measured by the slices named
above; it proves nothing about any named machine until a
citizen (Y-033 – Y-035) runs it there.
"""

import json
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

TRANSPORT = "local-only"
HEALTH_PATHS = ("/health", "/health/heartbeat")
_PRESENT_STATES = ("started", "joined", "paused", "draining")
_LIFECYCLE_STATES = (
    "created",
    "started",
    "joined",
    "paused",
    "draining",
    "stopped",
    "retired",
)


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


_HEALTH_VALIDATOR = _validator("node_health.schema.json")
_REPORT_VALIDATOR = _validator("health_report.schema.json")
_GOVERNOR_VALIDATOR = _validator("governor_state.schema.json")


class HealthError(Exception):
    """Any health-endpoint failure: an unshaped snapshot, a
    self-report claiming a peer's verdict, a component record
    that disagrees with the node it claims to describe."""


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise HealthError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_node_health(record: dict) -> dict:
    """A heartbeat record against the node_health v1 schema."""
    return _validate(_HEALTH_VALIDATOR, record, "node health")


def expected_liveness(lifecycle_state: str) -> str:
    """The only liveness a node may report for itself in a
    given lifecycle state."""
    return "present" if lifecycle_state in _PRESENT_STATES else "gone"


def build_node_health(
    *,
    node_id: str,
    seq: int,
    lifecycle_state: str,
    health: dict,
    load: dict,
    models: dict,
    governor_state: dict | None,
    census: dict,
    uptime_seconds: float | None,
) -> dict:
    """Assemble one validated heartbeat from a node's own
    component records, refusing any record that disagrees
    with the node or the lifecycle it claims to describe.
    The heartbeat's timestamp is the health report's own —
    one moment, told once."""
    if not isinstance(node_id, str) or not node_id:
        raise HealthError("a heartbeat needs a node_id")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise HealthError("a heartbeat seq must be a non-negative int")
    if lifecycle_state not in _LIFECYCLE_STATES:
        raise HealthError(f"unknown lifecycle_state: {lifecycle_state!r}")
    if not isinstance(health, dict):
        raise HealthError("a heartbeat needs its health report dict")
    _validate(_REPORT_VALIDATOR, health, "health report")
    if health["node_id"] != node_id:
        raise HealthError(
            f"health report names node {health['node_id']!r}, "
            f"heartbeat names {node_id!r}"
        )
    liveness = health["liveness"]
    if liveness == "suspect":
        raise HealthError(
            "a node never reports itself suspect — suspect is a peer's "
            "ageing verdict on a silent node (Y-054)"
        )
    if liveness != expected_liveness(lifecycle_state):
        raise HealthError(
            f"health report liveness {liveness!r} disagrees with "
            f"lifecycle_state {lifecycle_state!r}"
        )
    if health["queue_depth"] != (load or {}).get("queue_depth"):
        raise HealthError(
            "load queue_depth disagrees with the health report's queue_depth"
        )
    if list(models.get("loaded", [])) != list(health["models_loaded"]):
        raise HealthError(
            "models loaded disagrees with the health report's models_loaded"
        )
    if governor_state is not None:
        _validate(_GOVERNOR_VALIDATOR, governor_state, "governor state")
        if governor_state["node_id"] != node_id:
            raise HealthError(
                f"governor state names node {governor_state['node_id']!r}, "
                f"heartbeat names {node_id!r}"
            )
    if uptime_seconds is not None:
        if (
            not isinstance(uptime_seconds, (int, float))
            or isinstance(uptime_seconds, bool)
            or uptime_seconds < 0
        ):
            raise HealthError("uptime_seconds must be a non-negative number or None")
        uptime_seconds = round(float(uptime_seconds), 3)
    record = {
        "version": 1,
        "node_id": node_id,
        "at": health["at"],
        "seq": seq,
        "liveness": liveness,
        "lifecycle_state": lifecycle_state,
        "health": health,
        "load": load,
        "models": models,
        "governor_state": governor_state,
        "census": census,
        "uptime_seconds": uptime_seconds,
        "fabric_connected": False,
        "transport": TRANSPORT,
    }
    return validate_node_health(record)


class NodeHealthEndpoint:
    """One node's health endpoint: a snapshot in, heartbeats out.

    ``snapshot`` is a callable returning the node's present
    component records as a dict with keys ``lifecycle_state``,
    ``health``, ``load``, ``models``, ``governor_state``,
    ``census``, and ``uptime_seconds`` — the seam the node
    agent fills from its wired components, and the seam tests
    fill with fixtures. The endpoint owns only the sequence:
    each heartbeat it serves takes the next number, so a
    receiver (Part III) can spot a gap.
    """

    def __init__(self, node_id: str, snapshot, *, on_heartbeat=None):
        if not isinstance(node_id, str) or not node_id:
            raise HealthError("a health endpoint needs a node_id")
        if not callable(snapshot):
            raise HealthError("a health endpoint needs a snapshot callable")
        self.node_id = node_id
        self._snapshot = snapshot
        self._on_heartbeat = on_heartbeat
        self._seq = 0
        self._beats = 0
        self._last: dict | None = None

    @property
    def beats(self) -> int:
        """How many heartbeats this endpoint has served."""
        return self._beats

    @property
    def last(self) -> dict | None:
        """The last heartbeat served, or None before the first."""
        return self._last

    def heartbeat(self) -> dict:
        """Serve one heartbeat: stamp the next sequence number
        on a fresh snapshot of the node's own records."""
        try:
            snap = self._snapshot()
        except HealthError:
            raise
        except Exception as exc:
            raise HealthError(f"health snapshot failed: {exc}") from exc
        if not isinstance(snap, dict):
            raise HealthError("a health snapshot is a dict")
        required = {
            "lifecycle_state",
            "health",
            "load",
            "models",
            "governor_state",
            "census",
            "uptime_seconds",
        }
        missing = sorted(required - set(snap))
        if missing:
            raise HealthError(f"health snapshot is missing: {', '.join(missing)}")
        beat = build_node_health(
            node_id=self.node_id,
            seq=self._seq,
            lifecycle_state=snap["lifecycle_state"],
            health=snap["health"],
            load=snap["load"],
            models=snap["models"],
            governor_state=snap["governor_state"],
            census=snap["census"],
            uptime_seconds=snap["uptime_seconds"],
        )
        self._seq += 1
        self._beats += 1
        self._last = beat
        if self._on_heartbeat is not None:
            self._on_heartbeat(beat)
        return beat

    def handle(self, method: str, path: str) -> tuple[int, dict]:
        """Answer one local request as ``(status, body)``.

        No socket is opened — this is the handler a transport
        (Part III) or an operator tool calls in-process."""
        if path not in HEALTH_PATHS:
            return 404, {
                "error": "not_found",
                "node_id": self.node_id,
                "path": path,
                "known_paths": list(HEALTH_PATHS),
            }
        if not isinstance(method, str) or method.upper() != "GET":
            return 405, {
                "error": "method_not_allowed",
                "node_id": self.node_id,
                "path": path,
                "method": method,
            }
        beat = self.heartbeat()
        return (200 if beat["liveness"] == "present" else 503), beat


__all__ = [
    "HEALTH_PATHS",
    "TRANSPORT",
    "HealthError",
    "NodeHealthEndpoint",
    "build_node_health",
    "expected_liveness",
    "validate_node_health",
]
