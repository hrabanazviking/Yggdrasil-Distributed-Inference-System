"""The fabric core — Y-050.

Part III opens on the transport Y-049 chose: ADR-002's
benchmark picked the **minimal signed gossip** — one direct
hop, a store-and-forward outbox as its persistence, no
broker and no extra service. This slice is that transport
as the one API every other component speaks to the mesh
through: :class:`Fabric`.

The one API, for all components:

- **Publish/subscribe** — ``publish(from_node, topic,
  payload)`` carries a validated ``fabric_message`` v1 to
  every node subscribed to ``topic``; ``subscribe`` /
  ``unsubscribe`` manage a node's topics. A node subscribed
  to a topic it publishes to receives its own publication,
  as on any pub/sub fabric — subscribers who want only
  others' news filter by ``from_node``.
- **Direct channels** — ``send(from_node, to_node,
  payload)`` carries one message to one named node.

Delivery is the chosen transport's own rules, the ones
Y-049's criterion 1 ran on:

- a reachable, attached peer is delivered to at once —
  its handler is called with the message, or, with no
  handler, the message waits in its ``inbox`` to be read;
- an unreachable or detached peer's messages are **held
  in the sender's store-and-forward outbox** — direct
  messages whole, and one held copy per unreachable
  subscriber for a publication — and flushed when the
  peer attaches or the mesh heals. Nothing is silently
  dropped: a full outbox refuses the *new* message by
  name at the door (the policy's bound, as data), and a
  delivery whose handler raises is counted, recorded as
  a ``fabric.delivery_failed`` event, and — for direct
  messages — returned to the outbox for a later
  ``flush`` rather than lost;
- a partition is modelled in the open: ``cut(nodes)``
  splits the named nodes away from the rest of the mesh
  (each side still delivers within itself), ``heal()``
  reunites it and flushes every outbox across the seam.

Every message is a validated ``fabric_message`` v1:
per-sender sequenced (``seq`` rises per sender, so a
receiver sees one sender's traffic in order), stamped
from an injectable clock, and sized before it travels —
a payload that is not JSON data, or larger than the
policy's ``max_payload_bytes``, is refused by name
before any delivery. Payloads travel as JSON copies, so
a sender mutating its own dict afterwards cannot rewrite
a message already carried.

**Signing is a seam, honestly.** The chosen transport is
*signed* gossip: pass a ``signer`` and every message
carries its signature over :func:`message_digest` (the
SHA-256 of the message's own canonical fields); pass a
``verifier`` and a message whose signature is missing or
fails verification is a failed delivery, named as such,
never quietly carried. The keys themselves are Y-011's
— this module holds no key material and mints no trust;
TLS carriage of these channels is built at Y-051
(``yggdrasil.tls_channels``), discovery at
Y-052 (``yggdrasil.discovery``), the registry service that rides these topics
Y-053, heartbeats Y-054, epidemic state gossip Y-055.

Honesty: this is an in-process model of the transport —
``simulated: true`` and ``transport: minimal-gossip``
are schema constants on every message and report. No
socket is opened, no LAN is crossed, and no delivery
here proves anything about a real network; the wire
itself arrives with Y-051's TLS channels and is proven
on hardware in Part VIII. What is proven here is the
API's shape and the transport's delivery rules, exactly
as Y-049 benchmarked them.
"""

import hashlib
import json
import re
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.observability import make_event

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

TRANSPORT = "minimal-gossip"

_TOPIC_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")


def _load_schema(name: str) -> dict:
    return json.loads((_SCHEMAS / name).read_text())


def _validator(name: str):
    """A validator for one schema, with every contract schema
    registered so relative $refs resolve — the registry
    pattern the earlier slices build."""
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


_MESSAGE_VALIDATOR = _validator("fabric_message.schema.json")
_POLICY_VALIDATOR = _validator("fabric_policy.schema.json")
_REPORT_VALIDATOR = _validator("fabric_report.schema.json")


