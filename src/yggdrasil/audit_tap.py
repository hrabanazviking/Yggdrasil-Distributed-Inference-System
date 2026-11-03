"""Audit tap — Y-071.

Every Part III door counts what it does, and Y-064
gives the operator those counted standings. What
debugging sometimes needs instead is the events
themselves, in order — and a feed of fabric events
is a sensitive thing to leave lying around: it is
the mesh's memory of who said what to whom, and
the threat model (T-10) names the careless operator
who leaves a tap on as one of the mesh's own
enemies. This slice is that feed, kept by
:class:`AuditTap`, built so its dangers are
answered in its shape:

- **Off by default, on only by consent.** The
  policy's law is schema constants:
  ``default_state`` always ``off``,
  ``consent_required`` always true. The tap is
  born off; while off, an event handed to it is
  refused ``tap_off`` — counted, and *nothing*
  retained. Turning it on takes a named operator
  and a stated reason (``consent_missing``
  otherwise); the consent stands in the report —
  who enabled the tap, when, and why — for as
  long as it is on, so a tap left on is visible
  at a glance.
- **Loud when on.** Enabling and disabling are
  acts judged by named record like everything at
  the door, and both are evented
  (``audit_tap.enabled`` / ``audit_tap.disabled``):
  the tap's own switching can never be silent or
  hidden from its record, and its state stands
  first in its report.
- **Read-only.** The tap holds no handle on any
  fabric and has no road back into one: events
  are copied *in* (validated whole as Y-013
  EventEnvelopes first — a malformed event is
  refused ``malformed_event``), held as copies,
  and copied *out* on every read, so neither the
  source nor a reader can alter the feed through
  a dict it holds. Nothing observed can be
  injected, edited, or deleted from outside.
- **Bounded and counted, never silent.** The
  retained feed is a ring at the policy's
  ``max_events``: past the bound the oldest event
  is dropped and counted (``overflow_dropped``),
  never quietly overwritten. The policy's
  ``type_filter`` names the only types retained
  (empty retains all); any other type is refused
  ``type_filtered`` and counted apart. Reads are
  counted too — how often the feed was read and
  how many events it handed out stand in the
  report.
- **Real events feed the tap without rewiring.**
  A component's own event list — the Y-050
  fabric's, above all — is handed in with
  ``observe`` / ``observe_all``; the tap reads
  the envelopes the components already produce
  and changes none of them.

Honesty: everything here is SIMULATED —
``simulated: true`` is a schema constant on the
record and the report. The tap judges events
handed to it in-process; it opens no socket,
crosses no LAN, and sniffs no wire — it sees
exactly the events a component's keeper hands
it, no more, and claims no more. Event payloads
are whatever the emitting component put in its
Y-013 envelope (ids, counts, and names by that
contract's own discipline); the tap adds no
content of its own beyond the event's type and
its source's name in its records.
"""

import copy
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.observability import make_event, validate_event

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"


def _load_schema(name: str) -> dict:
    return json.loads((_SCHEMAS / name).read_text())


def _validator(name: str):
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


_POLICY_VALIDATOR = _validator("audit_tap_policy.schema.json")
_RECORD_VALIDATOR = _validator("audit_tap_record.schema.json")
_REPORT_VALIDATOR = _validator("audit_tap_report.schema.json")


class AuditTapError(Exception):
    """The tap's own failure: a policy, record, or
    report that fails its schema. (Judged acts at
    the door are refused by named record instead;
    see ``turn_on``, ``turn_off``, and ``observe``.)"""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record, kind: str):
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise AuditTapError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_policy(record: dict) -> dict:
    return _validate(_POLICY_VALIDATOR, record, "audit tap policy")


def validate_record(record: dict) -> dict:
    return _validate(_RECORD_VALIDATOR, record, "audit tap record")


def validate_report(record: dict) -> dict:
    return _validate(_REPORT_VALIDATOR, record, "audit tap report")


def default_policy() -> dict:
    """The tap's ordinary shape: off until an
    operator consents, every event type retained,
    and at most 1,024 events held in the feed —
    a policy, labelled as one: an operator's
    bounds, not a measurement of any mesh."""
    return validate_policy(
        {
            "version": 1,
            "default_state": "off",
            "consent_required": True,
            "read_only": True,
            "announce_when_on": True,
            "max_events": 1024,
            "type_filter": [],
        }
    )


