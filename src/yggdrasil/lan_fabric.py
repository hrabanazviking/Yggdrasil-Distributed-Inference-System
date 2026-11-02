"""The fabric on the real LAN — Y-062.

Part III built the fabric in pieces, every one of them
in-process: the core (Y-050), TLS everywhere (Y-051),
discovery (Y-052), the registry (Y-053), gossip (Y-055),
the task channel (Y-057), provenance (Y-059), rate
discipline (Y-060), and the waiting room (Y-061). Each
of those slices ended with the same honesty line: real
carriage is Y-062. This slice is that carriage — the
composition that stands the pieces up on real machines,
over a real network, with TLS on and discovery unaided.

- **One node, composed of the pieces, not rewired.**
  A :class:`LanNode` holds exactly one Y-051
  :class:`~yggdrasil.tls_channels.TlsNodeChannels`
  door, one Y-052
  :class:`~yggdrasil.discovery.DiscoveryService`, and
  one Y-050 :class:`~yggdrasil.fabric.Fabric` — each
  constructed by its own slice's rules, each keeping
  its own custody. This module mints no key, weakens
  no check, and restamps no other module's record: a
  fabric message is built by the fabric itself (and
  carries the fabric's own ``simulated: true`` factory
  stamp, because the core is transport-agnostic
  machinery — what this slice makes real is the
  *carriage* between machines, never a relabelled
  envelope), an announcement is built and judged by
  discovery, and a channel is established by the TLS
  door. Sockets are the caller's, as everywhere in
  this tree: deployment wiring owns the wire
  (``deploy/lan_node.py``), this module owns the
  session that rides it.

- **A session is discovery first, then traffic.**
  :meth:`LanNode.open_client` and
  :meth:`LanNode.open_server` wrap a connected socket
  through the TLS door — mutual TLS 1.3, Y-011
  certificates, a peer this node was not told to trust
  by name refused at the handshake — and then exchange
  announcements *on the established channel*: each
  side's Y-052 announcement is heard by the other
  through discovery's one door of trust (certificate
  verifies against the mesh root; the announcement's
  identity agrees with the certificate). An
  announcement naming anyone but the channel's proven
  peer refuses the session by name
  (``announcement_mismatch``): the handshake's peer and
  the announcement's peer must be one node. Only then
  may Y-050 fabric messages cross, as records on the
  channel, carried whole and exact.

- **Reception is judged, not assumed.** A message
  arriving on a session is validated as a
  ``fabric_message`` v1, must name the session's
  proven peer as its sender (``sender_mismatch``), and
  a direct message must name this node as its target
  (``not_for_me``). Publications are handed to the
  handlers this node registered for their topic and
  kept in the wire inbox; every refusal is raised as
  a :class:`LanError` carrying its reason, counted in
  the node's report, and evented — nothing is silently
  dropped and nothing is delivered on a claim alone.

- **A run is judged as a whole.** Deployment wiring
  (a real run, or a test) reports each established
  channel as a validated ``lan_leg`` v1: the pair, the
  path the carriage crossed (``lan``, ``tailnet``, or
  ``loopback``), the unaided discovery road, the TLS
  version and cipher the handshake reported, the
  fingerprint each side read out of the other's
  handshake certificate, whether announcements were
  exchanged, and how many fabric messages crossed in
  each direction. :func:`evaluate_lan_run` judges the
  legs against a ``lan_policy`` v1 — TLS only at a 1.3
  floor, plaintext never, no cloud rendezvous ever,
  discovery only by seed or mDNS — and answers a
  validated ``lan_report`` v1 whose verdict is
  ``proven`` only when every required pair stands on
  real (non-simulated) carriage with matching
  fingerprints and traffic in both directions,
  ``simulated_only`` when the same shape stands over
  stand-in carriage, and ``incomplete`` with every
  refusal counted apart by name otherwise:
  ``leg_missing``, ``tls_below_floor``,
  ``discovery_not_unaided``, ``fingerprint_mismatch``,
  ``announcement_missing``, ``leg_silent``,
  ``leg_outside_policy``, ``malformed_leg``.

Honesty: this module opens no socket and crosses no
network; its own tests ride socketpairs, say so, and
judge ``simulated_only``. The real run — three
citizens (the Pi, the laptop, this VM) on real
carriage — is deployment wiring's, recorded in
``docs/LAN_FABRIC.md`` with its measured legs named
per path; nothing here upgrades a stand-in leg into
a real one, and the mesh promises the Hailo chip
nothing beyond Project Aesir's own ledger.
"""

