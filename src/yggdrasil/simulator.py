"""Loopback multi-node simulator — Y-014.

The road's wind tunnel: many fake nodes on one machine, so every
later part can be tested before it touches hardware. A
``Simulator`` holds ``SimulatedNode``s in one process and moves
work between them over an in-process loopback — there are no
sockets, no threads, and no network code paths in this module at
all. ``transport`` says so in data: ``"loopback-simulated"``.

What a simulated node is: a validated NodeIdentity (Y-006) plus a
validated CapabilityCensus (Y-006/Y-022's shape, with SIMULATED
fixture values) plus a task handler. The default handler is a
plain echo — detailed fake engines (latency-shaped, failure-
shaped) are Y-015, and handlers are injectable precisely so that
slice can plug them in without changing the tunnel.

What the simulator does:

- registry: nodes join, stop, and restart; every other node can
  discover the present ones (the shape Part III's fabric will
  carry for real);
- health: every node answers a validated HealthReport (Y-006);
  a stopped node reports ``gone``;
- tasks: a validated TaskEnvelope (Y-007) is delivered to one
  capable node — an explicitly named one, or the first capable
  present node in node_id order (a deterministic stand-in, NOT
  Verðandi routing, which is Part IV) — producing validated
  StreamEvents and exactly one terminal ResultEnvelope with
  provenance, on a virtual clock the test can advance by hand.

Honesty: everything here is SIMULATED. Timings come from the
virtual clock and each node's declared simulated latency, never
from a wall clock or a real engine; results carry
``"simulated": True``; nothing in this module has run on
hardware, and a green simulator run proves shapes and logic,
never device behaviour.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.observability import make_event

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

TRANSPORT = "loopback-simulated"


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


_IDENTITY_VALIDATOR = _validator("node_identity.schema.json")
_CENSUS_VALIDATOR = _validator("capability_census.schema.json")
_HEALTH_VALIDATOR = _validator("health_report.schema.json")
_TASK_VALIDATOR = _validator("task_envelope.schema.json")
_RESULT_VALIDATOR = _validator("result_envelope.schema.json")
_STREAM_VALIDATOR = _validator("stream_event.schema.json")
_ERROR_CODES = set(_load_schema("error_codes.schema.json")["$defs"]["error_code"]["enum"])

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


class SimulatorError(Exception):
    """Any simulator failure. ``error_code`` names it in the
    Y-007 taxonomy when the failure is a task-routing failure."""

    def __init__(self, message: str, *, error_code: str | None = None):
        super().__init__(message)
        self.error_code = error_code


class SimulatedTaskFailure(Exception):
    """A handler raises this to shape a failed task: the node ran
    the task and the work failed with a named Y-007 error code.
    (Failure-shaped *engines* as reusable fixtures are Y-015;
    this is only the tunnel's failure channel.)"""

    def __init__(self, error_code: str, detail: str = ""):
        if error_code not in _ERROR_CODES:
            raise SimulatorError(f"unknown error_code: {error_code}")
        super().__init__(detail or error_code)
        self.error_code = error_code
        self.detail = detail or error_code


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise SimulatorError(f"{kind} fails its schema: {errors[0].message}")
    return record


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class VirtualClock:
    """Simulated time, advanced only by hand or by task latency.

    Starts at a fixed epoch so a workload run twice produces the
    same timestamps — determinism is the wind tunnel's point.
    """

    def __init__(self):
        self._elapsed_ms = 0.0

    @property
    def elapsed_ms(self) -> float:
        return self._elapsed_ms

    def advance(self, ms: float) -> None:
        if ms < 0:
            raise SimulatorError("simulated time cannot run backwards")
        self._elapsed_ms += ms

    def now(self) -> datetime:
        from datetime import timedelta

        return _EPOCH + timedelta(milliseconds=self._elapsed_ms)


def make_fake_identity(
    node_id: str,
    *,
    device_class: str = "vm",
    roles: list[str] | None = None,
) -> dict:
    """A validated SIMULATED NodeIdentity for one fake node.

    The public key is fixture data derived from the node_id —
    it proves the contract's shape and is NOT a cryptographic
    identity (real keypairs and certificates are Y-011).
    """
    identity: dict = {
        "node_id": node_id,
        "public_key": "sim-" + hashlib.sha256(node_id.encode()).hexdigest(),
        "device_class": device_class,
        "agent_version": "0.0.1-simulated",
    }
    if roles:
        identity["roles"] = sorted(set(roles))
    return _validate(_IDENTITY_VALIDATOR, identity, "node identity")


def make_fake_census(
    node_id: str,
    *,
    models: tuple[str, ...] = (),
    capabilities: tuple[str, ...] = ("generate",),
    engine: str = "ollama",
    latency_source_ms: float = 5.0,
    clock: VirtualClock | None = None,
) -> dict:
    """A validated SIMULATED CapabilityCensus for one fake node.

    All values are fixture data, labelled by the fixed past
    ``measured_at`` of the virtual epoch — nothing here was
    measured on any device (real censuses are Y-022).
    ``latency_source_ms`` is accepted for fixture symmetry with
    the node's simulated latency; the census shape carries no
    latency field, so it is not stored in the census itself.
    """
    caps = {name: name in capabilities for name in ("generate", "embed", "vision", "stream")}
    caps["provenance_grade"] = "unproven"
    census = {
        "node_id": node_id,
        "measured_at": _fmt((clock or VirtualClock()).now()),
        "cpu": {"arch": "simulated", "cores": 4},
        "ram_gb": 8.0,
        "accelerators": [{"kind": "none"}],
        "engines": [{"engine": engine, "reachable": True, "capabilities": caps}],
        "models_present": list(models),
    }
    return _validate(_CENSUS_VALIDATOR, census, "capability census")


def _echo_handler(task: dict, node: "SimulatedNode") -> dict:
    """The tunnel's default handler: a plain SIMULATED echo.

    The detailed fake engines (Y-015, ``yggdrasil.fake_engines``)
    plug in as handlers in this one's place; this exists so a
    task can travel end to end with no engine named at all."""
    prompt = task["payload"].get("prompt", "")
    return {"text": f"SIMULATED echo: {prompt}", "simulated": True}


class SimulatedNode:
    """One fake node: identity + census + a task handler."""

    def __init__(
        self,
        identity: dict,
        census: dict,
        *,
        handler=None,
        latency_ms: float = 5.0,
    ):
        _validate(_IDENTITY_VALIDATOR, identity, "node identity")
        _validate(_CENSUS_VALIDATOR, census, "capability census")
        if census["node_id"] != identity["node_id"]:
            raise SimulatorError(
                f"census is for {census['node_id']}, identity is {identity['node_id']}"
            )
        if latency_ms < 0:
            raise SimulatorError("simulated latency cannot be negative")
        self.identity = identity
        self.census = census
        self.node_id = identity["node_id"]
        self.handler = handler or _echo_handler
        self.latency_ms = float(latency_ms)
        self.present = True
        self.tasks_run = 0

    def capabilities(self) -> frozenset[str]:
        """Capabilities any reachable engine on this node offers."""
        found: set[str] = set()
        for engine in self.census.get("engines", []):
            if not engine.get("reachable"):
                continue
            caps = engine.get("capabilities", {})
            for name in ("generate", "embed", "vision", "stream"):
                if caps.get(name):
                    found.add(name)
        return frozenset(found)

    def engine_name(self) -> str:
        for engine in self.census.get("engines", []):
            if engine.get("reachable"):
                return engine["engine"]
        return "none"

    def refusal_for(self, task: dict) -> str | None:
        """An error code when this node cannot run the task,
        else None. Checks are exact matches against census truth:
        the required model must be present by its full census
        name, and every required capability must be offered."""
        requirements = task.get("requirements") or {}
        model = requirements.get("model")
        if model is not None and model not in self.census.get("models_present", []):
            return "model_not_present"
        needed = set(requirements.get("capabilities") or [])
        if not needed <= self.capabilities():
            return "no_capable_node"
        return None

    def health(self, clock: VirtualClock) -> dict:
        report = {
            "node_id": self.node_id,
            "at": _fmt(clock.now()),
            "liveness": "present" if self.present else "gone",
            "queue_depth": 0,
            "models_loaded": list(self.census.get("models_present", []))
            if self.present
            else [],
        }
        return _validate(_HEALTH_VALIDATOR, report, "health report")


class Simulator:
    """Many fake nodes on one machine, on a loopback that is
    data in this process — no sockets, no threads, no network."""

    transport = TRANSPORT
    is_simulated = True
    network_access = False

    def __init__(self):
        self.clock = VirtualClock()
        self._nodes: dict[str, SimulatedNode] = {}
        self.events: list[dict] = []
        self.results: list[dict] = []

    # -- registry ---------------------------------------------------

    def add_node(self, node: SimulatedNode) -> SimulatedNode:
        if node.node_id in self._nodes:
            raise SimulatorError(f"duplicate node: {node.node_id}")
        self._nodes[node.node_id] = node
        self._emit("node.joined", node.node_id, {"simulated": True})
        return node

    def add_fake_node(
        self,
        node_id: str,
        *,
        device_class: str = "vm",
        roles: list[str] | None = None,
        models: tuple[str, ...] = (),
        capabilities: tuple[str, ...] = ("generate",),
        handler=None,
        latency_ms: float = 5.0,
    ) -> SimulatedNode:
        identity = make_fake_identity(node_id, device_class=device_class, roles=roles)
        census = make_fake_census(
            node_id, models=models, capabilities=capabilities, clock=self.clock
        )
        return self.add_node(
            SimulatedNode(identity, census, handler=handler, latency_ms=latency_ms)
        )

    def spawn(self, count: int, *, prefix: str = "sim-node-", **kwargs) -> list[SimulatedNode]:
        """Many fake nodes at once — the tunnel's whole purpose."""
        if count < 1:
            raise SimulatorError("spawn at least one node")
        return [self.add_fake_node(f"{prefix}{i:02d}", **kwargs) for i in range(count)]

    def node(self, node_id: str) -> SimulatedNode:
        try:
            return self._nodes[node_id]
        except KeyError:
            raise SimulatorError(f"unknown node: {node_id}") from None

    def node_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._nodes))

    def stop_node(self, node_id: str) -> None:
        self.node(node_id).present = False
        self._emit("node.stopped", node_id, {"simulated": True})

    def start_node(self, node_id: str) -> None:
        self.node(node_id).present = True
        self._emit("node.joined", node_id, {"simulated": True})

    def peers(self, node_id: str) -> tuple[str, ...]:
        """Discovery, simulated: the present nodes a node can see
        (every present node except itself). What carries this view
        for real is Part III's fabric; the shape is the contract."""
        self.node(node_id)
        return tuple(
            sorted(
                other
                for other, node in self._nodes.items()
                if other != node_id and node.present
            )
        )

    def registry_snapshot(self) -> dict:
        """The registry truth at this simulated moment: every
        node's identity, census, and health, as validated data."""
        return {
            node_id: {
                "identity": node.identity,
                "census": node.census,
                "health": node.health(self.clock),
            }
            for node_id, node in sorted(self._nodes.items())
        }

    def health(self, node_id: str) -> dict:
        return self.node(node_id).health(self.clock)

    # -- tasks --------------------------------------------------------

    def _emit(self, type: str, node_id: str, payload: dict) -> dict:
        event = make_event(
            type=type,
            node_id=node_id,
            component="simulator",
            payload=payload,
            at=self.clock.now(),
        )
        self.events.append(event)
        return event

    def _stream(self, task_id: str, seq: int, event: str) -> dict:
        record = {"task_id": task_id, "seq": seq, "event": event}
        return _validate(_STREAM_VALIDATOR, record, "stream event")

    def submit(self, task: dict, *, to_node: str | None = None) -> tuple[dict, list[dict]]:
        """Deliver one task; return (ResultEnvelope, StreamEvents).

        With ``to_node`` the named node answers or refuses; a
        stopped named node is unreachable. Without it, the first
        capable present node in node_id order takes the task —
        a deterministic stand-in, NOT routing (Part IV) — and no
        capable node raises SimulatorError naming no_capable_node,
        because a ResultEnvelope's mandatory provenance cannot
        honestly name a node that never saw the task.
        """
        _validate(_TASK_VALIDATOR, task, "task envelope")
        if to_node is not None:
            node = self.node(to_node)
            if not node.present:
                raise SimulatorError(
                    f"node {to_node} is gone", error_code="partition_unreachable"
                )
        else:
            node = None
            for candidate_id in sorted(self._nodes):
                candidate = self._nodes[candidate_id]
                if candidate.present and candidate.refusal_for(task) is None:
                    node = candidate
                    break
            if node is None:
                raise SimulatorError(
                    "no capable present node for this task",
                    error_code="no_capable_node",
                )

        stream = [self._stream(task["id"], 0, "accepted")]
        requirements = task.get("requirements") or {}
        model = requirements.get("model") or (
            node.census.get("models_present", ["simulated-model"])[0]
            if node.census.get("models_present")
            else "simulated-model"
        )
        provenance = {"node_id": node.node_id, "engine": node.engine_name(), "model": model}

        refusal = node.refusal_for(task) if to_node is not None else None
        if refusal is not None:
            result = {
                "task_id": task["id"],
                "status": "refused",
                "provenance": provenance,
                "timings_ms": {"queue": 0.0, "run": 0.0, "total": 0.0},
                "error": {"code": refusal, "detail": f"SIMULATED refusal by {node.node_id}"},
                "result": {"simulated": True},
            }
            stream.append(self._stream(task["id"], 1, "failed"))
            self.results.append(_validate(_RESULT_VALIDATOR, result, "result envelope"))
            return self.results[-1], stream

        stream.append(self._stream(task["id"], 1, "started"))
        # A handler may shape latency per task (Y-015's fake
        # engines offer latency_ms_for); otherwise the node's
        # declared constant governs. Either way the value is
        # simulated — it advances the virtual clock only.
        latency_for = getattr(node.handler, "latency_ms_for", None)
        run_ms = float(latency_for(task)) if callable(latency_for) else node.latency_ms
        if run_ms < 0:
            raise SimulatorError("simulated latency cannot be negative")
        self.clock.advance(run_ms)
        node.tasks_run += 1
        try:
            outcome = node.handler(task, node)
        except SimulatedTaskFailure as failure:
            result = {
                "task_id": task["id"],
                "status": "failed",
                "provenance": provenance,
                "timings_ms": {"queue": 0.0, "run": run_ms, "total": run_ms},
                "error": {"code": failure.error_code, "detail": failure.detail},
                "result": {"simulated": True},
            }
            stream.append(self._stream(task["id"], 2, "failed"))
            self.results.append(_validate(_RESULT_VALIDATOR, result, "result envelope"))
            self._emit(
                "task.failed", node.node_id, {"simulated": True, "error_code": failure.error_code}
            )
            return self.results[-1], stream

        if not isinstance(outcome, dict):
            raise SimulatorError("a task handler must return a result object")
        outcome = dict(outcome)
        outcome.setdefault("simulated", True)
        result = {
            "task_id": task["id"],
            "status": "completed",
            "provenance": provenance,
            "timings_ms": {"queue": 0.0, "run": run_ms, "total": run_ms},
            "result": outcome,
        }
        stream.append(self._stream(task["id"], 2, "completed"))
        self.results.append(_validate(_RESULT_VALIDATOR, result, "result envelope"))
        self._emit("task.completed", node.node_id, {"simulated": True})
        return self.results[-1], stream

    def run_many(self, tasks: list[dict]) -> list[dict]:
        """Submit a workload in order; return its ResultEnvelopes."""
        return [self.submit(task)[0] for task in tasks]
