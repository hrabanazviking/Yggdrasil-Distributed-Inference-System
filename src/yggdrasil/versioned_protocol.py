"""Versioned protocol — Y-065.

Every fabric message already carries a ``version``
(Y-050), but until this slice nothing said what
happens when the two ends of a channel do not speak
the same one. The failure this slice removes is the
quiet kind: an old node meeting a new message and
crashing, or — worse — half-understanding it. Here a
mixed-version mesh **degrades by contract**:
:class:`ProtocolGate`.

- **Versions are data.** A validated
  ``protocol_policy`` v1 names the versions this
  mesh knows (``known_versions``), the one it
  prefers (``current_version``), and the floor below
  which a version is retired (``minimum_version``).
  The policy's law is schema constants:
  ``downgrade_allowed`` is always true and
  ``unknown_version_action`` is always
  ``refuse_and_count`` — a policy naming crash-on-
  unknown or no-downgrade fails validation.
- **Nodes declare what they speak.** A node enrols
  with the versions it speaks and the one it
  prefers. Enrolling in a version the policy does
  not know, preferring a version the node does not
  speak, or enrolling twice is refused by name.
- **Negotiation picks the highest common word.**
  ``negotiate(a, b)`` settles a pair's session
  version: the highest version both nodes speak,
  the policy knows, and the floor admits. A pair
  with no common word is a refused negotiation —
  counted, evented, returned as ``None`` — never an
  exception in the mesh's face. A session below a
  node's preferred version names that node
  ``degraded`` in the report: it works, at the older
  word, and the door says so.
- **Every message is judged, none crashes the
  gate.** ``carry`` takes a validated
  ``protocol_message`` v1 and answers a validated
  ``protocol_record`` v1:

  - the receiver speaks the message's version —
    delivered whole and exact, ``delivered``;
  - the receiver speaks only an older version both
    ends speak — delivered **degraded**: a copy at
    the older version with the fields that version
    does not have stripped and *named* in the
    record's ``dropped_fields`` (protocol v2 added
    ``priority`` and ``hop_limit``; a v1 copy carries
    neither). The payload is never touched;
  - anything else is **refused by name and counted
    apart**, delivered nowhere: ``unknown_version``
    (the policy has never heard of it),
    ``version_retired`` (below the floor),
    ``version_not_spoken`` (the sender claims a
    version it never enrolled in), ``no_common_version``
    (no older shared word to degrade to),
    ``unknown_sender`` / ``unknown_receiver``, and
    ``malformed`` (the message fails its schema, or
    carries a field ahead of the version it claims).

- **Real fabric messages feed the gate.**
  ``carry_fabric_message`` takes a validated Y-050
  ``fabric_message`` v1 direct message as it stands
  and carries it at its own version — the gate
  composes the fabric, it does not rewire it.

Honesty: everything here is SIMULATED —
``simulated: true`` is a schema constant on the
message, the record, and the report; the gate judges
and holds messages in-process, opens no socket, and
crosses no LAN (real carriage was Y-062's own
proof). The version field map below names v2's
additions because v2 is the newest word the policy
knows by default; a future version registers its
own fields there when it lands — until then a
version the policy does not know is refused, not
guessed at.
"""

import copy
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.fabric import validate_message as validate_fabric_message
from yggdrasil.observability import make_event

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

# The fields each protocol version added, beyond the
# version before it. Protocol v1 is the Y-050 fabric
# message's own shape; v2 added delivery intent —
# ``priority`` and ``hop_limit`` — as message fields.
# Downgrading a message strips exactly the fields its
# target version does not have, and names them.
FIELDS_BY_VERSION = {2: ("hop_limit", "priority")}


def _fields_above(version: int) -> list[str]:
    fields: list[str] = []
    for added_in, names in sorted(FIELDS_BY_VERSION.items()):
        if added_in > version:
            fields.extend(names)
    return sorted(fields)


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


_POLICY_VALIDATOR = _validator("protocol_policy.schema.json")
_MESSAGE_VALIDATOR = _validator("protocol_message.schema.json")
_RECORD_VALIDATOR = _validator("protocol_record.schema.json")
_REPORT_VALIDATOR = _validator("protocol_report.schema.json")


