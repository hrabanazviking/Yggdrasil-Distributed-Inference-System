"""The fabric on the real LAN tests — Y-062's acceptance.

The slice must prove: (a) the discipline is data —
TLS only at a 1.3 floor, plaintext never, no cloud
rendezvous ever, discovery only by the unaided
roads, and a weaker policy fails validation;
(b) two composed nodes establish a session over a
real TLS channel — mutual TLS 1.3 through the Y-051
door, announcements exchanged on the channel and
admitted through Y-052's one door of trust, so each
node's discovery table holds the other; (c) Y-050
fabric messages cross the session whole and exact —
a publication reaches the peer's wire inbox and its
topic handlers, a direct message reaches only its
named node, in both directions; (d) reception is
judged — a message naming a sender the channel did
not prove is refused ``sender_mismatch``, a direct
message for another node is refused ``not_for_me``,
a session opening whose announcement names anyone
but the proven peer is refused
``announcement_mismatch``, and a client session to
an unprovisioned peer is refused ``unknown_peer``
before any dial; (e) a whole run is judged as a
whole — ``proven`` only over real carriage with
matching fingerprints and two-way traffic,
``simulated_only`` over stand-in carriage, and
``incomplete`` with each refusal counted by name
(missing leg, TLS below the floor, a cloud or
unaided-discovery failure, a fingerprint mismatch,
a missing announcement exchange, a silent leg, a
malformed leg); (f) legs, reports, and events
validate, and the founding README is untouched.

Honesty: these tests ride socketpairs — no LAN is
crossed, no machine but this one is touched, and
every leg they judge is ``simulated: true``. The
real run over real carriage is deployment wiring's
(``deploy/lan_node.py``), recorded in
``docs/LAN_FABRIC.md``.
"""

import hashlib
import socket
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.discovery import DiscoveryService
from yggdrasil.fabric import Fabric
from yggdrasil.identity import NodeKeypair, create_mesh_root, issue_node_certificate
from yggdrasil.lan_fabric import (
    LanError,
    LanNode,
    default_policy,
    evaluate_lan_run,
    expected_fingerprints_from_certs,
    validate_leg,
    validate_policy,
    validate_report,
)
from yggdrasil.observability import validate_event
from yggdrasil.tls_channels import TlsNodeChannels

ROOT = Path(__file__).resolve().parents[1]
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
NODES = ("node-a", "node-b", "node-c")


class Mesh:
    """A three-node mesh, provisioned the bootstrap way."""

    def __init__(self, tmp_path: Path):
        self.root_keypair, self.root_cert = create_mesh_root(now=NOW)
        self.certs = {}
        self.keypairs = {}
        for node_id in NODES:
            keypair = NodeKeypair.generate()
            cert = issue_node_certificate(
                self.root_keypair,
                self.root_cert,
                node_id=node_id,
                node_keypair=keypair,
                device_class="vm",
                agent_version="0.0.1",
                now=NOW,
            )
            self.keypairs[node_id] = keypair
            self.certs[node_id] = cert
        self.tmp = tmp_path

    def lan_node(self, node_id: str, *, port: int = 9100) -> LanNode:
        channels = TlsNodeChannels.derive(
            root_keypair=self.root_keypair,
            node_id=node_id,
            keypair=self.keypairs[node_id],
            node_cert=self.certs[node_id],
            root_cert=self.root_cert,
            material_dir=self.tmp / node_id,
            now=lambda: NOW,
        )
        discovery = DiscoveryService(
            cert=self.certs[node_id],
            root_cert=self.root_cert,
            host="192.0.2.1",
            port=port,
            now=lambda: NOW,
        )
        fabric = Fabric(now=lambda: NOW)
        peer_certs = {
            peer: cert for peer, cert in self.certs.items() if peer != node_id
        }
        return LanNode(
            node_id=node_id,
            channels=channels,
            discovery=discovery,
            fabric=fabric,
            peer_certs=peer_certs,
            now=lambda: NOW,
        )


@pytest.fixture()
def mesh(tmp_path):
    return Mesh(tmp_path)


