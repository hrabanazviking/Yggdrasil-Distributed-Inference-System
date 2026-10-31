"""Node operator tools — Y-042.

The operator's door (Y-017) opened read-only: it could list,
but it could not act — its own doc said the verbs would
arrive "with the machinery they act on". The machinery is
here. This slice grows the door the operator's hand:

- **The verbs, on the agent (Y-021 grown).** ``drain()``
  already existed; Y-042 adds ``pause()`` / ``resume()`` and
  ``retire()`` to the node agent's lifecycle as the states
  ``paused`` and ``retired``. A paused node stays present —
  identity, models, queue, and in-flight leases all kept,
  in-flight work may finish — but takes nothing new, exactly
  as a draining node gathers nothing. Retirement is
  terminal and honest: refused while leases are in flight,
  refused for a node that never started, and a retired agent
  cannot be restarted by ``start()`` — a node that is wanted
  again is re-provisioned as a new agent, so the old one's
  book is never silently rewritten.
- **The ledger** (this module). Every verb ordered against
  a node lands as a validated ``operator_action`` v1 —
  who ordered it (``actor``, a name the operator gives, never
  a credential), the operator's stated ``reason`` when one
  was given, the states it moved between, or the refusal
  that met it with its words kept. Refusals are recorded
  *first* and then raised as :class:`OperatorToolsError`:
  a refused drain is part of the node's history, not a
  vanished keystroke. ``report()`` is the validated
  ``operator_report`` v1 — totals, applied and refused
  counted apart, retirement stated outright.
- **Inspect.** ``inspect()`` assembles the validated
  ``node_inspection`` v1 from the agent's own records —
  lifecycle and liveness, identity verified or not, leases
  in flight, queue depth, models loaded and present, the
  governor's shedding verdict, active failure incidents,
  journal entries, the generosity verdict, the degradation
  status, whether a census has ever been measured, and the
  citizen's readiness. A component that is not wired
  answers ``null`` — unwired is reported, never dressed as
  a calm zero.
- **Events.** Every action, applied or refused, is also
  emitted as a Y-013 EventEnvelope from the ``operator_cli``
  component (``operator.drained`` / ``operator.paused`` /
  ``operator.resumed`` / ``operator.inspected`` /
  ``operator.retired`` / ``operator.refused``) — action,
  outcome, and state names only, no reason text, no payload
  content.
- **The CLI grows with it.** ``yggdrasil node drain|pause|
  resume|inspect|retire`` (``src/yggdrasil/cli.py``) drives
  an :class:`OperatorTools` handed in by the caller — the
  deployment wiring that owns a live agent. With none handed
  in the command refuses by name: there is no demo agent
  behind the verbs, because a fake drain of a fake node
  would be exactly the lie this tree does not tell.

Honesty: the tools act on one agent *in this process* —
there is no fabric yet (Part III), so no verb reaches
across a network to a daemon elsewhere, and none is
claimed to. The ``simulated`` flag on every record is the
agent's own: tools over a citizen built from injected
probes say ``simulated: true``; tools over a plain agent
say ``false`` — the flag follows the node, never the wish.
Retire is a lifecycle farewell, not a wipe: keys, journal,
and the store on disk are the operator's property and are
left exactly where they lie.
"""

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.node_agent import NodeAgent, NodeAgentError
from yggdrasil.observability import ObservabilityError, make_event

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

DRAIN = "drain"
PAUSE = "pause"
RESUME = "resume"
INSPECT = "inspect"
RETIRE = "retire"
ACTIONS = (DRAIN, PAUSE, RESUME, INSPECT, RETIRE)

APPLIED = "applied"
REFUSED = "refused"

_EVENT_TYPES = {
    DRAIN: "operator.drained",
    PAUSE: "operator.paused",
    RESUME: "operator.resumed",
    INSPECT: "operator.inspected",
    RETIRE: "operator.retired",
}


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


_ACTION_VALIDATOR = _validator("operator_action.schema.json")
_REPORT_VALIDATOR = _validator("operator_report.schema.json")
_INSPECTION_VALIDATOR = _validator("node_inspection.schema.json")


