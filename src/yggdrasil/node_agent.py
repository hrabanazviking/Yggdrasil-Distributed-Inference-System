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
  loaded comes from the model lifecycle (Y-027) when one is
  wired and stays empty until then, and queue depth stays 0
  until the local queue exists (Y-031) — the report claims
  neither early.
- **census (Y-022, grown onto the skeleton):** ``census()``
  answers the node's measured CapabilityCensus through
  ``yggdrasil.census`` (probe-measured, never assumed), and
  ``refresh_census()`` re-measures and records a
  ``node.census_changed`` event naming the fields that changed.
  The skeleton itself measures nothing until asked. Engine
  adapters (Y-023 onward) feed the collector through the
  constructor's ``census_engines`` / ``census_models`` seams —
  an adapter's own discovery, in the census exactly as the
  adapter reported it. A model store (Y-026) handed in as
  ``model_store=`` fills the models seam by default: the
  census then lists only hash-verified models.
- **model lifecycle (Y-027, grown onto the skeleton):** a
  ``model_lifecycle=`` handed in at construction owns which
  store-verified models are resident in slots under the node's
  VRAM/RAM budgets, with measured swap costs. ``load_model`` /
  ``unload_model`` delegate to it (a started or joined node
  loads; unloading is also allowed while draining), and
  ``health()`` then reports its resident models as
  ``models_loaded`` — until one is wired, that list stays
  empty, as before: the report claims nothing early.
- **resource governor (Y-028, grown onto the skeleton):** a
  ``governor=`` handed in at construction holds the node's
  ceilings (thermal with hysteresis, memory fraction, CPU
  share). ``begin_task`` asks it before taking a lease — a
  node over a ceiling refuses new work gracefully, by name,
  and takes nothing; work already in flight is never touched.
  ``health()`` reports its verdict in the ``governor`` field
  (its reasons, or ``draining`` while draining, combined when
  both bite), and ``status()`` carries its last evaluated
  state. Without one wired, health keeps only drain's flag,
  exactly as before.