def open_pair(client_node: LanNode, server_node: LanNode, *, via="seed"):
    """Run one client/server session over a socketpair.

    Returns (client_session, server_session)."""
    left, right = socket.socketpair()
    results = {}

    def serve():
        try:
            results["server"] = server_node.open_server(left, via=via)
        except Exception as exc:  # noqa: BLE001 — the test asserts on it
            results["server"] = exc

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        results["client"] = client_node.open_client(
            right, server_node.node_id, via=via
        )
    except Exception as exc:  # noqa: BLE001 — the test asserts on it
        results["client"] = exc
    thread.join(timeout=30)
    if thread.is_alive():
        for sock in (left, right):
            try:
                sock.close()
            except OSError:
                pass
        thread.join(timeout=30)
    assert not thread.is_alive()
    return results["client"], results["server"]


def real_leg(mesh, a="node-a", b="node-b", **overrides):
    expected = expected_fingerprints_from_certs(mesh.certs)
    leg = {
        "version": 1,
        "node_a": a,
        "node_b": b,
        "path": "lan",
        "discovery_via": "seed",
        "tls_version": "TLSv1.3",
        "cipher": "TLS_AES_256_GCM_SHA384",
        "a_saw_b_fingerprint": expected[b],
        "b_saw_a_fingerprint": expected[a],
        "announcements_exchanged": True,
        "messages_a_to_b": 3,
        "messages_b_to_a": 2,
        "simulated": False,
        "established_at": "2026-11-02T12:00:00Z",
    }
    leg.update(overrides)
    return leg


def full_legs(mesh, **overrides):
    return [
        real_leg(mesh, "node-a", "node-b", **overrides),
        real_leg(mesh, "node-a", "node-c", **overrides),
        real_leg(mesh, "node-b", "node-c", **overrides),
    ]


# --- (a) the discipline is data ------------------------------------------


def test_policy_is_tls_only_unaided_and_floored(mesh):
    policy = default_policy(["node-c", "node-a", "node-b"])
    assert policy["required_nodes"] == ["node-a", "node-b", "node-c"]
    assert policy["transport"] == "tls"
    assert policy["minimum_tls_version"] == "TLSv1.3"
    assert policy["plaintext_allowed"] is False
    assert policy["cloud_rendezvous"] is False
    assert sorted(policy["discovery_roads"]) == ["mdns", "seed"]
    for broken in (
        {"plaintext_allowed": True},
        {"cloud_rendezvous": True},
        {"minimum_tls_version": "TLSv1.2"},
        {"transport": "plaintext"},
        {"discovery_roads": ["cloud"]},
        {"required_nodes": ["node-a"]},
    ):
        with pytest.raises(LanError):
            validate_policy({**policy, **broken})


def test_leg_and_report_validators(mesh):
    assert validate_leg(real_leg(mesh))["path"] == "lan"
    with pytest.raises(LanError):
        validate_leg(real_leg(mesh, path="carrier-pigeon"))
    with pytest.raises(LanError):
        validate_leg(real_leg(mesh, a_saw_b_fingerprint="not-a-fingerprint"))


# --- (b) a session stands on the real TLS door ----------------------------


def test_session_exchanges_announcements_through_discovery(mesh):
    node_a = mesh.lan_node("node-a", port=9101)
    node_b = mesh.lan_node("node-b", port=9102)
    client, server = open_pair(node_a, node_b)
    assert not isinstance(client, Exception), client
    assert not isinstance(server, Exception), server
    assert client.peer_node == "node-b"
    assert server.peer_node == "node-a"
    assert client.record["tls_version"] == "TLSv1.3"
    assert server.record["tls_version"] == "TLSv1.3"
    # Discovery admitted each peer through its one door of trust.
    assert [p["node_id"] for p in node_a.discovery.peers()] == ["node-b"]
    assert [p["node_id"] for p in node_b.discovery.peers()] == ["node-a"]
    assert node_a.report()["sessions_established"] == 1
    assert node_b.report()["peers_discovered"] == ["node-a"]
    for event in node_a.events + node_b.events:
        validate_event(event)
    client.close()
    server.close()


def test_session_to_unprovisioned_peer_refused_before_dial(mesh):
    node_a = mesh.lan_node("node-a", port=9101)
    left, _right = socket.socketpair()
    with pytest.raises(LanError) as caught:
        node_a.open_client(left, "node-stranger")
    assert caught.value.reason == "unknown_peer"
    left.close()


