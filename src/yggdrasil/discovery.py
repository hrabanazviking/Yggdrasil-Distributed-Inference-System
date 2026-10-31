"""Discovery — Y-052.

Nodes find each other on a LAN, with no cloud rendezvous
anywhere in the design: the policy schema itself carries
``cloud_rendezvous: false`` and ``require_certificate:
true`` as constants, so a policy that opened either door
fails validation rather than merely being discouraged.

Two roads in, one door of trust:

- **mDNS-shaped announcements.** A node announces itself:
  a validated ``discovery_announcement`` v1 naming the
  node, the TLS endpoint it may be reached at, and —
  carried whole — its Y-011 node certificate. Peers on
  the LAN hear announcements and answer queries with
  fresh ones.
- **A seed list.** The operator may name seed endpoints
  (host + port) — machines likely to be up, learned out
  of band. A seed is dialled directly and asked for its
  announcement; the answer walks through exactly the
  same door as an announcement heard off the wire.

The door: an announcement becomes a ``discovery_peer``
v1 entry only when its certificate **verifies against
the mesh root** (Y-011) *and* agrees with the
announcement's own identity fields — node id, device
class, agent version, and the key fingerprint. A foreign
mesh's node, an expired certificate, a certificate that
does not match the name it was announced under: refused
by name, counted in the report's ``refused_reasons``,
and recorded as a ``discovery.announcement_refused``
event. Discovery never yields a plaintext endpoint —
an announcement's transport is the schema constant
``tls`` (Y-051's channels are how a found peer is
actually spoken to).

Freshness is measured on the **hearing node's own
clock**: an entry is born when an announcement is heard,
refreshed by a repeat or newer announcement (sequences
rise per announcing node; an older sequence than the one
on record is refused ``stale_sequence``, a repeat of the
same sequence refreshes), and expires
``announcement_ttl_seconds`` after it was last heard —
a peer that goes quiet ages out of the table and its
expiry is an event, not a silence. (Why our clock, not
the announcer's: clock discipline is Y-068's subject,
and no node's temporal logic should trust a peer's
clock.) The table is bounded by the policy's
``max_peers``: expired entries are evicted first, and a
new peer heard while the table is full of fresh peers
is refused ``peer_table_full`` — named, never silently
dropped and never evicting a fresh peer to make room.

Honesty: the LAN here is :class:`SimulatedLan`, an
in-process model — announcements carry ``simulated:
true`` as a schema constant, no multicast packet is
sent, no socket is opened, and no real LAN is crossed.
What is proven is the discovery *rules*: announcement,
query/response, seed dialling, certificate-gated
admission, refresh, expiry, and the counted refusals.
Real mDNS carriage on the home LAN — where a deployment
transport behind this same service shape sends and
receives the same announcements — is Y-062, and its
hardware proof is Part VIII's. The registry service
that registers census + models with the peers found
here is Y-053; heartbeats and liveness ageing are
Y-054.
"""

import base64
import copy
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.identity import (
    IdentityError,
    certificate_identity,
    fingerprint,
    verify_certificate,
)
from yggdrasil.observability import make_event

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

_VIA_ROADS = ("mdns", "seed")


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


_POLICY_VALIDATOR = _validator("discovery_policy.schema.json")
_ANNOUNCEMENT_VALIDATOR = _validator("discovery_announcement.schema.json")
_PEER_VALIDATOR = _validator("discovery_peer.schema.json")
_REPORT_VALIDATOR = _validator("discovery_report.schema.json")