import base64
import copy
import json
import re
import threading
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.discovery import DiscoveryService
from yggdrasil.fabric import Fabric, validate_message
from yggdrasil.identity import fingerprint
from yggdrasil.observability import make_event
from yggdrasil.tls_channels import TlsChannel, TlsNodeChannels

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

_NODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_DISCOVERY_ROADS = ("seed", "mdns")


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


_POLICY_VALIDATOR = _validator("lan_policy.schema.json")
_LEG_VALIDATOR = _validator("lan_leg.schema.json")
_REPORT_VALIDATOR = _validator("lan_report.schema.json")


class LanError(Exception):
    """The LAN composition's own failure: a record
    that fails its schema, or a refused act. A refusal
    carries its counted reason in ``reason``."""

    def __init__(self, message: str, *, reason: str | None = None):
        super().__init__(message)
        self.reason = reason


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record, kind: str):
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise LanError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_policy(record: dict) -> dict:
    return _validate(_POLICY_VALIDATOR, record, "LAN policy")


def validate_leg(record: dict) -> dict:
    return _validate(_LEG_VALIDATOR, record, "LAN leg")


def validate_report(record: dict) -> dict:
    return _validate(_REPORT_VALIDATOR, record, "LAN report")


def default_policy(required_nodes) -> dict:
    """The LAN as data: the named citizens must all
    stand on the wire together, TLS only at a 1.3
    floor, no plaintext, no cloud rendezvous, and the
    two unaided discovery roads (seed list, mDNS).
    A policy, not a fact about a network."""
    return validate_policy(
        {
            "version": 1,
            "required_nodes": sorted(required_nodes),
            "transport": "tls",
            "minimum_tls_version": "TLSv1.3",
            "plaintext_allowed": False,
            "cloud_rendezvous": False,
            "discovery_roads": ["mdns", "seed"],
        }
    )


class LanSession:
    """One established channel between this node and
    one proven peer, after discovery: fabric messages
    cross whole and exact as records on the Y-051
    channel, and every arrival is judged by the node
    before it is delivered anywhere."""

    def __init__(self, node: "LanNode", channel: TlsChannel, *, via: str):
        self._node = node
        self.channel = channel
        self.peer_node = channel.peer_node
        self.via = via
        self.messages_sent = 0
        self.messages_received = 0
        self._closed = False

    @property
    def record(self) -> dict:
        return self.channel.record

    def send_message(self, message: dict) -> dict:
        """Carry one Y-050 fabric message to this
        session's peer. The message must be one this
        node built: a publication, or a direct message
        to this peer — anything else is refused by
        name before it reaches the wire."""
        message = validate_message(copy.deepcopy(message))
        if message["from_node"] != self._node.node_id:
            raise self._node._refuse(
                "not_own_message",
                self.peer_node,
                f"a session carries only its own node's messages; "
                f"{message['from_node']!r} is not {self._node.node_id!r}",
            )
        if message["kind"] == "direct" and message["to_node"] != self.peer_node:
            raise self._node._refuse(
                "wrong_peer",
                self.peer_node,
                f"direct message names {message['to_node']!r}; this "
                f"session's peer is {self.peer_node!r}",
            )
        self.channel.send_json({"kind": "fabric", "message": message})
        self.messages_sent += 1
        return message

    def recv_message(self) -> dict | None:
        """Take the next record off the channel: a
        fabric message is judged and delivered by the
        node and returned; the peer's closing record
        answers ``None``. Anything else is refused by
        name."""
        record = self.channel.recv_json()
        if not isinstance(record, dict) or "kind" not in record:
            raise self._node._refuse(
                "malformed_record",
                self.peer_node,
                "a LAN record must be an object naming its kind",
            )
        if record["kind"] == "bye":
            self._closed = True
            return None
        if record["kind"] != "fabric" or not isinstance(record.get("message"), dict):
            raise self._node._refuse(
                "malformed_record",
                self.peer_node,
                f"unknown LAN record kind {record['kind']!r}",
            )
        message = self._node._deliver_wire(self, record["message"])
        self.messages_received += 1
        return message

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self.channel.send_json({"kind": "bye"})
            except Exception:  # noqa: BLE001 — the peer may already be gone; a failed farewell on a closing channel is not a new failure
                self.channel.close()
                return
            self.channel.close()