- **task sandbox (Y-029, grown onto the skeleton):** a
  ``sandbox=`` handed in at construction runs tasks bounded
  (time, tokens, output), cancellable, with streamed partial
  results. ``execute_task`` takes the lease first (the
  governor's admission rides along), holds a model-lifecycle
  use lease on the task's model for the run when one is wired
  (Y-027 — a running task's model cannot be unloaded), runs
  the sandbox, and releases both leases whatever the outcome.
  ``status()`` carries the sandbox's policy and its finished
  execution count. Without one wired, the agent still only
  leases, exactly as before.
- **local queue (Y-031, grown onto the skeleton):** a
  ``local_queue=`` handed in at construction holds tasks in
  priority order (the task's own criticality) behind a depth
  bound. ``queue_task`` accepts work only while joined — a
  draining node takes no new held work, exactly as it takes
  no new leases — and a full queue refuses by the Y-007 name
  ``queue_full``. ``health()`` then reports the queue's true
  depth (the Y-030 heartbeat agrees, by construction), and
  ``execute_next_queued`` peeks the head, runs it through
  the sandbox behind the lease, and only then takes it off
  the queue — a refused run stays held. Without one wired,
  queue depth stays 0, as before.
- **health endpoint (Y-030, grown onto the skeleton):**
  ``heartbeat()`` serves the validated ``node_health`` v1
  record — liveness, lifecycle, the health report, counted
  load, resident/present models, the governor's full state,
  census measured/fingerprint, and uptime — through
  ``yggdrasil.health_endpoint``, the shape Part III's
  heartbeats will carry (``fabric_connected`` still False,
  transport still local-only: the endpoint is a local
  handler, no socket is opened here). ``handle_health``
  answers ``(method, path)`` in-process — 200 while present,
  503 while gone, 404/405 named — and every heartbeat is
  recorded as a ``node.heartbeat`` Y-013 event (counts and
  names only). ``status()`` carries the endpoint's beat
  count and last sequence number.
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
from yggdrasil.census import CensusCollector, census_fingerprint
from yggdrasil.governor import GovernorError
from yggdrasil.health_endpoint import HealthError, NodeHealthEndpoint
from yggdrasil.identity import (
    ROOT_NODE_ID,
    IdentityError,
    LocalKeyStore,
    certificate_identity,
    describe_custody,
    fingerprint,
    verify_certificate,
)
from yggdrasil.local_queue import QueueError
from yggdrasil.model_lifecycle import ModelLifecycleError
from yggdrasil.observability import LocalJsonlSink, make_event
from yggdrasil.sandbox import SandboxError

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
        model_store=None,
        model_lifecycle=None,
        governor=None,
        sandbox=None,
        local_queue=None,
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
        # The model store (Y-026): when one is handed in and no
        # explicit census_models was given, the census lists
        # exactly the store's hash-verified models — presence is
        # the store's measurement, re-hashed on every census.
        self._model_store = model_store
        if census_models is None and model_store is not None:
            census_models = model_store.model_names_for_census
        self._census_models = census_models
        # The model lifecycle (Y-027): residency in slots under
        # memory budgets, with measured swap costs. When wired,
        # health() reports its resident models.
        self._model_lifecycle = model_lifecycle
        # The resource governor (Y-028): ceilings on heat,
        # memory, and CPU share. When wired, begin_task asks
        # its permission first, and health() reports its verdict.
        self._governor = governor
        # The task sandbox (Y-029): bounded, cancellable task
        # execution with streamed partial results. When wired,
        # execute_task runs behind the lease this skeleton has
        # held since Y-021.
        self._sandbox = sandbox
        # The local queue (Y-031): tasks held per node in
        # priority order. When wired, health() reports its
        # true depth and queue_task holds work for the node.
        self._local_queue = local_queue
        self._health_endpoint: NodeHealthEndpoint | None = None
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

    @property
    def model_store(self):
        """The node's model store (Y-026), or None when the
        agent was built without one."""
        return self._model_store

    @property
    def model_lifecycle(self):
        """The node's model lifecycle manager (Y-027), or None
        when the agent was built without one."""
        return self._model_lifecycle

    @property
    def governor(self):
        """The node's resource governor (Y-028), or None when
        the agent was built without one."""
        return self._governor

    @property
    def sandbox(self):
        """The node's task execution sandbox (Y-029), or None
        when the agent was built without one."""
        return self._sandbox

    @property
    def local_queue(self):
        """The node's local queue (Y-031), or None when the
        agent was built without one."""
        return self._local_queue

    # -- task execution (Y-029) -----------------------------------------

    def execute_task(
        self,
        task: dict,
        runner,
        *,
        engine: str,
        model: str,
        memory_bytes: int = 0,
        cpu_share: float = 0.0,
        on_event=None,
    ):
        """Run one task in the sandbox (Y-029), behind this
        agent's lease: ``begin_task`` first (a joined node, the
        governor's admission when one is wired), a
        model-lifecycle use lease on ``model`` for the run's
        duration when a lifecycle is wired — a model with a
        task running against it cannot be unloaded or evicted
        (Y-027) — then the sandbox itself, bounded and
        cancellable, streamed through ``on_event``. Both leases
        release whatever the outcome. Returns the sandbox's
        (ResultEnvelope, StreamEvents, SandboxReport)."""
        if self._sandbox is None:
            raise NodeAgentError("this agent has no task sandbox wired")
        if not isinstance(task, dict) or not isinstance(task.get("id"), str):
            raise NodeAgentError("execute_task needs a task envelope with an id")
        task_id = task["id"]
        self.begin_task(task_id, memory_bytes=memory_bytes, cpu_share=cpu_share)
        acquired = False
        try:
            if self._model_lifecycle is not None:
                try:
                    self._model_lifecycle.acquire(model)
                    acquired = True
                except ModelLifecycleError as exc:
                    raise NodeAgentError(str(exc)) from exc
            try:
                return self._sandbox.run(
                    task, runner, engine=engine, model=model, on_event=on_event
                )
            except SandboxError as exc:
                raise NodeAgentError(str(exc)) from exc
        finally:
            try:
                if acquired:
                    self._model_lifecycle.release(model)
            finally:
                self.end_task(task_id)

    # -- local queue (Y-031) ----------------------------------------------

    def queue_task(self, task: dict) -> dict:
        """Hold one task in the local queue (Y-031); returns
        its validated queued-task entry. Only a joined node
        takes new held work — a draining node sheds, it does
        not gather. A full queue refuses by the Y-007 name
        ``queue_full``; a duplicate id refuses by name."""
        if self._local_queue is None:
            raise NodeAgentError("this agent has no local queue wired")
        self._require("queue_task", JOINED)
        try:
            return self._local_queue.enqueue(task)
        except QueueError as exc:
            raise NodeAgentError(str(exc)) from exc

    def cancel_queued_task(self, task_id: str) -> bool:
        """Remove one held task without serving it (Y-031).
        False when it is not held."""
        if self._local_queue is None:
            raise NodeAgentError("this agent has no local queue wired")
        self._require("cancel_queued_task", JOINED, DRAINING)
        return self._local_queue.cancel(task_id)

    def next_queued_task(self) -> dict | None:
        """The entry at the head of the queue's service order
        (Y-031), without taking it; None when empty."""
        if self._local_queue is None:
            raise NodeAgentError("this agent has no local queue wired")
        return self._local_queue.peek()

    def execute_next_queued(
        self,
        runner,
        *,
        engine: str,
        model: str,
        memory_bytes: int = 0,
        cpu_share: float = 0.0,
        on_event=None,
    ):
        """Serve the queue's head through the sandbox (Y-029
        + Y-031): peek the next task, run it behind the lease
        via :meth:`execute_task`, and only then take it off
        the queue — so a run refused before it starts (the
        governor, a non-resident model) leaves the task held,
        not vanished. Returns the sandbox's triple, or None
        when the queue is empty."""
        if self._local_queue is None:
            raise NodeAgentError("this agent has no local queue wired")
        self._require("execute_next_queued", JOINED)
        entry = self._local_queue.peek()
        if entry is None:
            return None
        task = self._local_queue.held_task(entry["task_id"])
        if task is None:  # taken by another hand between peek and fetch
            return None
        outcome = self.execute_task(
            task,
            runner,
            engine=engine,
            model=model,
            memory_bytes=memory_bytes,
            cpu_share=cpu_share,
            on_event=on_event,
        )
        self._local_queue.remove(entry["task_id"])
        return outcome

    def queue_report(self) -> dict | None:
        """The local queue's numbers (Y-031), or None when no
        queue is wired."""
        return self._local_queue.report() if self._local_queue is not None else None

    # -- model lifecycle (Y-027) ------------------------------------------

    def load_model(self, name: str, **kwargs) -> dict:
        """Load a store-verified model into a slot (Y-027).
        Only a started or joined node loads models."""
        if self._model_lifecycle is None:
            raise NodeAgentError("this agent has no model lifecycle wired")
        self._require("load_model", STARTED, JOINED)
        try:
            return self._model_lifecycle.load(name, **kwargs)
        except ModelLifecycleError as exc:
            raise NodeAgentError(str(exc)) from exc

    def unload_model(self, name: str) -> dict:
        """Unload a resident model (Y-027); also allowed while
        draining, when a node is shedding exactly this weight."""
        if self._model_lifecycle is None:
            raise NodeAgentError("this agent has no model lifecycle wired")
        self._require("unload_model", STARTED, JOINED, DRAINING)
        try:
            return self._model_lifecycle.unload(name)
        except ModelLifecycleError as exc:
            raise NodeAgentError(str(exc)) from exc

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

    def begin_task(self, task_id: str, *, memory_bytes: int = 0, cpu_share: float = 0.0) -> str:
        """Take a task lease. Only a joined node takes work;
        execution behind the lease is Y-029. When a resource
        governor (Y-028) is wired, it is asked first: a node
        over its heat, memory, or CPU ceiling refuses the new
        lease gracefully — by name, with the governor's
        reasons — and takes nothing."""
        self._require("begin_task", JOINED)
        if not isinstance(task_id, str) or not task_id:
            raise NodeAgentError("a task lease needs a task_id")
        if task_id in self._in_flight:
            raise NodeAgentError(f"task {task_id} is already in flight on this node")
        if self._governor is not None:
            try:
                decision = self._governor.admit(
                    task_id=task_id, memory_bytes=memory_bytes, cpu_share=cpu_share
                )
            except GovernorError as exc:
                raise NodeAgentError(str(exc)) from exc
            if not decision["admitted"]:
                raise NodeAgentError(
                    f"governor refused task {task_id}: "
                    f"{', '.join(decision['reasons'])} "
                    f"(projected memory fraction {decision['projected_memory_fraction']}, "
                    f"projected cpu share {decision['projected_cpu_share']})"
                )
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

        Models loaded comes from the model lifecycle (Y-027)
        when one is wired, and stays empty until then; queue
        depth is the local queue's true depth (Y-031) when one
        is wired and stays 0 until then — the report claims
        neither early. The endpoint Part III's heartbeats carry
        is Y-030."""
        report: dict = {
            "node_id": self.config.node_id,
            "at": _fmt(self._now()),
            "liveness": "present" if self._state in _PRESENT_STATES else "gone",
            "queue_depth": len(self._local_queue) if self._local_queue is not None else 0,
            "models_loaded": (
                self._model_lifecycle.loaded_models()
                if self._model_lifecycle is not None
                else []
            ),
        }
        if self._state == DRAINING:
            report["governor"] = {"shedding": True, "reason": "draining"}
        if self._governor is not None:
            try:
                verdict = self._governor.evaluate()
            except GovernorError as exc:
                raise NodeAgentError(str(exc)) from exc
            reasons = list(verdict["reasons"])
            if self._state == DRAINING and "draining" not in reasons:
                reasons.insert(0, "draining")
            report["governor"] = {
                "shedding": bool(reasons),
                "reason": ", ".join(reasons) if reasons else "within_ceilings",
            }
        return _validate(_HEALTH_VALIDATOR, report, "health report")

    # -- health endpoint (Y-030) ------------------------------------------

    @property
    def health_endpoint(self) -> NodeHealthEndpoint:
        """This node's health endpoint (Y-030), built lazily:
        constructing it serves no heartbeat and measures
        nothing — beats happen only when asked."""
        if self._health_endpoint is None:
            self._health_endpoint = NodeHealthEndpoint(
                self.config.node_id,
                self._health_snapshot,
                on_heartbeat=self._record_heartbeat,
            )
        return self._health_endpoint

    def _record_heartbeat(self, beat: dict) -> None:
        self._emit(
            "node.heartbeat",
            {
                "seq": beat["seq"],
                "liveness": beat["liveness"],
                "lifecycle_state": beat["lifecycle_state"],
                "in_flight": beat["load"]["in_flight"],
                "queue_depth": beat["load"]["queue_depth"],
                "models_loaded_count": len(beat["models"]["loaded"]),
                "fabric_connected": False,
            },
        )

    def _health_snapshot(self) -> dict:
        """The node's present component records, assembled
        from exactly the components wired into this agent —
        nothing is measured for the occasion beyond the
        governor's own evaluation and the model store's
        re-hash (its ask-time measurement, Y-026)."""
        health = self.health()
        governor_state = None
        readings: dict = {"thermal_c": None, "memory_fraction": None, "cpu_share_used": None}
        if self._governor is not None:
            try:
                governor_state = self._governor.evaluate()
            except GovernorError as exc:
                raise NodeAgentError(str(exc)) from exc
            readings = governor_state["readings"]
        if self._model_store is not None:
            models_present: list[str] | None = self._model_store.present_models()
        elif self._census_collector is not None and self._census_collector.current is not None:
            models_present = list(self._census_collector.current.get("models_present", []))
        else:
            models_present = None
        census_now = (
            self._census_collector.current if self._census_collector is not None else None
        )
        uptime = None
        if self._state in _PRESENT_STATES and self._started_at is not None:
            uptime = max(0.0, (self._now() - self._started_at).total_seconds())
        return {
            "lifecycle_state": self._state,
            "health": health,
            "load": {
                "in_flight": len(self._in_flight),
                "queue_depth": health["queue_depth"],
                "active_tasks": (
                    len(self._sandbox.active_task_ids)
                    if self._sandbox is not None
                    else len(self._in_flight)
                ),
                "executions": (
                    len(self._sandbox.reports) if self._sandbox is not None else 0
                ),
                "thermal_c": readings["thermal_c"],
                "memory_fraction": readings["memory_fraction"],
                "cpu_share_used": readings["cpu_share_used"],
            },
            "models": {
                "loaded": list(health["models_loaded"]),
                "present": models_present,
                "loader_simulated": (
                    self._model_lifecycle.loader_simulated
                    if self._model_lifecycle is not None
                    else None
                ),
            },
            "governor_state": governor_state,
            "census": {
                "measured": census_now is not None,
                "measured_at": census_now["measured_at"] if census_now else None,
                "fingerprint": census_fingerprint(census_now) if census_now else None,
            },
            "uptime_seconds": uptime,
        }

    def heartbeat(self) -> dict:
        """Serve one heartbeat (Y-030): the validated
        node_health v1 record Part III's fabric will carry."""
        try:
            return self.health_endpoint.heartbeat()
        except HealthError as exc:
            raise NodeAgentError(str(exc)) from exc

    def handle_health(self, method: str, path: str) -> tuple[int, dict]:
        """Answer one health request in-process (Y-030):
        ``(status, body)`` — no socket is opened; a transport
        (Part III) binds to this seam later."""
        try:
            return self.health_endpoint.handle(method, path)
        except HealthError as exc:
            raise NodeAgentError(str(exc)) from exc

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
            "models_loaded": (
                self._model_lifecycle.loaded_models()
                if self._model_lifecycle is not None
                else []
            ),
            "governor": self._governor.last_state if self._governor is not None else None,
            "health_endpoint": {
                "beats": self._health_endpoint.beats if self._health_endpoint else 0,
                "last_seq": (
                    self._health_endpoint.last["seq"]
                    if self._health_endpoint is not None and self._health_endpoint.last
                    else None
                ),
            },
            "sandbox": (
                {
                    "policy": self._sandbox.policy,
                    "active": list(self._sandbox.active_task_ids),
                    "executions": len(self._sandbox.reports),
                }
                if self._sandbox is not None
                else None
            ),
            "queue": self.queue_report(),
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
    heartbeat = agent.heartbeat()
    agent.drain()
    status = agent.stop()
    print(
        json.dumps(
            {"status": status, "health": health, "heartbeat": heartbeat},
            indent=2,
            sort_keys=True,
        )
    )
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