class DiscoveryError(Exception):
    """Discovery's own failure: a policy, announcement, peer
    entry, or report that fails its schema, or a use of the
    API that would manufacture a peer — a local node whose
    own certificate does not verify, a seed list entry that
    is not an endpoint, a LAN attach that would speak for
    two nodes at once."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise DiscoveryError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_policy(record: dict) -> dict:
    return _validate(_POLICY_VALIDATOR, record, "discovery policy")


def validate_announcement(record: dict) -> dict:
    return _validate(_ANNOUNCEMENT_VALIDATOR, record, "discovery announcement")


def validate_peer(record: dict) -> dict:
    return _validate(_PEER_VALIDATOR, record, "discovery peer")


def validate_report(record: dict) -> dict:
    return _validate(_REPORT_VALIDATOR, record, "discovery report")


def default_policy() -> dict:
    """Discovery's bounds as data: a heard announcement
    stays fresh for 90 seconds and a peer table holds at
    most 256 peers. A policy, labelled as one — an
    operator's numbers, not facts about a LAN. The two
    constants are not numbers at all: a certificate is
    always required, and there is never a cloud
    rendezvous."""
    return validate_policy(
        {
            "version": 1,
            "announcement_ttl_seconds": 90,
            "max_peers": 256,
            "require_certificate": True,
            "cloud_rendezvous": False,
        }
    )


def build_announcement(
    cert: dict,
    *,
    host: str,
    port: int,
    seq: int,
    at: datetime | None = None,
) -> dict:
    """One node's announcement, derived from its Y-011 node
    certificate: the identity fields and the fingerprint
    are read out of the certificate, never restated by the
    caller, so an announcement cannot name one node and
    carry another's key. The certificate rides whole — it
    is the hearer's evidence, checked at the door."""
    try:
        identity = certificate_identity(cert)
    except IdentityError as exc:
        raise DiscoveryError(f"cannot announce from this certificate: {exc}") from exc
    public_key = base64.b64decode(cert["public_key"].encode("ascii"), validate=True)
    record = {
        "version": 1,
        "node_id": identity["node_id"],
        "fingerprint": fingerprint(public_key),
        "device_class": identity["device_class"],
        "agent_version": identity["agent_version"],
        "certificate": copy.deepcopy(cert),
        "host": host,
        "port": port,
        "transport": "tls",
        "seq": seq,
        "announced_at": _fmt(at or datetime.now(UTC)),
        "simulated": True,
    }
    return validate_announcement(record)