class LanNode:
    """One citizen standing on the LAN: a Y-051 TLS
    door, a Y-052 discovery service, and a Y-050
    fabric, composed — never rewired. ``peer_certs``
    (node_id → Y-011 certificate) is the trust the
    operator provisioned: the door will shake hands
    only with those named peers, and discovery admits
    announcements only under the same root.

    Sockets are the caller's: :meth:`open_client` and
    :meth:`open_server` take one connected socket
    each, the way the TLS door always has.
    """

    def __init__(
        self,
        *,
        node_id: str,
        channels: TlsNodeChannels,
        discovery: DiscoveryService,
        fabric: Fabric,
        peer_certs: dict,
        now=None,
    ):
        if not _NODE_RE.match(node_id or ""):
            raise LanError(f"node id {node_id!r} is not a node name")
        if channels.node_id != node_id or discovery.node_id != node_id:
            raise LanError(
                "the composed pieces disagree about which node this is: "
                f"channels say {channels.node_id!r}, discovery says "
                f"{discovery.node_id!r}, the node claims {node_id!r}"
            )
        self.node_id = node_id
        self._channels = channels
        self._discovery = discovery
        self._fabric = fabric
        self._peer_certs = copy.deepcopy(peer_certs)
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._fabric.attach(node_id)
        self._wire_inbox: list[dict] = []
        self._handlers: dict[str, list] = {}
        self._sessions = 0
        self._sent = 0
        self._received = 0
        self._refusals = 0
        self._refusals_by_reason: dict[str, int] = {}
        self._events: list[dict] = []
        self._event_seq = 0

    # -- the composed pieces, read-only doors ----------

    @property
    def channels(self) -> TlsNodeChannels:
        return self._channels

    @property
    def discovery(self) -> DiscoveryService:
        return self._discovery

    @property
    def fabric(self) -> Fabric:
        return self._fabric

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    def _emit(self, type_: str, payload: dict) -> None:
        self._event_seq += 1
        self._events.append(
            make_event(
                type=type_,
                node_id=self.node_id,
                component="fabric",
                seq=self._event_seq,
                at=self._now(),
                payload=payload,
            )
        )

    def _refuse(self, reason: str, peer: str | None, detail: str) -> LanError:
        with self._lock:
            self._refusals += 1
            self._refusals_by_reason[reason] = (
                self._refusals_by_reason.get(reason, 0) + 1
            )
            self._emit(
                "fabric.lan_refused",
                {"node": self.node_id, "peer": peer, "reason": reason},
            )
        return LanError(detail, reason=reason)

    # -- sessions ----------------------------------------

    def _check_via(self, via: str) -> str:
        if via not in _DISCOVERY_ROADS:
            raise LanError(
                f"unknown discovery road {via!r}: discovery is unaided — "
                "the roads are the seed list and mDNS, never a cloud"
            )
        return via

    def _hear_announcement(self, session: LanSession, announcement, via: str) -> None:
        if not isinstance(announcement, dict):
            raise self._refuse(
                "announcement_mismatch",
                session.peer_node,
                "the peer opened its session with no announcement",
            )
        if announcement.get("node_id") != session.peer_node:
            raise self._refuse(
                "announcement_mismatch",
                session.peer_node,
                f"the channel proved {session.peer_node!r} but the "
                f"announcement names {announcement.get('node_id')!r}",
            )
        entry = self._discovery.hear(announcement, via=via)
        if entry is None:
            raise self._refuse(
                "announcement_refused",
                session.peer_node,
                f"discovery refused {session.peer_node!r}'s announcement",
            )

    def _established(self, session: LanSession) -> LanSession:
        with self._lock:
            self._sessions += 1
            self._emit(
                "fabric.lan_session_established",
                {
                    "node": self.node_id,
                    "peer": session.peer_node,
                    "tls_version": session.record["tls_version"],
                    "cipher": session.record["cipher"],
                    "via": session.via,
                },
            )
        return session

    def open_client(self, raw_socket, peer_node_id: str, *, via: str = "seed") -> LanSession:
        """Open a session to one named peer over a
        connected socket the caller dialled: mutual
        TLS through the door, then this node announces
        first and hears the peer's answer."""
        via = self._check_via(via)
        cert = self._peer_certs.get(peer_node_id)
        if cert is None:
            raise self._refuse(
                "unknown_peer",
                peer_node_id,
                f"no provisioned certificate for {peer_node_id!r}; "
                "this node was not told to trust it by name",
            )
        channel = self._channels.wrap_client(raw_socket, peer_cert=cert)
        session = LanSession(self, channel, via=via)
        try:
            channel.send_json(
                {"kind": "announce", "announcement": self._discovery.announce()}
            )
            record = channel.recv_json()
            if not isinstance(record, dict) or record.get("kind") != "announce":
                raise self._refuse(
                    "announcement_mismatch",
                    peer_node_id,
                    "the peer answered the session opening with no announcement",
                )
            self._hear_announcement(session, record.get("announcement"), via)
        except Exception:
            channel.close()
            raise
        return self._established(session)

    def open_server(self, raw_socket, *, via: str = "seed") -> LanSession:
        """Accept a session from any provisioned peer
        over a connected socket the caller accepted:
        mutual TLS through the door, then the peer
        announces first and this node answers."""
        via = self._check_via(via)
        channel = self._channels.wrap_server(raw_socket, peer_certs=self._peer_certs)
        session = LanSession(self, channel, via=via)
        try:
            record = channel.recv_json()
            if not isinstance(record, dict) or record.get("kind") != "announce":
                raise self._refuse(
                    "announcement_mismatch",
                    session.peer_node,
                    "the peer opened its session with no announcement",
                )
            self._hear_announcement(session, record.get("announcement"), via)
            channel.send_json(
                {"kind": "announce", "announcement": self._discovery.announce()}
            )
        except Exception:
            channel.close()
            raise
        return self._established(session)

    # -- traffic ------------------------------------------

    def publish(self, topic: str, payload) -> dict:
        """Build one publication with the node's own
        fabric (Y-050 builds it, stamps it, and returns
        it); carry it on sessions with
        :meth:`LanSession.send_message`."""
        return self._fabric.publish(self.node_id, topic, payload)

    def direct(self, to_node: str, payload) -> dict:
        """Build one direct message with the node's
        own fabric; carry it on the session to that
        peer."""
        return self._fabric.send(self.node_id, to_node, payload)

    def subscribe_wire(self, topic: str, handler=None) -> None:
        """Receive wire publications on a topic: the
        handler (if any) is called with a copy of each
        message delivered from a session, and every
        delivered message is also kept in the wire
        inbox."""
        with self._lock:
            self._handlers.setdefault(topic, [])
            if handler is not None:
                self._handlers[topic].append(handler)

    def wire_inbox(self) -> list[dict]:
        """Drain the wire inbox: every message a
        session delivered to this node, in order."""
        with self._lock:
            drained, self._wire_inbox = self._wire_inbox, []
            return drained

    def _deliver_wire(self, session: LanSession, message: dict) -> dict:
        try:
            message = validate_message(copy.deepcopy(message))
        except Exception as exc:
            raise self._refuse(
                "malformed_message", session.peer_node, f"wire message: {exc}"
            ) from exc
        if message["from_node"] != session.peer_node:
            raise self._refuse(
                "sender_mismatch",
                session.peer_node,
                f"the channel proved {session.peer_node!r} but the "
                f"message names {message['from_node']!r} as its sender",
            )
        if message["kind"] == "direct" and message["to_node"] != self.node_id:
            raise self._refuse(
                "not_for_me",
                session.peer_node,
                f"direct message names {message['to_node']!r}; "
                f"this node is {self.node_id!r}",
            )
        with self._lock:
            self._received += 1
            self._wire_inbox.append(message)
            handlers = list(self._handlers.get(message["topic"] or "", []))
        for handler in handlers:
            handler(copy.deepcopy(message))
        return message

    # -- the node's own counted standing -------------------

    def report(self) -> dict:
        """This node's LAN standing, as plain counts:
        sessions established, messages built for the
        wire and delivered from it, refusals apart by
        name, and the peers discovery currently holds.
        (The run as a whole is judged by
        :func:`evaluate_lan_run`.)"""
        with self._lock:
            return {
                "version": 1,
                "node": self.node_id,
                "sessions_established": self._sessions,
                "messages_received": self._received,
                "refusals": self._refusals,
                "refusals_by_reason": dict(self._refusals_by_reason),
                "peers_discovered": sorted(
                    peer["node_id"] for peer in self._discovery.peers()
                ),
            }