class ProtocolError(Exception):
    """The gate's own failure: a policy or enrolment
    that fails its contract, or a use of the API that
    would manufacture a standing — an unknown node in
    a negotiation, a duplicate enrolment."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record, kind: str):
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise ProtocolError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_policy(record: dict) -> dict:
    checked = _validate(_POLICY_VALIDATOR, record, "protocol policy")
    known = checked["known_versions"]
    if checked["current_version"] not in known:
        raise ProtocolError(
            "current_version_not_known: current_version must be one of known_versions"
        )
    if checked["minimum_version"] not in known:
        raise ProtocolError(
            "minimum_version_not_known: minimum_version must be one of known_versions"
        )
    if checked["minimum_version"] > checked["current_version"]:
        raise ProtocolError(
            "minimum_past_current: minimum_version must not exceed current_version"
        )
    return checked


def validate_message(record: dict) -> dict:
    return _validate(_MESSAGE_VALIDATOR, record, "protocol message")


def validate_record(record: dict) -> dict:
    return _validate(_RECORD_VALIDATOR, record, "protocol record")


def validate_report(record: dict) -> dict:
    return _validate(_REPORT_VALIDATOR, record, "protocol report")


def default_policy() -> dict:
    """The mesh's ordinary word: it knows protocol
    versions 1 and 2, prefers 2, and still admits 1 —
    a policy, not a measurement."""
    return validate_policy(
        {
            "version": 1,
            "known_versions": [1, 2],
            "current_version": 2,
            "minimum_version": 1,
            "downgrade_allowed": True,
            "unknown_version_action": "refuse_and_count",
        }
    )


class ProtocolGate:
    """The mixed-version door: enrolments, pair
    negotiations, and per-message judgement under one
    policy. ``now`` is injectable so records are
    testable."""

    def __init__(self, policy: dict | None = None, *, now=None):
        self._policy = validate_policy(
            copy.deepcopy(policy) if policy is not None else default_policy()
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._nodes: dict[str, dict] = {}
        self._sessions: dict[tuple[str, str], dict] = {}
        self._inboxes: dict[str, list[dict]] = {}
        self._negotiations = 0
        self._negotiations_refused = 0
        self._carried = 0
        self._delivered = 0
        self._degraded = 0
        self._refused = 0
        self._refusals_by_reason: dict[str, int] = {}
        self._dropped_fields: dict[str, int] = {}
        self._events: list[dict] = []
        self._event_seq = 0

    @property
    def policy(self) -> dict:
        return copy.deepcopy(self._policy)

    @property
    def events(self) -> list[dict]:
        return list(self._events)

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

    # -- enrolment -----------------------------------------

    def enrol(self, node_id: str, versions, *, preferred: int | None = None) -> dict:
        """Enrol one node with the protocol versions it
        speaks. A version the policy does not know is
        refused ``version_not_known``; a preference for
        a version the node does not speak is refused
        ``preferred_not_spoken``; enrolling twice is
        refused ``already_enrolled``. The preference
        defaults to the highest version spoken."""
        with self._lock:
            if node_id in self._nodes:
                raise ProtocolError(f"already_enrolled: node {node_id} is already enrolled")
            spoken = sorted({int(v) for v in versions}) if versions else []
            if not spoken:
                raise ProtocolError("versions_empty: a node must speak at least one version")
            for version in spoken:
                if version not in self._policy["known_versions"]:
                    raise ProtocolError(
                        f"version_not_known: protocol version {version} "
                        "is not one the policy knows"
                    )
            chosen = spoken[-1] if preferred is None else preferred
            if chosen not in spoken:
                raise ProtocolError(
                    f"preferred_not_spoken: node {node_id} does not speak "
                    f"its preferred version {chosen}"
                )
            record = {"node_id": node_id, "versions": spoken, "preferred": chosen}
            self._nodes[node_id] = record
            self._inboxes.setdefault(node_id, [])
            self._emit(
                "protocol.node_enrolled",
                node_id,
                {"versions": spoken, "preferred": chosen},
            )
            return copy.deepcopy(record)

    # -- negotiation -----------------------------------------

    def _common_version(self, a: str, b: str) -> int | None:
        shared = (
            set(self._nodes[a]["versions"])
            & set(self._nodes[b]["versions"])
            & set(self._policy["known_versions"])
        )
        admitted = [v for v in shared if v >= self._policy["minimum_version"]]
        return max(admitted) if admitted else None

    def negotiate(self, a: str, b: str) -> int | None:
        """Settle the session version for one pair: the
        highest version both speak that the policy
        knows and admits. Returns the version, or
        ``None`` when the pair has no common word — a
        refused negotiation, counted and evented,
        never raised. Either node being unenrolled is
        the caller's own error and raises."""
        with self._lock:
            for node_id in (a, b):
                if node_id not in self._nodes:
                    raise ProtocolError(f"unknown_node: node {node_id} is not enrolled")
            pair = tuple(sorted((a, b)))
            version = self._common_version(a, b)
            self._negotiations += 1
            degraded = []
            if version is None:
                self._negotiations_refused += 1
                self._sessions[pair] = {
                    "pair": list(pair),
                    "version": None,
                    "degraded_nodes": [],
                }
                self._emit(
                    "protocol.negotiation_refused",
                    pair[0],
                    {"pair": list(pair), "reason": "no_common_version"},
                )
                return None
            degraded = [
                node_id
                for node_id in pair
                if version < self._nodes[node_id]["preferred"]
            ]
            self._sessions[pair] = {
                "pair": list(pair),
                "version": version,
                "degraded_nodes": degraded,
            }
            self._emit(
                "protocol.session_agreed",
                pair[0],
                {"pair": list(pair), "version": version, "degraded_nodes": degraded},
            )
            return version

    def session(self, a: str, b: str) -> dict | None:
        """The standing session for a pair, if one has
        been negotiated."""
        with self._lock:
            found = self._sessions.get(tuple(sorted((a, b))))
            return copy.deepcopy(found) if found is not None else None

    # -- carriage -----------------------------------------

    def _record(self, message, verdict, reason, delivered_version, dropped_fields):
        self._carried += 1
        sender = message.get("from_node") if isinstance(message, dict) else None
        receiver = message.get("to_node") if isinstance(message, dict) else None
        message_id = message.get("message_id") if isinstance(message, dict) else None
        version = message.get("protocol_version") if isinstance(message, dict) else None
        record = {
            "version": 1,
            "seq": self._carried,
            "at": _fmt(self._now()),
            "sender": sender if isinstance(sender, str) else None,
            "receiver": receiver if isinstance(receiver, str) else None,
            "message_id": message_id if isinstance(message_id, str) else None,
            "message_version": version if isinstance(version, int) else None,
            "delivered_version": delivered_version,
            "verdict": verdict,
            "reason": reason,
            "dropped_fields": sorted(dropped_fields),
            "simulated": True,
        }
        if verdict == "refused":
            self._refused += 1
            self._refusals_by_reason[reason] = self._refusals_by_reason.get(reason, 0) + 1
        elif verdict == "degraded":
            self._degraded += 1
            for field in record["dropped_fields"]:
                self._dropped_fields[field] = self._dropped_fields.get(field, 0) + 1
        else:
            self._delivered += 1
        self._emit(
            "protocol.message_carried",
            record["sender"] or "unknown",
            {
                "verdict": verdict,
                "reason": reason or "none",
                "message_version": record["message_version"] or 0,
                "delivered_version": delivered_version or 0,
            },
        )
        return validate_record(record)

    def _refuse(self, message, reason):
        return self._record(message, "refused", reason, None, [])

    def carry(self, message) -> dict:
        """Judge and carry one ``protocol_message`` v1
        from its sender to its receiver. Delivered and
        degraded messages land in the receiver's inbox
        (a degraded one as a copy at the delivered
        version, its newer fields stripped and named);
        refused messages land nowhere. Every outcome is
        a validated record — traffic faults are counted,
        never raised."""
        with self._lock:
            try:
                checked = validate_message(copy.deepcopy(message))
            except Exception:  # noqa: BLE001 — a message that fails its contract is this door's data: refused malformed and counted, never the gate's crash
                return self._refuse(message, "malformed")
            sender, receiver = checked["from_node"], checked["to_node"]
            version = checked["protocol_version"]
            if sender not in self._nodes:
                return self._refuse(checked, "unknown_sender")
            if receiver not in self._nodes:
                return self._refuse(checked, "unknown_receiver")
            if version not in self._policy["known_versions"]:
                return self._refuse(checked, "unknown_version")
            if version < self._policy["minimum_version"]:
                return self._refuse(checked, "version_retired")
            ahead = [
                field
                for field in _fields_above(version)
                if checked.get(field) is not None
            ]
            if ahead:
                # A message claiming a version while
                # carrying a newer version's fields is
                # not that version's message at all.
                return self._refuse(checked, "malformed")
            if version not in self._nodes[sender]["versions"]:
                return self._refuse(checked, "version_not_spoken")
            if version in self._nodes[receiver]["versions"]:
                self._inboxes[receiver].append(checked)
                return self._record(checked, "delivered", None, version, [])
            shared = [
                v
                for v in self._nodes[receiver]["versions"]
                if v < version
                and v in self._nodes[sender]["versions"]
                and v >= self._policy["minimum_version"]
            ]
            if not shared:
                return self._refuse(checked, "no_common_version")
            target = max(shared)
            dropped = [
                field
                for field in _fields_above(target)
                if checked.get(field) is not None
            ]
            downgraded = copy.deepcopy(checked)
            downgraded["protocol_version"] = target
            for field in _fields_above(target):
                downgraded[field] = None
            self._inboxes[receiver].append(downgraded)
            return self._record(checked, "degraded", None, target, dropped)

    def carry_fabric_message(self, message) -> dict:
        """Carry one Y-050 ``fabric_message`` v1 direct
        message at its own version, without rewiring
        the fabric: the fabric message's ``version``
        is its protocol version, and it carries no
        newer fields. A publication (no single
        receiver) is refused ``malformed`` at this
        door — fan-out applies this same judgement per
        subscriber."""
        with self._lock:
            try:
                checked = validate_fabric_message(copy.deepcopy(message))
            except Exception:  # noqa: BLE001 — a fabric message that fails its contract is this door's data: refused malformed and counted, never the gate's crash
                return self._refuse(
                    message if isinstance(message, dict) else {}, "malformed"
                )
            if checked["to_node"] is None:
                return self._refuse(
                    {
                        "from_node": checked["from_node"],
                        "to_node": None,
                        "message_id": checked["message_id"],
                        "protocol_version": checked["version"],
                    },
                    "malformed",
                )
            return self.carry(
                {
                    "version": 1,
                    "message_id": checked["message_id"],
                    "from_node": checked["from_node"],
                    "to_node": checked["to_node"],
                    "protocol_version": checked["version"],
                    "sent_at": checked["sent_at"],
                    "priority": None,
                    "hop_limit": None,
                    "payload": checked["payload"],
                    "simulated": True,
                }
            )

    def inbox(self, node_id: str) -> list[dict]:
        """Drain and return the messages delivered to a
        node, in delivery order — degraded deliveries
        as the downgraded copies the node received."""
        with self._lock:
            if node_id not in self._nodes:
                raise ProtocolError(f"unknown_node: node {node_id} is not enrolled")
            drained, self._inboxes[node_id] = self._inboxes[node_id], []
            return drained

    # -- the door ------------------------------------------

    def report(self) -> dict:
        with self._lock:
            return validate_report(
                {
                    "version": 1,
                    "at": _fmt(self._now()),
                    "simulated": True,
                    "policy": copy.deepcopy(self._policy),
                    "nodes": [
                        copy.deepcopy(self._nodes[node_id])
                        for node_id in sorted(self._nodes)
                    ],
                    "sessions": [
                        copy.deepcopy(self._sessions[pair])
                        for pair in sorted(self._sessions)
                    ],
                    "negotiations": self._negotiations,
                    "negotiations_refused": self._negotiations_refused,
                    "carried": self._carried,
                    "delivered": self._delivered,
                    "degraded": self._degraded,
                    "refused": self._refused,
                    "refusals_by_reason": dict(sorted(self._refusals_by_reason.items())),
                    "dropped_fields": dict(sorted(self._dropped_fields.items())),
                }
            )