class DiscoveryService:
    """One node's discovery: it announces itself, hears its
    peers' announcements, and keeps the peer table.

    ``cert`` is the local node's Y-011 node certificate and
    ``root_cert`` the mesh root it must verify against —
    the service refuses to exist for a node whose own
    certificate does not verify. ``now`` is injectable so
    freshness is testable and replayable. Events are
    Y-013 envelopes from the ``fabric`` component, kept on
    ``events`` and also handed to ``event_sink`` when one
    is given. All state changes are lock-guarded.
    """

    def __init__(
        self,
        *,
        cert: dict,
        root_cert: dict,
        host: str,
        port: int,
        policy: dict | None = None,
        now=None,
        event_sink=None,
    ):
        self._now = now or (lambda: datetime.now(UTC))
        try:
            verify_certificate(cert, root_cert, now=self._now())
            identity = certificate_identity(cert)
        except IdentityError as exc:
            raise DiscoveryError(
                f"local node may not announce: {exc}"
            ) from exc
        self._cert = copy.deepcopy(cert)
        self._root_cert = copy.deepcopy(root_cert)
        self._node_id = identity["node_id"]
        self._host = host
        self._port = port
        self._policy = validate_policy(
            dict(policy) if policy is not None else default_policy()
        )
        self._event_sink = event_sink
        self._lock = threading.RLock()
        self._peers: dict[str, dict] = {}
        self._seq = 0
        self._events: list[dict] = []
        self._event_seq = 0
        self._heard = 0
        self._accepted = 0
        self._refused = 0
        self._refused_reasons: dict[str, int] = {}
        self._expired = 0
        self._mdns_queries = 0
        self._seed_queries = 0
        self._seed_unreachable = 0

    # -- the local node ------------------------------------

    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def endpoint(self) -> tuple[str, int]:
        return (self._host, self._port)

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    def _emit(self, type_: str, payload: dict) -> None:
        self._event_seq += 1
        event = make_event(
            type=type_,
            node_id=self._node_id,
            component="fabric",
            seq=self._event_seq,
            at=self._now(),
            payload=payload,
        )
        self._events.append(event)
        if self._event_sink is not None:
            self._event_sink(event)

    def announce(self) -> dict:
        """This node's next announcement: its sequence rises
        by one on every call, as an mDNS responder's would
        across a long evening of answering."""
        with self._lock:
            self._seq += 1
            return build_announcement(
                self._cert,
                host=self._host,
                port=self._port,
                seq=self._seq,
                at=self._now(),
            )

    # -- hearing -------------------------------------------

    def _refuse(self, reason: str, peer_node_id, via: str) -> None:
        self._refused += 1
        self._refused_reasons[reason] = self._refused_reasons.get(reason, 0) + 1
        self._emit(
            "discovery.announcement_refused",
            {"peer_node_id": peer_node_id, "reason": reason, "via": via},
        )

    def hear(self, announcement: dict, *, via: str = "mdns") -> dict | None:
        """Hear one announcement. Returns the peer-table
        entry it produced or refreshed, or ``None`` when
        the announcement was refused — every refusal named
        and counted, nothing silently dropped and nothing
        admitted on a claim alone."""
        if via not in _VIA_ROADS:
            raise DiscoveryError(f"unknown discovery road: {via}")
        with self._lock:
            self._heard += 1
            heard_at = self._now()
            try:
                record = validate_announcement(copy.deepcopy(announcement))
            except DiscoveryError:
                return self._refuse("malformed_announcement", None, via)
            peer_node_id = record["node_id"]
            if peer_node_id == self._node_id:
                return self._refuse("own_announcement", peer_node_id, via)
            cert = record["certificate"]
            try:
                verify_certificate(cert, self._root_cert, now=heard_at)
                identity = certificate_identity(cert)
            except IdentityError:
                return self._refuse("certificate_invalid", peer_node_id, via)
            public_key = base64.b64decode(
                cert["public_key"].encode("ascii"), validate=True
            )
            if (
                identity["node_id"] != record["node_id"]
                or identity["device_class"] != record["device_class"]
                or identity["agent_version"] != record["agent_version"]
                or fingerprint(public_key) != record["fingerprint"]
            ):
                return self._refuse("identity_mismatch", peer_node_id, via)

            self._expire_locked(heard_at)
            existing = self._peers.get(peer_node_id)
            if existing is not None and record["seq"] < existing["seq"]:
                return self._refuse("stale_sequence", peer_node_id, via)
            if existing is None and len(self._peers) >= self._policy["max_peers"]:
                return self._refuse("peer_table_full", peer_node_id, via)

            ttl = timedelta(seconds=self._policy["announcement_ttl_seconds"])
            if existing is None:
                entry = {
                    "version": 1,
                    "node_id": peer_node_id,
                    "fingerprint": record["fingerprint"],
                    "device_class": record["device_class"],
                    "host": record["host"],
                    "port": record["port"],
                    "transport": "tls",
                    "sources": [via],
                    "seq": record["seq"],
                    "first_seen_at": _fmt(heard_at),
                    "last_seen_at": _fmt(heard_at),
                    "expires_at": _fmt(heard_at + ttl),
                    "simulated": True,
                }
            else:
                entry = copy.deepcopy(existing)
                entry["fingerprint"] = record["fingerprint"]
                entry["device_class"] = record["device_class"]
                entry["host"] = record["host"]
                entry["port"] = record["port"]
                entry["seq"] = record["seq"]
                entry["last_seen_at"] = _fmt(heard_at)
                entry["expires_at"] = _fmt(heard_at + ttl)
                if via not in entry["sources"]:
                    entry["sources"] = sorted([*entry["sources"], via])
            entry = validate_peer(entry)
            self._peers[peer_node_id] = entry
            self._accepted += 1
            if existing is None:
                self._emit(
                    "discovery.peer_found",
                    {"peer_node_id": peer_node_id, "via": via},
                )
            return copy.deepcopy(entry)

    # -- freshness -------------------------------------------

    def _expire_locked(self, moment: datetime) -> int:
        dropped = 0
        for node_id, entry in list(self._peers.items()):
            if moment >= _parse(entry["expires_at"]):
                del self._peers[node_id]
                self._expired += 1
                dropped += 1
                self._emit(
                    "discovery.peer_expired", {"peer_node_id": node_id}
                )
        return dropped

    def expire(self) -> int:
        """Drop every entry whose freshness window has
        closed on this node's clock; returns how many."""
        with self._lock:
            return self._expire_locked(self._now())

    def peers(self) -> list[dict]:
        """The peer table as it stands — fresh entries only
        (expiry runs first), sorted by node id."""
        with self._lock:
            self._expire_locked(self._now())
            return [copy.deepcopy(self._peers[k]) for k in sorted(self._peers)]

    def peer(self, node_id: str) -> dict | None:
        """One named peer's fresh entry, or ``None`` — named
        and absent is an honest answer, never an error."""
        for entry in self.peers():
            if entry["node_id"] == node_id:
                return entry
        return None

    # -- the two roads in ------------------------------------

    def discover_on_lan(self, lan: "SimulatedLan") -> list[dict]:
        """Ask the LAN who is here: every other attached
        node answers with a fresh announcement, heard via
        the mDNS road. Returns the entries heard."""
        with self._lock:
            self._mdns_queries += 1
        heard = []
        for announcement in lan.announcements_except(self._node_id):
            entry = self.hear(announcement, via="mdns")
            if entry is not None:
                heard.append(entry)
        return heard

    def discover_via_seeds(self, seeds, fetch) -> list[dict]:
        """Walk the operator's seed list: ``fetch(host,
        port)`` — deployment wiring that dials the endpoint
        (over Y-051's TLS, on a real LAN) — returns the
        seed's announcement or raises / returns ``None``
        when the seed cannot be reached. An unreachable
        seed is counted and recorded as an event; a reached
        seed's announcement walks through the same
        certificate door as any other. Returns the entries
        heard."""
        heard = []
        for seed in seeds:
            if (
                not isinstance(seed, dict)
                or not isinstance(seed.get("host"), str)
                or not isinstance(seed.get("port"), int)
            ):
                raise DiscoveryError(
                    f"seed list entry is not an endpoint: {seed!r}"
                )
            with self._lock:
                self._seed_queries += 1
            try:
                announcement = fetch(seed["host"], seed["port"])
            except Exception:  # noqa: BLE001 — any failure of the dial is the seed being unreachable; the failure itself is the data, counted below
                announcement = None
            if announcement is None:
                with self._lock:
                    self._seed_unreachable += 1
                    self._emit(
                        "discovery.seed_unreachable",
                        {"host": seed["host"], "port": seed["port"]},
                    )
                continue
            entry = self.hear(announcement, via="seed")
            if entry is not None:
                heard.append(entry)
        return heard

    # -- the counted state -----------------------------------

    def report(self) -> dict:
        """Discovery's counted state as a validated
        ``discovery_report`` v1: who is known, what was
        heard, accepted, refused and why, what expired,
        and the queries made — names and counts only."""
        with self._lock:
            known = self.peers()
            record = {
                "version": 1,
                "at": _fmt(self._now()),
                "local_node_id": self._node_id,
                "simulated": True,
                "known_peers": known,
                "announcements_heard": self._heard,
                "announcements_accepted": self._accepted,
                "announcements_refused": self._refused,
                "refused_reasons": dict(sorted(self._refused_reasons.items())),
                "peers_expired": self._expired,
                "mdns_queries": self._mdns_queries,
                "seed_queries": self._seed_queries,
                "seed_unreachable": self._seed_unreachable,
            }
            return validate_report(record)