def evaluate_lan_run(
    policy: dict,
    *,
    expected_fingerprints: dict,
    legs,
    now=None,
) -> dict:
    """Judge one LAN run's observed legs against its
    policy and the enrolled Y-011 fingerprints.

    Every required pair of nodes must show one leg on
    real carriage: TLS 1.3 as the handshake reported
    it, each side's observed fingerprint equal to the
    fingerprint of the other's enrolled certificate,
    discovery by an unaided road the policy allows,
    announcements exchanged, and fabric messages
    carried in *both* directions. Each failure is a
    counted refusal by name; a leg that fails its own
    schema is ``malformed_leg`` and proves nothing.
    The verdict is ``proven`` only when nothing was
    refused and no leg was simulated, ``simulated_only``
    when the full shape stands over stand-in carriage,
    and ``incomplete`` otherwise. This function judges
    records; it manufactures none — a pair with no leg
    is ``leg_missing``, never quietly complete.
    """
    policy = validate_policy(copy.deepcopy(policy))
    moment = (now or (lambda: datetime.now(UTC)))()
    required = list(policy["required_nodes"])
    roads = set(policy["discovery_roads"])
    refusals_by_reason: dict[str, int] = {}

    def refuse(reason: str) -> None:
        refusals_by_reason[reason] = refusals_by_reason.get(reason, 0) + 1

    valid_legs = []
    for leg in legs:
        try:
            valid_legs.append(validate_leg(copy.deepcopy(leg)))
        except LanError:
            refuse("malformed_leg")

    pairs = list(combinations(sorted(required), 2))
    proven = 0
    messages_carried = 0
    any_simulated = False
    for leg in valid_legs:
        messages_carried += leg["messages_a_to_b"] + leg["messages_b_to_a"]
        any_simulated = any_simulated or leg["simulated"]
    for a_node, b_node in pairs:
        leg = next(
            (
                item
                for item in valid_legs
                if {item["node_a"], item["node_b"]} == {a_node, b_node}
            ),
            None,
        )
        if leg is None:
            refuse("leg_missing")
            continue
        ok = True
        if leg["tls_version"] != policy["minimum_tls_version"]:
            refuse("tls_below_floor")
            ok = False
        if leg["discovery_via"] not in roads:
            refuse("discovery_not_unaided")
            ok = False
        a_expected = expected_fingerprints.get(leg["node_a"])
        b_expected = expected_fingerprints.get(leg["node_b"])
        if (
            leg["b_saw_a_fingerprint"] != a_expected
            or leg["a_saw_b_fingerprint"] != b_expected
        ):
            refuse("fingerprint_mismatch")
            ok = False
        if not leg["announcements_exchanged"]:
            refuse("announcement_missing")
            ok = False
        forward = (
            leg["messages_a_to_b"] if leg["node_a"] == a_node else leg["messages_b_to_a"]
        )
        backward = (
            leg["messages_b_to_a"] if leg["node_a"] == a_node else leg["messages_a_to_b"]
        )
        if forward <= 0 or backward <= 0:
            refuse("leg_silent")
            ok = False
        if ok:
            proven += 1
    for leg in valid_legs:
        if leg["node_a"] not in required or leg["node_b"] not in required:
            refuse("leg_outside_policy")

    refusals = sum(refusals_by_reason.values())
    if refusals:
        verdict = "incomplete"
    elif any_simulated:
        verdict = "simulated_only"
    else:
        verdict = "proven"
    return validate_report(
        {
            "version": 1,
            "at": _fmt(moment),
            "policy": policy,
            "required_nodes": required,
            "pairs_expected": len(pairs),
            "pairs_proven": proven,
            "legs": len(valid_legs),
            "messages_carried": messages_carried,
            "simulated": any_simulated,
            "verdict": verdict,
            "refusals": refusals,
            "refusals_by_reason": refusals_by_reason,
        }
    )


def expected_fingerprints_from_certs(certs: dict) -> dict:
    """The judge's expectation, read out of the
    enrolled Y-011 certificates themselves: node_id →
    the fingerprint of that node's certificate key."""
    expected = {}
    for node_id, cert in certs.items():
        public_key = base64.b64decode(cert["public_key"].encode("ascii"))
        expected[node_id] = fingerprint(public_key)
    return expected
