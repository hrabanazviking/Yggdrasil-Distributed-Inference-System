"""Node agent skeleton — Y-021.

The trunk's first ring: one agent process per device, the thing
the systemd unit at ``deploy/yggdrasil-node.service`` has pointed
at since Y-009 (``python3 -m yggdrasil.node_agent``). This slice
builds the skeleton only — identity, config, and the lifecycle
every later Part II slice hangs its machinery on:

- **identity (Y-011):** the agent *is* its key. ``start()`` loads
  the node's keypair and certificate from the key store, verifies
  the certificate against the mesh-root certificate (chain,
  fingerprint, signature, validity window — Y-011's checks, run
  again here at every start), and refuses to start when the
  stored key does not match the certificate, when the certificate
  names a different node or device class than the config, or when
  anything is missing. The agent never mints its own trust:
  provisioning (mesh root + issued certificate, stored via
  Y-011's ``LocalKeyStore``) is the operator's act, before the
  service ever starts.
- **config:** one operator-written JSON file shaped by
  ``contracts/schemas/node_config.schema.json`` — node_id, device
  class, key store directory, optional display name and log file.
  Nothing else is configurable yet because nothing else exists
  yet; later slices add their knobs to this schema, versioned.
- **lifecycle:** ``created → started → joined → draining →
  stopped``, with restart from ``stopped`` allowed (identity is
  re-verified on every start). Illegal moves are refused by name.
  A joined node may hold task *leases* (``begin_task`` /
  ``end_task``) — the lifecycle gate only: a draining node takes
  no new leases, and ``stop()`` refuses while leases are in
  flight, so a node sheds work before it leaves. Execution
  itself (bounded, cancellable, streamed) is Y-029; the queue
  behind the leases is Y-031.
- **health:** ``health()`` answers the Y-006 HealthReport shape
  from lifecycle truth — present while started/joined/draining,
  gone otherwise, governor shedding named while draining. The
  endpoint Part III's heartbeats will carry is Y-030; models
  loaded stays empty until engines land (Y-023 onward) and queue
  depth stays 0 until the local queue exists (Y-031) — the
  report claims neither early.
- **census (Y-022, grown onto the skeleton):** ``census()``
  answers the node's measured CapabilityCensus through
  ``yggdrasil.census`` (probe-measured, never assumed), and
  ``refresh_census()`` re-measures and records a
  ``node.census_changed`` event naming the fields that changed.
  The skeleton itself measures nothing until asked. Engine
  adapters (Y-023 onward) feed the collector through the
  constructor's ``census_engines`` / ``census_models`` seams —
  an adapter's own discovery, in the census exactly as the
  adapter reported it.
- **observability:** every lifecycle move is recorded as a
  Y-013 EventEnvelope from the ``node_agent`` component (the
  first real emitter in the tree), and written to the config's
  local JSONL log file when one is named — local file only, no
  network path in this module.

Honesty: ``join()`` in this slice is a *local* act. There is no
fabric yet (Part III), so joining records the node's intent and
readiness in its own ledger and says so in data —
``fabric_connected`` is False in every status and event this
module produces, and no peer has heard of this node. A green
skeleton run proves identity, config, and lifecycle logic on
this machine; it proves nothing about any mesh or any hardware
citizen (the real citizens are Y-033 – Y-035).
"""

import argparse
import base64
import json
import signal
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil import __version__
from yggdrasil.census import CensusCollector
from yggdrasil.identity import (
    ROOT_NODE_ID,
    IdentityError,
    LocalKeyStore,
    certificate_identity,
    describe_custody,
    fingerprint,
    verify_certificate,
)
from yggdrasil.observability import LocalJsonlSink, make_event

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

TRANSPORT = "local-only"  # the fabric that would carry a join is Part III

CREATED = "created"
STARTED = "started"
JOINED = "joined"
DRAINING = "draining"
STOPPED = "stopped"
LIFECYCLE_STATES = (CREATED, STARTED, JOINED, DRAINING, STOPPED)
_PRESENT_STATES = (STARTED, JOINED, DRAINING)


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


_CONFIG_VALIDATOR = _validator("node_config.schema.json")
_IDENTITY_VALIDATOR = _validator("node_identity.schema.json")
_HEALTH_VALIDATOR = _validator("health_report.schema.json")