class SimulatedLan:
    """The LAN as an in-process model: nodes attach under
    their node id, and an attached node asked for its
    neighbours receives a fresh announcement from every
    other attached node, exactly as mDNS query/response
    would deliver them. No multicast packet, no socket —
    the announcements carried are the real validated
    records, and the hearing node's certificate door is
    the real Y-011 verification; only the wire is a
    model, and the records say so (``simulated: true``).
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._services: dict[str, DiscoveryService] = {}

    def attach(self, service: DiscoveryService) -> None:
        with self._lock:
            if service.node_id in self._services:
                raise DiscoveryError(
                    f"node already on this LAN: {service.node_id}"
                )
            self._services[service.node_id] = service

    def detach(self, node_id: str) -> None:
        with self._lock:
            self._services.pop(node_id, None)

    def node_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._services)

    def broadcast(self, from_node: str) -> dict:
        """One attached node announces itself to the whole
        LAN: every other attached node hears the
        announcement via the mDNS road. Returns the
        announcement made."""
        with self._lock:
            service = self._services.get(from_node)
            if service is None:
                raise DiscoveryError(f"node not on this LAN: {from_node}")
            announcement = service.announce()
            others = [s for n, s in self._services.items() if n != from_node]
        for other in others:
            other.hear(announcement, via="mdns")
        return announcement

    def announcements_except(self, node_id: str) -> list[dict]:
        """Fresh announcements from every attached node but
        the named one — the answers to its query."""
        with self._lock:
            return [
                service.announce()
                for other_id, service in sorted(self._services.items())
                if other_id != node_id
            ]

    def announcement_at(self, host: str, port: int) -> dict | None:
        """The seed road's reach: a fresh announcement from
        whichever attached node sits at this endpoint, or
        ``None`` when nothing does — a dead seed is an
        honest ``None``, never a manufactured peer."""
        with self._lock:
            for service in self._services.values():
                if service.endpoint == (host, port):
                    return service.announce()
        return None