def test_announcement_naming_another_node_refuses_the_session(mesh):
    node_a = mesh.lan_node("node-a", port=9101)
    node_b = mesh.lan_node("node-b", port=9102)
    left, right = socket.socketpair()
    results = {}

    def serve():
        try:
            results["server"] = node_b.open_server(left)
        except Exception as exc:  # noqa: BLE001
            results["server"] = exc

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    # The client proves it is node-a at the handshake, then
    # announces as node-c: the session must refuse by name.
    channel = node_a.channels.wrap_client(right, peer_cert=mesh.certs["node-b"])
    foreign = DiscoveryService(
        cert=mesh.certs["node-c"],
        root_cert=mesh.root_cert,
        host="192.0.2.3",
        port=9103,
        now=lambda: NOW,
    )
    channel.send_json({"kind": "announce", "announcement": foreign.announce()})
    thread.join(timeout=30)
    if thread.is_alive():
        channel.close()
        thread.join(timeout=30)
    assert not thread.is_alive()
    assert isinstance(results["server"], LanError)
    assert results["server"].reason == "announcement_mismatch"
    assert node_b.report()["refusals_by_reason"] == {"announcement_mismatch": 1}
    channel.close()


# --- (c) fabric messages cross whole and exact ----------------------------


def test_publication_and_direct_cross_in_both_directions(mesh):
    node_a = mesh.lan_node("node-a", port=9101)
    node_b = mesh.lan_node("node-b", port=9102)
    seen = []
    node_b.subscribe_wire("lan.hello", handler=seen.append)
    a_side, b_side = open_pair(node_a, node_b)

    published = node_a.publish("lan.hello", {"text": "hail", "n": 1})
    a_side.send_message(published)
    delivered = b_side.recv_message()
    assert delivered == published
    assert delivered["payload"] == {"text": "hail", "n": 1}
    assert seen == [published]
    assert node_b.wire_inbox() == [published]

    direct = node_b.direct("node-a", {"answer": 42})
    b_side.send_message(direct)
    assert a_side.recv_message() == direct
    assert node_a.wire_inbox() == [direct]
    a_side.close()
    b_side.close()


def test_direct_message_for_a_third_node_refused_at_the_sender(mesh):
    node_a = mesh.lan_node("node-a", port=9101)
    node_b = mesh.lan_node("node-b", port=9102)
    a_side, b_side = open_pair(node_a, node_b)
    stray = node_a.direct("node-c", {"for": "node-c"})
    with pytest.raises(LanError) as caught:
        a_side.send_message(stray)
    assert caught.value.reason == "wrong_peer"
    a_side.close()
    b_side.close()


# --- (d) reception is judged ----------------------------------------------


def test_message_naming_an_unproven_sender_is_refused(mesh):
    node_a = mesh.lan_node("node-a", port=9101)
    node_b = mesh.lan_node("node-b", port=9102)
    node_c = mesh.lan_node("node-c", port=9103)
    a_side, b_side = open_pair(node_a, node_b)
    # A genuine node-c message, replayed down node-a's channel:
    # the channel proved node-a, so node-c's name is refused.
    forged = node_c.publish("lan.hello", {"text": "i am c, honestly"})
    a_side.channel.send_json({"kind": "fabric", "message": forged})
    with pytest.raises(LanError) as caught:
        b_side.recv_message()
    assert caught.value.reason == "sender_mismatch"
    assert node_b.report()["refusals_by_reason"] == {"sender_mismatch": 1}
    assert node_b.wire_inbox() == []
    a_side.close()
    b_side.close()


def test_direct_message_for_another_node_is_refused_at_reception(mesh):
    node_a = mesh.lan_node("node-a", port=9101)
    node_b = mesh.lan_node("node-b", port=9102)
    a_side, b_side = open_pair(node_a, node_b)
    stray = node_a.direct("node-c", {"for": "node-c"})
    a_side.channel.send_json({"kind": "fabric", "message": stray})
    with pytest.raises(LanError) as caught:
        b_side.recv_message()
    assert caught.value.reason == "not_for_me"
    a_side.close()
    b_side.close()


# --- (e) a run is judged as a whole ----------------------------------------


def test_run_over_real_carriage_is_proven(mesh):
    policy = default_policy(NODES)
    report = evaluate_lan_run(
        policy,
        expected_fingerprints=expected_fingerprints_from_certs(mesh.certs),
        legs=full_legs(mesh),
        now=lambda: NOW,
    )
    assert validate_report(report)["verdict"] == "proven"
    assert report["pairs_expected"] == report["pairs_proven"] == 3
    assert report["messages_carried"] == 15
    assert report["simulated"] is False
    assert report["refusals"] == 0