class NodeAgentError(Exception):
    """Any config, identity-at-start, or lifecycle failure."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise NodeAgentError(f"{kind} fails its schema: {errors[0].message}")
    return record


@dataclass(frozen=True)
class NodeConfig:
    """The agent's operator-written config (schema v1).

    Paths inside the config are read relative to the config
    file's directory when loaded from a file, and relative to
    the working directory when built from a dict directly.
    """

    node_id: str
    device_class: str
    key_store_dir: Path
    display_name: str | None = None
    log_file: Path | None = None
    version: int = 1

    @classmethod
    def from_dict(cls, data: dict, *, base_dir: Path | None = None) -> "NodeConfig":
        _validate(_CONFIG_VALIDATOR, data, "node config")
        base = Path(base_dir) if base_dir is not None else Path.cwd()

        def resolve(text: str) -> Path:
            path = Path(text)
            return path if path.is_absolute() else base / path

        return cls(
            node_id=data["node_id"],
            device_class=data["device_class"],
            key_store_dir=resolve(data["key_store_dir"]),
            display_name=data.get("display_name"),
            log_file=resolve(data["log_file"]) if data.get("log_file") else None,
            version=data["version"],
        )

    @classmethod
    def load(cls, path: Path) -> "NodeConfig":
        path = Path(path)
        try:
            data = json.loads(path.read_text())
        except OSError as exc:
            raise NodeAgentError(f"cannot read node config {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise NodeAgentError(f"node config {path} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise NodeAgentError(f"node config {path} is not a JSON object")
        return cls.from_dict(data, base_dir=path.resolve().parent)

    def to_dict(self) -> dict:
        data: dict = {
            "version": self.version,
            "node_id": self.node_id,
            "device_class": self.device_class,
            "key_store_dir": str(self.key_store_dir),
        }
        if self.display_name is not None:
            data["display_name"] = self.display_name
        if self.log_file is not None:
            data["log_file"] = str(self.log_file)
        return _validate(_CONFIG_VALIDATOR, data, "node config")


class NodeAgent:
    """One node's agent: verified identity + lifecycle ledger.

    The clock is injectable (``now``) so tests can pin the
    certificate window and every timestamp the agent emits.
    """

    transport = TRANSPORT
    is_simulated = False
    fabric_connected = False

    def __init__(
        self,
        config: NodeConfig,
        *,
        key_store: LocalKeyStore | None = None,
        now=None,
        census_probes=None,
        census_engines=None,
        census_models=None,
    ):
        self.config = config
        self._store = key_store or LocalKeyStore(config.key_store_dir)
        self._now = now or (lambda: datetime.now(UTC))
        # The capability census (Y-022): measured by probes the
        # caller may inject (tests, odd hardware); the collector
        # is built lazily so constructing an agent never measures
        # a machine nobody asked about yet. Engine adapters
        # (Y-023 onward) feed their live truth through
        # census_engines / census_models — lists or callables,
        # exactly as the collector accepts (Y-022).
        self._census_probes = census_probes
        self._census_engines = census_engines
        self._census_models = census_models
        self._census_collector: CensusCollector | None = None
        self._state = CREATED
        self._identity: dict | None = None
        self._fingerprint: str | None = None
        self._in_flight: set[str] = set()
        self._events: list[dict] = []
        self._seq = 0
        self._started_at: datetime | None = None
        self._joined_at: datetime | None = None
        self._sink = LocalJsonlSink(config.log_file) if config.log_file else None

    # -- facts --------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def node_id(self) -> str:
        return self.config.node_id

    @property
    def identity(self) -> dict | None:
        """The verified NodeIdentity (Y-006 shape), or None
        before the first successful start."""
        return self._identity

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    @property
    def in_flight(self) -> tuple[str, ...]:
        return tuple(sorted(self._in_flight))

    def custody(self) -> dict:
        """Where this node's keys live, in Y-011's own words."""
        return describe_custody()

    # -- census (Y-022) -------------------------------------------------

    def _collector(self) -> CensusCollector:
        if self._census_collector is None:
            self._census_collector = CensusCollector(
                self.config.node_id,
                probes=self._census_probes,
                engines=self._census_engines,
                models_present=self._census_models,
                now=self._now,
            )
        return self._census_collector

    def census(self) -> dict:
        """This node's measured CapabilityCensus (Y-022): the
        current one, measured on first asking. Values are probe
        readings, never assumptions, and accelerator entries
        carry ``proven: False`` — detection is not proof."""
        collector = self._collector()
        return collector.current if collector.current is not None else collector.measure()

    def refresh_census(self) -> dict:
        """Re-measure the census; return it with the names of
        the fields that changed. A real change (not the first
        measurement, not a no-change refresh) is recorded as a
        ``node.census_changed`` Y-013 event, so the node's own
        ledger — and later the fabric — learns the machine
        changed, in field names."""
        census, changed = self._collector().refresh()
        if changed and changed != ("initial",):
            self._emit(
                "node.census_changed",
                {"changed_fields": list(changed), "fabric_connected": False},
            )
        return {"census": census, "changed_fields": list(changed)}

    # -- internals ------------------------------------------------------

    def _emit(self, type: str, payload: dict) -> dict:
        event = make_event(
            type=type,
            node_id=self.config.node_id,
            component="node_agent",
            payload=payload,
            seq=self._seq,
            at=self._now(),
        )
        self._seq += 1
        self._events.append(event)
        if self._sink is not None:
            self._sink.write(event)
        return event

    def _require(self, verb: str, *states: str) -> None:
        if self._state not in states:
            raise NodeAgentError(
                f"cannot {verb} from state {self._state!r} "
                f"(allowed from: {', '.join(states)})"
            )

    def _verified_identity(self) -> tuple[dict, str]:
        """Load and verify this node's identity, or raise.

        Every check is Y-011's: the keypair loads from the store,
        the node certificate verifies against the mesh-root
        certificate, the stored key matches the certificate, and
        the certificate agrees with the config on node_id and
        device class — the config may not rename the node its
        certificate names.
        """
        store = self._store
        try:
            keypair = store.load_keypair(self.config.node_id)
            root_cert = store.load_certificate(ROOT_NODE_ID)
            cert = store.load_certificate(self.config.node_id)
            verify_certificate(cert, root_cert, now=self._now())
        except IdentityError as exc:
            raise NodeAgentError(f"identity does not verify at start: {exc}") from exc
        if cert["public_key"] != base64.b64encode(keypair.public_key).decode("ascii"):
            raise NodeAgentError(
                "stored key does not match the node certificate's public key"
            )
        identity = certificate_identity(cert)
        if identity["node_id"] != self.config.node_id:
            raise NodeAgentError(
                f"certificate names node {identity['node_id']!r}, "
                f"config names {self.config.node_id!r}"
            )
        if identity["device_class"] != self.config.device_class:
            raise NodeAgentError(
                f"certificate device_class {identity['device_class']!r} != "
                f"config device_class {self.config.device_class!r}"
            )
        if self.config.display_name is not None:
            identity["display_name"] = self.config.display_name
        _validate(_IDENTITY_VALIDATOR, identity, "node identity")
        return identity, fingerprint(keypair.public_key)

    # -- lifecycle ----------------------------------------------------

    def start(self) -> dict:
        """created/stopped → started. Identity first: a node that
        cannot prove who it is does not start, and stays down."""
        self._require("start", CREATED, STOPPED)
        identity, fp = self._verified_identity()
        self._identity = identity
        self._fingerprint = fp
        self._in_flight.clear()
        self._joined_at = None
        self._started_at = self._now()
        self._state = STARTED
        self._emit(
            "node.started",
            {"lifecycle_state": STARTED, "fabric_connected": False},
        )
        return self.status()

    def join(self) -> dict:
        """started → joined. A local act until Part III's fabric
        exists: the event and the status both say no peer has
        been contacted (``fabric_connected`` stays False)."""
        self._require("join", STARTED)
        self._joined_at = self._now()
        self._state = JOINED
        self._emit(
            "node.joined",
            {
                "lifecycle_state": JOINED,
                "fabric_connected": False,
                "join_scope": TRANSPORT,
            },
        )
        return self.status()

    def begin_task(self, task_id: str) -> str:
        """Take a task lease. Only a joined node takes work;
        execution behind the lease is Y-029."""
        self._require("begin_task", JOINED)
        if not isinstance(task_id, str) or not task_id:
            raise NodeAgentError("a task lease needs a task_id")
        if task_id in self._in_flight:
            raise NodeAgentError(f"task {task_id} is already in flight on this node")
        self._in_flight.add(task_id)
        return task_id

    def end_task(self, task_id: str) -> None:
        """Release a task lease (joined or draining — a draining
        node is finishing exactly this work)."""
        self._require("end_task", JOINED, DRAINING)
        if task_id not in self._in_flight:
            raise NodeAgentError(f"task {task_id} is not in flight on this node")
        self._in_flight.discard(task_id)

    def drain(self) -> dict:
        """started/joined → draining: no new leases; health
        reports the governor shedding."""
        self._require("drain", STARTED, JOINED)
        self._state = DRAINING
        self._emit(
            "node.draining",
            {
                "lifecycle_state": DRAINING,
                "fabric_connected": False,
                "in_flight": len(self._in_flight),
            },
        )
        return self.status()

    def stop(self) -> dict:
        """started/joined/draining → stopped. Refused while
        leases are in flight: a node sheds its work (drain, then
        end the leases) before it leaves."""
        self._require("stop", STARTED, JOINED, DRAINING)
        if self._in_flight:
            raise NodeAgentError(
                f"stop refused: {len(self._in_flight)} task(s) still in flight "
                f"on this node — drain and end them before stopping"
            )
        self._state = STOPPED
        self._emit(
            "node.stopped",
            {"lifecycle_state": STOPPED, "fabric_connected": False},
        )
        return self.status()

    # -- reports --------------------------------------------------------

    def health(self) -> dict:
        """The Y-006 HealthReport, told from lifecycle truth.

        Models loaded is empty and queue depth is 0 because
        neither engines (Y-023) nor a queue (Y-031) exist yet —
        the report claims neither early. The endpoint Part III's
        heartbeats carry is Y-030."""
        report: dict = {
            "node_id": self.config.node_id,
            "at": _fmt(self._now()),
            "liveness": "present" if self._state in _PRESENT_STATES else "gone",
            "queue_depth": 0,
            "models_loaded": [],
        }
        if self._state == DRAINING:
            report["governor"] = {"shedding": True, "reason": "draining"}
        return _validate(_HEALTH_VALIDATOR, report, "health report")

    def status(self) -> dict:
        """The agent's local summary — not a contract shape; the
        contract shapes are identity, health, and events."""
        return {
            "node_id": self.config.node_id,
            "device_class": self.config.device_class,
            "agent_version": __version__,
            "lifecycle_state": self._state,
            "identity": self._identity,
            "key_fingerprint": self._fingerprint,
            "custody": describe_custody(),
            "in_flight": sorted(self._in_flight),
            "started_at": _fmt(self._started_at) if self._started_at else None,
            "joined_at": _fmt(self._joined_at) if self._joined_at else None,
            "census": (
                self._census_collector.current
                if self._census_collector is not None
                else None
            ),
            "fabric_connected": False,
            "transport": TRANSPORT,
            "simulated": False,
        }


def build_agent(config_path: Path) -> NodeAgent:
    """Config file → agent (the systemd unit's front door)."""
    return NodeAgent(NodeConfig.load(Path(config_path)))


def _run_check(agent: NodeAgent) -> int:
    """The preflight: the whole lifecycle once, in-process —
    start, join, health, drain, stop — printed as JSON."""
    agent.start()
    agent.join()
    health = agent.health()
    agent.drain()
    status = agent.stop()
    print(json.dumps({"status": status, "health": health}, indent=2, sort_keys=True))
    return 0


def _serve(agent: NodeAgent) -> int:
    """Start, join, and serve until SIGTERM/SIGINT; then drain
    and stop before exiting — the systemd unit's lifecycle."""
    agent.start()
    status = agent.join()
    print(json.dumps(status, sort_keys=True), flush=True)
    stopped = threading.Event()

    def _on_signal(signum, frame):
        del signum, frame
        stopped.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    stopped.wait()
    if agent.in_flight:
        agent.drain()
        print(
            "yggdrasil node_agent: signal received with tasks in flight; "
            "drained and holding leases — refusing to vanish mid-task",
            file=sys.stderr,
        )
        return 1
    agent.drain()
    status = agent.stop()
    print(json.dumps(status, sort_keys=True), flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="yggdrasil-node-agent",
        description="The Yggdrasil node agent (Y-021 skeleton).",
    )
    parser.add_argument(
        "--config",
        default="/etc/yggdrasil/node.json",
        help="node config JSON (schema: node_config v1)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="run the lifecycle once (start, join, health, drain, stop) and exit",
    )
    args = parser.parse_args(argv)
    try:
        agent = build_agent(Path(args.config))
        return _run_check(agent) if args.check else _serve(agent)
    except NodeAgentError as exc:
        print(f"yggdrasil node_agent: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