class FabricError(Exception):
    """The fabric's own failure: a message, policy, or
    report that fails its schema, or a use of the API that
    would manufacture a delivery — an unattached sender, a
    duplicate attach, an outbox past its policy bound."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise FabricError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_message(record: dict) -> dict:
    return _validate(_MESSAGE_VALIDATOR, record, "fabric message")


def validate_policy(record: dict) -> dict:
    return _validate(_POLICY_VALIDATOR, record, "fabric policy")


def validate_report(record: dict) -> dict:
    return _validate(_REPORT_VALIDATOR, record, "fabric report")


def default_policy() -> dict:
    """The fabric's bounds as data: an outbox may hold
    1,024 undelivered messages per sender and one payload
    may be at most 256 KiB of JSON. A policy, labelled as
    one — an operator's numbers, not facts about a LAN."""
    return validate_policy(
        {"version": 1, "max_outbox_messages": 1024, "max_payload_bytes": 262_144}
    )


def message_digest(message: dict) -> str:
    """The digest a signature covers: SHA-256 over the
    canonical JSON of the message's own fields — id, kind,
    both ends, topic, per-sender seq, send time, and the
    payload itself. The signature field never signs
    itself. Pure function of the message, so any holder
    of the message recomputes the same digest."""
    signed = {
        key: message[key]
        for key in (
            "message_id",
            "kind",
            "from_node",
            "to_node",
            "topic",
            "seq",
            "sent_at",
            "payload",
        )
    }
    canonical = json.dumps(signed, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Fabric:
    """The mesh's one message API over the chosen gossip
    transport. ``policy`` defaults to
    :func:`default_policy`; ``now`` and ``id_factory``
    are injectable so runs are testable and replayable;
    ``signer`` / ``verifier`` are the Y-011 key seam —
    callables ``(node_id, digest) -> signature`` and
    ``(node_id, digest, signature) -> bool``. All state
    changes are lock-guarded; handlers are called with a
    copy of the message and may safely call back into the
    fabric."""

    def __init__(
        self,
        policy: dict | None = None,
        *,
        now=None,
        id_factory=None,
        signer=None,
        verifier=None,
    ):
        self._policy = validate_policy(
            dict(policy) if policy is not None else default_policy()
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._signer = signer
        self._verifier = verifier
        self._lock = threading.RLock()
        self._handlers: dict[str, object] = {}
        self._inboxes: dict[str, list[dict]] = {}
        self._subscriptions: dict[str, dict[str, None]] = {}
        self._outboxes: dict[str, list[tuple[dict, str]]] = {}
        self._seqs: dict[str, int] = {}
        self._split: set[str] | None = None
        self._published = 0
        self._sent_direct = 0
        self._delivered = 0
        self._failed = 0
        self._events: list[dict] = []
        self._event_seq = 0

    # -- events -------------------------------------------------

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

    # -- membership ----------------------------------------------

    @staticmethod
    def _check_node_id(node_id: str) -> str:
        if not isinstance(node_id, str) or not node_id or node_id.strip() != node_id:
            raise FabricError(f"node_id must be a non-empty name, got {node_id!r}")
        return node_id

    def attach(self, node_id: str, handler=None) -> None:
        """Bring a node onto the fabric, optionally with the
        handler its deliveries are called through (without
        one, deliveries wait in its inbox). Attaching
        flushes every outbox holding mail for this node."""
        node_id = self._check_node_id(node_id)
        with self._lock:
            if node_id in self._handlers:
                raise FabricError(f"node {node_id} is already attached")
            self._handlers[node_id] = handler
            self._inboxes.setdefault(node_id, [])
            self._emit("fabric.node_attached", node_id, {"node": node_id})
            self._flush_for_target(node_id)

    def detach(self, node_id: str) -> None:
        """Take a node off the fabric. Its subscriptions go
        with it (a re-attached node subscribes afresh); its
        unread inbox is kept for its return; mail others
        send it from now on waits in their outboxes."""
        node_id = self._check_node_id(node_id)
        with self._lock:
            if node_id not in self._handlers:
                raise FabricError(f"node {node_id} is not attached")
            del self._handlers[node_id]
            for topic in list(self._subscriptions):
                self._subscriptions[topic].pop(node_id, None)
                if not self._subscriptions[topic]:
                    del self._subscriptions[topic]
            self._emit("fabric.node_detached", node_id, {"node": node_id})

    def attached(self, node_id: str) -> bool:
        return node_id in self._handlers

    def set_handler(self, node_id: str, handler) -> None:
        """Replace an attached node's delivery handler."""
        node_id = self._check_node_id(node_id)
        with self._lock:
            if node_id not in self._handlers:
                raise FabricError(f"node {node_id} is not attached")
            self._handlers[node_id] = handler

    def inbox(self, node_id: str) -> list[dict]:
        """Drain and return the messages delivered to a
        handler-less node, in delivery order."""
        node_id = self._check_node_id(node_id)
        with self._lock:
            if node_id not in self._inboxes:
                raise FabricError(f"node {node_id} has never attached")
            drained, self._inboxes[node_id] = self._inboxes[node_id], []
            return drained

    # -- subscriptions --------------------------------------------

    @staticmethod
    def _check_topic(topic: str) -> str:
        if not isinstance(topic, str) or not _TOPIC_RE.match(topic):
            raise FabricError(
                f"topic must be dotted lowercase segments, got {topic!r}"
            )
        return topic

    def subscribe(self, node_id: str, topic: str) -> None:
        node_id = self._check_node_id(node_id)
        topic = self._check_topic(topic)
        with self._lock:
            if node_id not in self._handlers:
                raise FabricError(f"node {node_id} is not attached")
            self._subscriptions.setdefault(topic, {})[node_id] = None

    def unsubscribe(self, node_id: str, topic: str) -> None:
        node_id = self._check_node_id(node_id)
        topic = self._check_topic(topic)
        with self._lock:
            if node_id not in self._handlers:
                raise FabricError(f"node {node_id} is not attached")
            subscribers = self._subscriptions.get(topic, {})
            if node_id not in subscribers:
                raise FabricError(f"node {node_id} is not subscribed to {topic}")
            del subscribers[node_id]
            if not subscribers:
                del self._subscriptions[topic]

    def subscribers(self, topic: str) -> list[str]:
        topic = self._check_topic(topic)
        return list(self._subscriptions.get(topic, {}))

    # -- partition --------------------------------------------------

    def cut(self, nodes) -> None:
        """Split the named nodes away from the rest of the
        mesh. Each side keeps delivering within itself;
        traffic across the seam waits in sender outboxes
        until :meth:`heal`. Cutting again replaces the
        split."""
        split = {self._check_node_id(n) for n in nodes}
        if not split:
            raise FabricError("a cut must name at least one node")
        with self._lock:
            self._split = split

    def heal(self) -> int:
        """Reunite a cut mesh and flush every outbox across
        the seam. Returns how many held messages the heal
        delivered."""
        with self._lock:
            self._split = None
            delivered = 0
            for sender in list(self._outboxes):
                delivered += self._flush_outbox(sender)
            return delivered

    def _reachable(self, src: str, dst: str) -> bool:
        if self._split is None or src == dst:
            return True
        return (src in self._split) == (dst in self._split)

    # -- messages ---------------------------------------------------

    def _prepare_payload(self, payload) -> tuple[object, int]:
        try:
            encoded = json.dumps(payload, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise FabricError(f"payload is not JSON data: {exc}") from exc
        size = len(encoded.encode("utf-8"))
        if size > self._policy["max_payload_bytes"]:
            raise FabricError(
                f"payload of {size} bytes exceeds the policy bound "
                f"{self._policy['max_payload_bytes']} bytes"
            )
        return json.loads(encoded), size

    def _build_message(
        self,
        *,
        kind: str,
        from_node: str,
        to_node: str | None,
        topic: str | None,
        payload,
    ) -> dict:
        copied, _size = self._prepare_payload(payload)
        self._seqs[from_node] = self._seqs.get(from_node, 0) + 1
        message = {
            "version": 1,
            "message_id": self._id_factory(),
            "kind": kind,
            "from_node": from_node,
            "to_node": to_node,
            "topic": topic,
            "payload": copied,
            "seq": self._seqs[from_node],
            "sent_at": _fmt(self._now()),
            "transport": TRANSPORT,
            "simulated": True,
            "signature": None,
        }
        if self._signer is not None:
            message["signature"] = self._signer(from_node, message_digest(message))
        return validate_message(message)

    def _deliver(self, message: dict, target: str) -> bool:
        """One delivery attempt. True when the message
        reached the target's handler or inbox; False when
        it failed — unverifiable signature or a handler
        that raised — counted and evented either way it
        fails, never silently."""
        if self._verifier is not None:
            signature = message["signature"]
            ok = signature is not None and bool(
                self._verifier(message["from_node"], message_digest(message), signature)
            )
            if not ok:
                self._failed += 1
                self._emit(
                    "fabric.delivery_failed",
                    message["from_node"],
                    {
                        "sender": message["from_node"],
                        "target": target,
                        "kind": message["kind"],
                        "reason": "signature_invalid",
                    },
                )
                return False
        handler = self._handlers.get(target)
        if handler is None:
            self._inboxes.setdefault(target, []).append(
                json.loads(json.dumps(message))
            )
            self._delivered += 1
            return True
        try:
            handler(json.loads(json.dumps(message)))
        except Exception:  # noqa: BLE001 — a handler's failure is this delivery's data: counted, evented, and (for direct mail) held for retry, never the fabric's crash
            self._failed += 1
            self._emit(
                "fabric.delivery_failed",
                message["from_node"],
                {
                    "sender": message["from_node"],
                    "target": target,
                    "kind": message["kind"],
                    "reason": "handler_error",
                },
            )
            return False
        self._delivered += 1
        return True

    def _queue(self, message: dict, target: str) -> None:
        outbox = self._outboxes.setdefault(message["from_node"], [])
        if len(outbox) >= self._policy["max_outbox_messages"]:
            raise FabricError(
                f"outbox for {message['from_node']} is at the policy bound "
                f"{self._policy['max_outbox_messages']} messages; "
                f"message to {target} refused, not silently dropped"
            )
        outbox.append((message, target))
        self._emit(
            "fabric.message_queued",
            message["from_node"],
            {
                "sender": message["from_node"],
                "target": target,
                "kind": message["kind"],
                "topic": message["topic"] or "none",
            },
        )

    def publish(self, from_node: str, topic: str, payload) -> dict:
        """Publish one message to a topic: delivered at
        once to every reachable subscriber, held in the
        publisher's outbox for each unreachable one, and
        flushed to them on heal. A subscriber whose handler
        fails is a failed delivery, counted — the rest of
        the topic is still served. Returns the validated
        message."""
        from_node = self._check_node_id(from_node)
        topic = self._check_topic(topic)
        with self._lock:
            if from_node not in self._handlers:
                raise FabricError(f"node {from_node} is not attached")
            targets = list(self._subscriptions.get(topic, {}))
            held = [
                t
                for t in targets
                if t not in self._handlers or not self._reachable(from_node, t)
            ]
            outbox = self._outboxes.get(from_node, [])
            if len(outbox) + len(held) > self._policy["max_outbox_messages"]:
                raise FabricError(
                    f"publishing to {topic} would hold {len(held)} copies in "
                    f"{from_node}'s outbox, past the policy bound "
                    f"{self._policy['max_outbox_messages']}; refused whole, "
                    "nothing delivered or queued"
                )
            message = self._build_message(
                kind="publish",
                from_node=from_node,
                to_node=None,
                topic=topic,
                payload=payload,
            )
            self._published += 1
            held_set = set(held)
            for target in targets:
                if target in held_set:
                    self._queue(message, target)
                else:
                    self._deliver(message, target)
            return message

    def send(self, from_node: str, to_node: str, payload) -> dict:
        """Send one message down a direct channel. A
        reachable peer is delivered to at once; a detached
        or cut-off peer's message waits in the sender's
        outbox, as does one whose delivery just failed —
        held, counted, and flushed on attach, heal, or an
        explicit :meth:`flush`. Returns the validated
        message."""
        from_node = self._check_node_id(from_node)
        to_node = self._check_node_id(to_node)
        with self._lock:
            if from_node not in self._handlers:
                raise FabricError(f"node {from_node} is not attached")
            message = self._build_message(
                kind="direct",
                from_node=from_node,
                to_node=to_node,
                topic=None,
                payload=payload,
            )
            self._sent_direct += 1
            if to_node in self._handlers and self._reachable(from_node, to_node):
                if not self._deliver(message, to_node):
                    self._queue(message, to_node)
            else:
                self._queue(message, to_node)
            return message

    # -- outbox ------------------------------------------------------

    def outbox(self, node_id: str) -> list[dict]:
        """The messages one sender is holding, in the order
        they were queued — copies, so reading the outbox
        cannot rewrite it."""
        node_id = self._check_node_id(node_id)
        return [
            json.loads(json.dumps(message))
            for message, _target in self._outboxes.get(node_id, [])
        ]

    def _flush_outbox(self, sender: str) -> int:
        entries = self._outboxes.get(sender, [])
        if not entries:
            return 0
        kept: list[tuple[dict, str]] = []
        delivered = 0
        for message, target in entries:
            if target in self._handlers and self._reachable(sender, target):
                if self._deliver(message, target):
                    delivered += 1
                else:
                    kept.append((message, target))
            else:
                kept.append((message, target))
        self._outboxes[sender] = kept
        if delivered:
            self._emit(
                "fabric.outbox_flushed",
                sender,
                {"node": sender, "delivered": delivered, "remaining": len(kept)},
            )
        return delivered

    def _flush_for_target(self, target: str) -> int:
        delivered = 0
        for sender in list(self._outboxes):
            entries = self._outboxes[sender]
            kept: list[tuple[dict, str]] = []
            flushed_here = 0
            for message, entry_target in entries:
                if entry_target == target and self._reachable(sender, target):
                    if self._deliver(message, target):
                        delivered += 1
                        flushed_here += 1
                    else:
                        kept.append((message, entry_target))
                else:
                    kept.append((message, entry_target))
            self._outboxes[sender] = kept
            if flushed_here:
                self._emit(
                    "fabric.outbox_flushed",
                    sender,
                    {
                        "node": sender,
                        "delivered": flushed_here,
                        "remaining": len(kept),
                    },
                )
        return delivered

    def flush(self, node_id: str) -> int:
        """Retry one sender's whole outbox now. Returns how
        many held messages this flush delivered."""
        node_id = self._check_node_id(node_id)
        with self._lock:
            return self._flush_outbox(node_id)

    # -- report ------------------------------------------------------

    def report(self) -> dict:
        """The fabric's counted state as a validated
        ``fabric_report`` v1: attached nodes, topic
        subscriber counts, and the message totals — names
        and counts only, never payload content."""
        with self._lock:
            depths = {
                sender: len(entries)
                for sender, entries in self._outboxes.items()
                if entries
            }
            return validate_report(
                {
                    "version": 1,
                    "at": _fmt(self._now()),
                    "simulated": True,
                    "transport": TRANSPORT,
                    "attached_nodes": sorted(self._handlers),
                    "topics": {
                        topic: len(subs)
                        for topic, subs in sorted(self._subscriptions.items())
                    },
                    "published": self._published,
                    "sent_direct": self._sent_direct,
                    "delivered": self._delivered,
                    "queued": sum(depths.values()),
                    "failed_deliveries": self._failed,
                    "outbox_depths": depths,
                    "partitioned": bool(self._split),
                }
            )