def test_run_over_socketpairs_is_simulated_only(mesh):
    policy = default_policy(NODES)
    report = evaluate_lan_run(
        policy,
        expected_fingerprints=expected_fingerprints_from_certs(mesh.certs),
        legs=full_legs(mesh, simulated=True, path="loopback"),
        now=lambda: NOW,
    )
    assert report["verdict"] == "simulated_only"
    assert report["pairs_proven"] == 3
    assert report["simulated"] is True


def test_run_refusals_are_counted_by_name(mesh):
    policy = default_policy(NODES)
    expected = expected_fingerprints_from_certs(mesh.certs)
    legs = [
        real_leg(mesh, "node-a", "node-b", tls_version="TLSv1.2"),
        real_leg(mesh, "node-a", "node-c", a_saw_b_fingerprint=expected["node-a"]),
        # the node-b <-> node-c leg is missing entirely
    ]
    report = evaluate_lan_run(
        policy, expected_fingerprints=expected, legs=legs, now=lambda: NOW
    )
    assert report["verdict"] == "incomplete"
    assert report["pairs_proven"] == 0
    assert report["refusals_by_reason"] == {
        "tls_below_floor": 1,
        "fingerprint_mismatch": 1,
        "leg_missing": 1,
    }


def test_run_refuses_silent_cloud_and_malformed_legs(mesh):
    policy = default_policy(NODES)
    expected = expected_fingerprints_from_certs(mesh.certs)
    legs = full_legs(mesh)
    legs[0] = real_leg(mesh, "node-a", "node-b", messages_b_to_a=0)
    legs[1] = real_leg(mesh, "node-a", "node-c", discovery_via="none")
    legs[2] = {"not": "a leg"}
    report = evaluate_lan_run(
        policy, expected_fingerprints=expected, legs=legs, now=lambda: NOW
    )
    assert report["verdict"] == "incomplete"
    assert report["refusals_by_reason"] == {
        "leg_silent": 1,
        "discovery_not_unaided": 1,
        "malformed_leg": 1,
        "leg_missing": 1,
    }


def test_session_legs_from_socketpairs_judge_simulated_only(mesh):
    """The in-tree shape end to end: sessions over
    socketpairs produce legs whose judgement is
    simulated_only — never proven."""
    nodes = {node_id: mesh.lan_node(node_id, port=9100 + i) for i, node_id in enumerate(NODES)}
    sessions = []
    for a_id, b_id in (("node-a", "node-b"), ("node-a", "node-c"), ("node-b", "node-c")):
        a_side, b_side = open_pair(nodes[a_id], nodes[b_id])
        message = nodes[a_id].publish("lan.hello", {"from": a_id})
        a_side.send_message(message)
        b_side.recv_message()
        reply = nodes[b_id].direct(a_id, {"from": b_id})
        b_side.send_message(reply)
        a_side.recv_message()
        sessions.append((a_id, b_id, a_side, b_side))
    expected = expected_fingerprints_from_certs(mesh.certs)
    legs = []
    for a_id, b_id, a_side, b_side in sessions:
        legs.append(
            {
                "version": 1,
                "node_a": a_id,
                "node_b": b_id,
                "path": "loopback",
                "discovery_via": a_side.via,
                "tls_version": a_side.record["tls_version"],
                "cipher": a_side.record["cipher"],
                "a_saw_b_fingerprint": a_side.record["peer_fingerprint"],
                "b_saw_a_fingerprint": b_side.record["peer_fingerprint"],
                "announcements_exchanged": True,
                "messages_a_to_b": a_side.messages_sent,
                "messages_b_to_a": b_side.messages_sent,
                "simulated": True,
                "established_at": "2026-11-02T12:00:00Z",
            }
        )
        a_side.close()
        b_side.close()
    report = evaluate_lan_run(
        default_policy(NODES),
        expected_fingerprints=expected,
        legs=legs,
        now=lambda: NOW,
    )
    assert report["verdict"] == "simulated_only"
    assert report["pairs_proven"] == 3
    assert report["messages_carried"] == 6


# --- (f) the founding document ---------------------------------------------


def test_readme_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