class OperatorToolsError(Exception):
    """Any operator-tools failure: an unshaped ask, or a verb
    the node refused — recorded in the ledger before it is
    raised here."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise OperatorToolsError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_action(record: dict) -> dict:
    """An operator action against the operator_action v1 schema."""
    return _validate(_ACTION_VALIDATOR, record, "operator action")


def validate_report(record: dict) -> dict:
    """An operator report against the operator_report v1 schema."""
    return _validate(_REPORT_VALIDATOR, record, "operator report")


def validate_inspection(record: dict) -> dict:
    """A node inspection against the node_inspection v1 schema."""
    return _validate(_INSPECTION_VALIDATOR, record, "node inspection")


class OperatorTools:
    """One node's operator hand: the verbs, the ledger, and
    the inspection, over one :class:`NodeAgent` in this
    process.

    ``actor`` names who is ordering — a plain operator name
    recorded on every action this instance takes (default
    ``"operator"``); it is a name in a local ledger, never a
    credential, and holding these tools *is* the authority:
    whoever can run code in the node's own process already
    owns the node. ``now`` is an injectable clock.
    """

    def __init__(self, agent: NodeAgent, *, actor: str = "operator", now=None):
        if not isinstance(agent, NodeAgent):
            raise OperatorToolsError("operator tools need a NodeAgent to act on")
        if not isinstance(actor, str) or not actor.strip():
            raise OperatorToolsError("operator tools need an actor name")
        self._agent = agent
        self._actor = actor
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._actions: list[dict] = []
        self._events: list[dict] = []
        self._seq = 0

    # -- facts ----------------------------------------------------------

    @property
    def agent(self) -> NodeAgent:
        return self._agent

    @property
    def actor(self) -> str:
        return self._actor

    @property
    def node_id(self) -> str:
        return self._agent.node_id

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    def actions(self) -> list[dict]:
        """Every recorded action, in the order ordered."""
        return [dict(row) for row in self._actions]

    # -- internals --------------------------------------------------------

    def _check_reason(self, reason: str | None) -> str | None:
        if reason is None:
            return None
        if not isinstance(reason, str) or not reason.strip():
            raise OperatorToolsError("an operator reason must be a non-empty string")
        return reason

    def _queue_depth(self) -> int | None:
        report = self._agent.queue_report()
        return report["depth"] if report is not None else None

    def _simulated(self) -> bool:
        """The node's own simulation truth: a citizen's
        report carries it (a citizen built from injected
        probes is simulated; one measured live is not); a
        plain agent carries its class flag."""
        citizen = self._agent.citizen_report
        if citizen is not None:
            return bool(citizen.get("simulated", False))
        return bool(getattr(self._agent, "is_simulated", False))

    def _record(
        self,
        action: str,
        *,
        reason: str | None,
        outcome: str,
        from_state: str,
        to_state: str | None,
        refusal: str | None,
    ) -> dict:
        with self._lock:
            record = {
                "version": 1,
                "node_id": self.node_id,
                "at": _fmt(self._now()),
                "seq": self._seq,
                "action": action,
                "actor": self._actor,
                "reason": reason,
                "outcome": outcome,
                "from_state": from_state,
                "to_state": to_state,
                "refusal": refusal,
                "in_flight": len(self._agent.in_flight),
                "queue_depth": self._queue_depth(),
                "simulated": self._simulated(),
            }
            validate_action(record)
            self._actions.append(record)
            self._seq += 1
            try:
                event = make_event(
                    type=(
                        _EVENT_TYPES[action] if outcome == APPLIED else "operator.refused"
                    ),
                    node_id=self.node_id,
                    component="operator_cli",
                    payload={
                        "action": action,
                        "outcome": outcome,
                        "from_state": from_state,
                        "to_state": to_state,
                    },
                    seq=record["seq"],
                    at=self._now(),
                )
            except ObservabilityError as exc:
                raise OperatorToolsError(str(exc)) from exc
            self._events.append(event)
            return dict(record)

    def _act(self, action: str, verb, *, reason: str | None) -> dict:
        """Run one lifecycle verb and ledger the outcome. A
        refusal is recorded with the node's own words, then
        raised — the ledger keeps what the exception carries."""
        reason = self._check_reason(reason)
        from_state = self._agent.state
        try:
            verb()
        except NodeAgentError as exc:
            self._record(
                action,
                reason=reason,
                outcome=REFUSED,
                from_state=from_state,
                to_state=None,
                refusal=str(exc),
            )
            raise OperatorToolsError(str(exc)) from exc
        return self._record(
            action,
            reason=reason,
            outcome=APPLIED,
            from_state=from_state,
            to_state=self._agent.state,
            refusal=None,
        )

    # -- the verbs --------------------------------------------------------

    def drain(self, *, reason: str | None = None) -> dict:
        """Drain the node: no new work; in-flight work finishes."""
        return self._act(DRAIN, self._agent.drain, reason=reason)

    def pause(self, *, reason: str | None = None) -> dict:
        """Pause the node: present but taking nothing new,
        until resumed."""
        return self._act(PAUSE, self._agent.pause, reason=reason)

    def resume(self, *, reason: str | None = None) -> dict:
        """Resume a paused node to joined."""
        return self._act(RESUME, self._agent.resume, reason=reason)

    def retire(self, *, reason: str | None = None) -> dict:
        """Retire the node permanently (refused while leases
        are in flight — drain and end them first)."""
        return self._act(RETIRE, self._agent.retire, reason=reason)

    def inspect(self) -> dict:
        """The node's present-tense truth as one validated
        ``node_inspection`` v1, assembled from the agent's
        own records. Inspecting changes nothing on the node;
        the asking itself is ledgered like every verb."""
        agent = self._agent
        status = agent.status()
        health = agent.health()
        failures = agent.failure_report()
        active_incidents = None
        if failures is not None:
            active = failures["active"]
            active_incidents = (
                len(active["quarantined_engines"])
                + len(active["quarantined_models"])
                + (1 if active["thermal_throttling"] else 0)
                + (1 if active["oom_active"] else 0)
            )
        journal = agent.journal_report()
        degradation = agent.degradation_state()
        census = status["census"]
        models_present = None
        if agent.model_store is not None:
            models_present = agent.model_store.present_models()
        elif census is not None:
            models_present = list(census.get("models_present", []))
        inspection = {
            "version": 1,
            "node_id": self.node_id,
            "at": _fmt(self._now()),
            "lifecycle_state": status["lifecycle_state"],
            "liveness": health["liveness"],
            "device_class": status["device_class"],
            "identity_verified": status["identity"] is not None,
            "in_flight": list(status["in_flight"]),
            "queue_depth": self._queue_depth(),
            "models_loaded": list(status["models_loaded"]),
            "models_present": models_present,
            "governor_shedding": (
                health["governor"]["shedding"] if "governor" in health else None
            ),
            "governor_reason": (
                health["governor"]["reason"] if "governor" in health else None
            ),
            "active_failure_incidents": active_incidents,
            "journal_entries": (
                journal["total_entries"] if journal is not None else None
            ),
            "generous": (
                status["generosity"]["generous"]
                if status["generosity"] is not None
                else None
            ),
            "degradation_status": (
                degradation["status"] if degradation is not None else None
            ),
            "census_measured": census is not None,
            "citizen_ready": (
                status["citizen"]["ready"] if status["citizen"] is not None else None
            ),
            "fabric_connected": False,
            "transport": status["transport"],
            "simulated": self._simulated(),
        }
        validate_inspection(inspection)
        self._record(
            INSPECT,
            reason=None,
            outcome=APPLIED,
            from_state=status["lifecycle_state"],
            to_state=status["lifecycle_state"],
            refusal=None,
        )
        return inspection

    # -- the book ---------------------------------------------------------

    def report(self) -> dict:
        """The validated operator_report v1: every verb ever
        ordered through these tools, applied and refused
        counted apart, retirement stated outright."""
        with self._lock:
            by_action = {action: 0 for action in ACTIONS}
            applied = 0
            for row in self._actions:
                by_action[row["action"]] += 1
                if row["outcome"] == APPLIED:
                    applied += 1
            return validate_report(
                {
                    "version": 1,
                    "node_id": self.node_id,
                    "at": _fmt(self._now()),
                    "actions_total": len(self._actions),
                    "applied": applied,
                    "refused": len(self._actions) - applied,
                    "by_action": by_action,
                    "retired": self._agent.state == "retired",
                    "last_action_at": (
                        self._actions[-1]["at"] if self._actions else None
                    ),
                    "history": [dict(row) for row in self._actions],
                }
            )


__all__ = [
    "ACTIONS",
    "APPLIED",
    "DRAIN",
    "INSPECT",
    "PAUSE",
    "REFUSED",
    "RESUME",
    "RETIRE",
    "OperatorTools",
    "OperatorToolsError",
    "validate_action",
    "validate_inspection",
    "validate_report",
]