def _named(value) -> str | None:
    """A name worth recording: a non-empty string,
    or None."""
    return value if isinstance(value, str) and value else None


class AuditTap:
    """The audit tap: an operator-consented,
    read-only feed of fabric events, off by
    default and loud when on. ``now`` is
    injectable so reports are testable; it stamps
    records, reports, and the tap's own events
    only."""

    def __init__(self, policy: dict | None = None, *, now=None):
        self._policy = validate_policy(
            copy.deepcopy(policy) if policy is not None else default_policy()
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._on = False
        self._enabled_by: str | None = None
        self._enabled_at: str | None = None
        self._consent_reason: str | None = None
        self._feed: list[dict] = []
        self._records: list[dict] = []
        self._refusals_by_reason: dict[str, int] = {}
        self._events_seen = 0
        self._filtered_out = 0
        self._overflow_dropped = 0
        self._reads = 0
        self._events_returned = 0
        self._by_type: dict[str, int] = {}
        self._events: list[dict] = []
        self._event_seq = 0

    @property
    def policy(self) -> dict:
        return copy.deepcopy(self._policy)

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    @property
    def is_on(self) -> bool:
        return self._on

    def _emit(self, type_: str, node_id: str, payload: dict) -> None:
        self._event_seq += 1
        self._events.append(
            make_event(
                type=type_,
                node_id=node_id,
                component="fabric",
                seq=self._event_seq,
                at=self._now(),
                payload=payload,
            )
        )

    def _record(
        self, action, operator_id, node_id, event_type, verdict, reason
    ) -> dict:
        record = {
            "version": 1,
            "seq": len(self._records) + 1,
            "at": _fmt(self._now()),
            "action": action,
            "operator_id": operator_id,
            "node_id": node_id,
            "event_type": event_type,
            "verdict": verdict,
            "reason": reason,
            "events_retained": len(self._feed),
            "simulated": True,
        }
        checked = validate_record(record)
        self._records.append(checked)
        if verdict == "refused":
            self._refusals_by_reason[reason] = (
                self._refusals_by_reason.get(reason, 0) + 1
            )
            self._emit(
                "audit_tap.act_refused",
                operator_id or node_id or "audit-tap",
                {"action": action, "reason": reason},
            )
        return checked

    # -- consent: the switch --------------------------------

    def turn_on(self, operator_id, reason) -> dict:
        """Turn the tap on — the consent act. It
        takes a named operator and a stated reason;
        either missing is refused ``consent_missing``
        and the tap stays off. An accepted consent
        stands in the report (who, when, why) for as
        long as the tap is on, and the enabling is
        evented: the tap is loud when on from its
        first moment. Turning on an on tap is refused
        ``already_on``."""
        with self._lock:
            operator = _named(operator_id)
            stated = _named(reason)
            if operator is None or stated is None:
                return self._record(
                    "enable", operator, None, None, "refused", "consent_missing"
                )
            if self._on:
                return self._record(
                    "enable", operator, None, None, "refused", "already_on"
                )
            self._on = True
            self._enabled_by = operator
            self._enabled_at = _fmt(self._now())
            self._consent_reason = stated
            self._emit(
                "audit_tap.enabled",
                operator,
                {"operator_id": operator, "reason": stated},
            )
            return self._record("enable", operator, None, None, "accepted", None)

    def turn_off(self, operator_id) -> dict:
        """Turn the tap off, by a named operator —
        any named operator of the household may
        close the tap; the record names who did.
        The disabling is evented and recorded like
        the enabling, the consent standing is
        cleared from the report (the records keep
        the history), and the retained feed is NOT
        erased: it stays readable, as the debugging
        evidence it was gathered to be. Refused
        ``operator_missing`` without a name and
        ``not_on`` when the tap already stands off."""
        with self._lock:
            operator = _named(operator_id)
            if operator is None:
                return self._record(
                    "disable", None, None, None, "refused", "operator_missing"
                )
            if not self._on:
                return self._record(
                    "disable", operator, None, None, "refused", "not_on"
                )
            self._on = False
            self._enabled_by = None
            self._enabled_at = None
            self._consent_reason = None
            self._emit(
                "audit_tap.disabled", operator, {"operator_id": operator}
            )
            return self._record("disable", operator, None, None, "accepted", None)

    # -- intake ---------------------------------------------

    @staticmethod
    def _event_fields(event):
        if not isinstance(event, dict):
            return None, None
        event_type = event.get("type") if isinstance(event.get("type"), str) else None
        node_id = None
        source = event.get("source")
        if isinstance(source, dict) and isinstance(source.get("node_id"), str):
            node_id = source["node_id"]
        return node_id, event_type

    def observe(self, event) -> dict:
        """Hand one event to the tap. The event is
        validated whole as a Y-013 EventEnvelope
        and held as a copy; the caller's dict is
        never the feed's dict. Judged by name: a
        malformed event is refused
        ``malformed_event``; anything handed to an
        off tap is refused ``tap_off`` and retained
        nowhere; a type outside the policy's filter
        is refused ``type_filtered``. An accepted
        event past the retention bound drops the
        oldest retained event, counted in the
        report's ``overflow_dropped`` — never a
        silent overwrite."""
        with self._lock:
            node_id, event_type = self._event_fields(event)
            try:
                checked = validate_event(copy.deepcopy(event))
            except Exception:  # noqa: BLE001 — a malformed event is this act's data: refused by name and counted, never the tap's crash
                return self._record(
                    "observe", None, node_id, event_type,
                    "refused", "malformed_event",
                )
            node_id = checked["source"]["node_id"]
            event_type = checked["type"]
            if not self._on:
                return self._record(
                    "observe", None, node_id, event_type, "refused", "tap_off"
                )
            type_filter = self._policy["type_filter"]
            if type_filter and event_type not in type_filter:
                self._filtered_out += 1
                return self._record(
                    "observe", None, node_id, event_type,
                    "refused", "type_filtered",
                )
            self._feed.append(checked)
            self._events_seen += 1
            self._by_type[event_type] = self._by_type.get(event_type, 0) + 1
            while len(self._feed) > self._policy["max_events"]:
                dropped = self._feed.pop(0)
                self._overflow_dropped += 1
                self._by_type[dropped["type"]] -= 1
                if self._by_type[dropped["type"]] == 0:
                    del self._by_type[dropped["type"]]
            return self._record(
                "observe", None, node_id, event_type, "accepted", None
            )

    def observe_all(self, events) -> list[dict]:
        """Hand a component's whole event list to
        the tap — the road a keeper walks a fabric's
        events down. Returns each event's record, in
        order; one malformed event refuses its own
        record and stops nothing."""
        with self._lock:
            return [self.observe(event) for event in events]

    # -- the feed ------------------------------------------------

    def feed(self, *, type: str | None = None, limit: int | None = None) -> list[dict]:
        """Read the retained feed, in arrival order —
        the tap's one road out, and read-only: every
        event handed out is a fresh copy, so a
        reader cannot alter the feed. ``type`` keeps
        only events of that exact type; ``limit``
        caps how many are returned (the first ones,
        in order). The retained feed stays readable
        while the tap is off — it is the evidence
        the tap was turned on to gather. Every read
        is counted in the report."""
        with self._lock:
            chosen = [
                event for event in self._feed if type is None or event["type"] == type
            ]
            if limit is not None:
                chosen = chosen[: max(limit, 0)]
            self._reads += 1
            self._events_returned += len(chosen)
            return copy.deepcopy(chosen)

    # -- the door --------------------------------------------------

    def report(self) -> dict:
        with self._lock:
            return validate_report(
                {
                    "version": 1,
                    "at": _fmt(self._now()),
                    "simulated": True,
                    "policy": copy.deepcopy(self._policy),
                    "state": "on" if self._on else "off",
                    "enabled_by": self._enabled_by,
                    "enabled_at": self._enabled_at,
                    "consent_reason": self._consent_reason,
                    "events_retained": len(self._feed),
                    "events_seen": self._events_seen,
                    "filtered_out": self._filtered_out,
                    "overflow_dropped": self._overflow_dropped,
                    "reads": self._reads,
                    "events_returned": self._events_returned,
                    "events_by_type": dict(sorted(self._by_type.items())),
                    "refusals_by_reason": dict(
                        sorted(self._refusals_by_reason.items())
                    ),
                    "records": copy.deepcopy(self._records),
                }
            )


__all__ = [
    "AuditTap",
    "AuditTapError",
    "default_policy",
    "validate_policy",
    "validate_record",
    "validate_report",
]
