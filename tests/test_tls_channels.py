"""TLS everywhere tests — Y-051's acceptance.

The slice must prove: (a) the X.509 bridge is faithful —
certificates issued in pure Python DER from the mesh
root's Y-011 key carry the node's Y-011 key, window, and
serial, their signatures verify under Y-011's own
verifier, and OpenSSL chains them; (b) a real mutual
TLS 1.3 handshake completes over a socketpair, both
ends' channel records naming the peer by its Y-011
fingerprint, and a validated fabric message rides the
channel byte-identical; (c) no plaintext mode exists —
the policy schema refuses plaintext/anonymity outright,
every built context demands a peer certificate under a
TLS 1.3 floor, a plaintext speaker fails the handshake,
and a TLS-1.2-capped peer fails it too; (d) identity
binds — another mesh's node, a valid mesh node the
server was not told to trust, a peer whose handshake key
is not the expected certificate's key, and identity
material that does not verify at construction are all
refused by name and counted; (e) records are bounded
both ways and reports/events validate with names and
counts only; (f) the founding README is untouched.

The handshakes are real OpenSSL TLS over an in-process
socketpair — no LAN is crossed, and nothing here claims
one was (that proof is Y-062's).
"""

import hashlib
import socket
import ssl
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from yggdrasil.fabric import Fabric, validate_message
from yggdrasil.identity import (
    NodeKeypair,
    create_mesh_root,
    fingerprint,
    issue_node_certificate,
)
from yggdrasil.observability import validate_event
from yggdrasil.tls_channels import (
    TlsError,
    TlsNodeChannels,
    certificate_pem,
    default_policy,
    mesh_root_x509_der,
    node_x509_der,
    peer_public_key,
    validate_channel,
    validate_policy,
    validate_report,
    verify_x509_signature,
)

ROOT = Path(__file__).resolve().parents[1]
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
NOW = datetime(2026, 10, 31, 15, 0, tzinfo=UTC)


class Mesh:
    """A two-node mesh, provisioned the bootstrap way."""

    def __init__(self, tmp_path: Path):
        self.root_keypair, self.root_cert = create_mesh_root(now=NOW)
        self.certs = {}
        self.keypairs = {}
        for node_id in ("node-a", "node-b", "node-c"):
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

    def channels(self, node_id: str, **kwargs) -> TlsNodeChannels:
        return TlsNodeChannels.derive(
            root_keypair=self.root_keypair,
            node_id=node_id,
            keypair=self.keypairs[node_id],
            node_cert=self.certs[node_id],
            root_cert=self.root_cert,
            material_dir=self.tmp / node_id,
            now=lambda: NOW,
            **kwargs,
        )


@pytest.fixture()
def mesh(tmp_path):
    return Mesh(tmp_path)


def handshake(server_channels, server_kwargs, client_channels, client_kwargs):
    """Run one client/server wrap over a socketpair.

    Returns (server_result, client_result) where each is a
    TlsChannel or the exception its wrap raised."""
    left, right = socket.socketpair()
    results = {}

    def serve():
        try:
            results["server"] = server_channels.wrap_server(left, **server_kwargs)
        except Exception as exc:  # noqa: BLE001 — the test asserts on it
            results["server"] = exc

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        results["client"] = client_channels.wrap_client(right, **client_kwargs)
    except Exception as exc:  # noqa: BLE001 — the test asserts on it
        results["client"] = exc
    thread.join(timeout=30)
    if thread.is_alive():
        # A client that refused before handshaking leaves
        # the server waiting on the socket; closing our
        # ends lets its handshake fail so the thread ends.
        for sock in (left, right):
            try:
                sock.close()
            except OSError:
                pass
        thread.join(timeout=30)
    assert not thread.is_alive()
    return results["server"], results["client"]


# --- (a) the X.509 bridge ---------------------------------------------------


def test_x509_derivation_carries_the_y011_identity(mesh):
    root_der = mesh_root_x509_der(mesh.root_keypair, mesh.root_cert)
    assert peer_public_key(root_der) == mesh.root_keypair.public_key
    assert verify_x509_signature(root_der, mesh.root_keypair.public_key)
    node_der = node_x509_der(
        mesh.root_keypair, mesh.root_cert, mesh.certs["node-a"]
    )
    assert peer_public_key(node_der) == mesh.keypairs["node-a"].public_key
    assert verify_x509_signature(node_der, mesh.root_keypair.public_key)
    # A tampered certificate's signature no longer verifies.
    tampered = bytearray(node_der)
    tampered[-1] ^= 0x01
    assert not verify_x509_signature(bytes(tampered), mesh.root_keypair.public_key)


def test_x509_issuance_refuses_identity_y011_refuses(mesh):
    other_root_keypair, other_root_cert = create_mesh_root(now=NOW)
    foreign = issue_node_certificate(
        other_root_keypair,
        other_root_cert,
        node_id="node-x",
        node_keypair=NodeKeypair.generate(),
        device_class="vm",
        agent_version="0.0.1",
        now=NOW,
    )
    with pytest.raises(TlsError, match="does not verify"):
        node_x509_der(mesh.root_keypair, mesh.root_cert, foreign)
    with pytest.raises(TlsError, match="does not match"):
        mesh_root_x509_der(NodeKeypair.generate(), mesh.root_cert)


def test_construction_refuses_bad_material(mesh, tmp_path):
    good_der = node_x509_der(
        mesh.root_keypair, mesh.root_cert, mesh.certs["node-a"]
    )
    root_der = mesh_root_x509_der(mesh.root_keypair, mesh.root_cert)
    base = {
        "node_id": "node-a",
        "keypair": mesh.keypairs["node-a"],
        "node_cert": mesh.certs["node-a"],
        "root_cert": mesh.root_cert,
        "node_der": good_der,
        "root_der": root_der,
        "material_dir": tmp_path / "m",
    }
    with pytest.raises(TlsError, match="does not match the node certificate"):
        TlsNodeChannels(**{**base, "keypair": NodeKeypair.generate()})
    with pytest.raises(TlsError, match="names"):
        TlsNodeChannels(**{**base, "node_id": "node-b"})
    wrong_node_der = node_x509_der(
        mesh.root_keypair, mesh.root_cert, mesh.certs["node-b"]
    )
    with pytest.raises(TlsError, match="does not carry this node's Y-011 key"):
        TlsNodeChannels(**{**base, "node_der": wrong_node_der})
    channels = TlsNodeChannels(**base)
    key_mode = (tmp_path / "m" / "node-a.tls.key.pem").stat().st_mode & 0o777
    assert key_mode == 0o600
    assert channels.node_id == "node-a"


def test_construction_refuses_expired_identity(mesh, tmp_path):
    # A root and node certificate born ten days ago, the
    # node's window closed after one day: expired by now.
    old_root_keypair, old_root_cert = create_mesh_root(now=NOW - timedelta(days=10))
    expired = issue_node_certificate(
        old_root_keypair,
        old_root_cert,
        node_id="node-a",
        node_keypair=mesh.keypairs["node-a"],
        device_class="vm",
        agent_version="0.0.1",
        lifetime_days=1,
        now=NOW - timedelta(days=10),
    )
    with pytest.raises(TlsError, match="does not verify"):
        TlsNodeChannels.derive(
            root_keypair=old_root_keypair,
            node_id="node-a",
            keypair=mesh.keypairs["node-a"],
            node_cert=expired,
            root_cert=old_root_cert,
            material_dir=tmp_path / "old",
        )


# --- (c) no plaintext mode ----------------------------------------------------


def test_policy_constants_refuse_plaintext_and_anonymity():
    policy = default_policy()
    assert policy["plaintext_allowed"] is False
    assert policy["peer_certificate_required"] is True
    assert policy["minimum_version"] == "1.3"
    for broken in (
        {**policy, "plaintext_allowed": True},
        {**policy, "peer_certificate_required": False},
        {**policy, "minimum_version": "1.2"},
        {**policy, "protocol": "plaintext"},
    ):
        with pytest.raises(TlsError, match="fails its schema"):
            validate_policy(broken)


def test_built_contexts_demand_certificates_under_a_13_floor(mesh):
    channels = mesh.channels("node-a")
    for context in (channels.server_context(), channels.client_context()):
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.minimum_version == ssl.TLSVersion.TLSv1_3


def test_plaintext_speaker_fails_the_handshake(mesh):
    server = mesh.channels("node-a")
    left, right = socket.socketpair()
    results = {}

    def serve():
        try:
            results["server"] = server.wrap_server(
                left, peer_certs={"node-b": mesh.certs["node-b"]}
            )
        except Exception as exc:  # noqa: BLE001 — asserted below
            results["server"] = exc

    thread = threading.Thread(target=serve)
    thread.start()
    right.sendall(b"GET / HTTP/1.0\r\n\r\n")  # plaintext, no handshake at all
    right.close()
    thread.join(timeout=30)
    assert isinstance(results["server"], TlsError)
    report = validate_report(server.report())
    assert report["refusals_by_reason"] == {"handshake_failed": 1}
    assert report["channels_established"] == 0


def test_tls12_capped_peer_fails_the_handshake(mesh, tmp_path):
    server = mesh.channels("node-a")
    # A raw context for node-b, deliberately capped at 1.2:
    # the material is genuine, the version is not enough.
    raw = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    raw.check_hostname = False
    raw.verify_mode = ssl.CERT_REQUIRED
    raw.maximum_version = ssl.TLSVersion.TLSv1_2
    raw.load_verify_locations(
        cadata=certificate_pem(
            mesh_root_x509_der(mesh.root_keypair, mesh.root_cert)
        )
    )
    raw.load_cert_chain(
        certfile=str(_material(mesh, tmp_path, "node-b")),
        keyfile=str(tmp_path / "node-b" / "node-b.tls.key.pem"),
    )
    left, right = socket.socketpair()
    results = {}

    def serve():
        try:
            results["server"] = server.wrap_server(
                left, peer_certs={"node-b": mesh.certs["node-b"]}
            )
        except Exception as exc:  # noqa: BLE001 — asserted below
            results["server"] = exc

    thread = threading.Thread(target=serve)
    thread.start()
    with pytest.raises(ssl.SSLError):
        raw.wrap_socket(right, server_side=False)
    thread.join(timeout=30)
    assert isinstance(results["server"], TlsError)
    assert server.report()["refusals_by_reason"] == {"handshake_failed": 1}


def _material(mesh, tmp_path, node_id):
    mesh.channels(node_id)
    return tmp_path / node_id / f"{node_id}.tls.crt.pem"


# --- (b) a real mutual handshake, and carriage --------------------------------


def test_mutual_handshake_and_fabric_message_carriage(mesh):
    server = mesh.channels("node-a")
    client = mesh.channels("node-b")
    server_channel, client_channel = handshake(
        server,
        {"peer_certs": {"node-b": mesh.certs["node-b"]}},
        client,
        {"peer_cert": mesh.certs["node-a"]},
    )
    assert not isinstance(server_channel, Exception), server_channel
    assert not isinstance(client_channel, Exception), client_channel
    for channel, local, peer, role in (
        (server_channel, "node-a", "node-b", "server"),
        (client_channel, "node-b", "node-a", "client"),
    ):
        record = validate_channel(channel.record)
        assert record["local_node"] == local
        assert record["peer_node"] == peer
        assert record["role"] == role
        assert record["tls_version"] == "TLSv1.3"
        assert record["cipher"]
        assert record["simulated"] is False
        assert record["peer_fingerprint"] == fingerprint(
            mesh.keypairs[peer].public_key
        )
    # A fabric message rides the channel and arrives identical.
    fabric = Fabric(now=lambda: NOW)
    fabric.attach("node-b")
    fabric.attach("node-a")
    fabric.subscribe("node-a", "registry.updates")
    message = fabric.publish(
        "node-b", "registry.updates", {"node": "node-b", "models": ["m1"]}
    )
    client_channel.send_json(message)
    received = server_channel.recv_json()
    assert received == validate_message(message)
    server_channel.send_json({"ack": message["message_id"]})
    assert client_channel.recv_json() == {"ack": message["message_id"]}
    server_channel.close()
    client_channel.close()
    for channels, peer in ((server, "node-b"), (client, "node-a")):
        report = validate_report(channels.report())
        assert report["channels_established"] == 1
        assert report["refusals"] == 0
        assert report["peers"] == [peer]
        for event in channels.events:
            validate_event(event)
        established = [
            e for e in channels.events
            if e["type"] == "fabric.tls_channel_established"
        ]
        assert len(established) == 1
        assert established[0]["source"]["component"] == "fabric"


# --- (d) identity binds ---------------------------------------------------------


def test_foreign_mesh_node_fails_the_handshake(mesh):
    other_root_keypair, other_root_cert = create_mesh_root(now=NOW)
    foreign_keypair = NodeKeypair.generate()
    foreign_cert = issue_node_certificate(
        other_root_keypair,
        other_root_cert,
        node_id="node-b",
        node_keypair=foreign_keypair,
        device_class="vm",
        agent_version="0.0.1",
        now=NOW,
    )
    foreign = TlsNodeChannels.derive(
        root_keypair=other_root_keypair,
        node_id="node-b",
        keypair=foreign_keypair,
        node_cert=foreign_cert,
        root_cert=other_root_cert,
        material_dir=mesh.tmp / "foreign",
        now=lambda: NOW,
    )
    server = mesh.channels("node-a")
    # The foreign client expects its own mesh's "node-a",
    # so it proceeds to the handshake — where OpenSSL
    # itself refuses both directions, because neither
    # certificate chains to the other mesh's root.
    foreign_peer = issue_node_certificate(
        other_root_keypair,
        other_root_cert,
        node_id="node-a",
        node_keypair=NodeKeypair.generate(),
        device_class="vm",
        agent_version="0.0.1",
        now=NOW,
    )
    server_channel, client_channel = handshake(
        server,
        {"peer_certs": {"node-b": mesh.certs["node-b"]}},
        foreign,
        {"peer_cert": foreign_peer},
    )
    # OpenSSL itself refuses: the foreign certificate does
    # not chain to this mesh's root on either side.
    assert isinstance(server_channel, TlsError)
    assert isinstance(client_channel, TlsError)
    assert server.report()["refusals_by_reason"] == {"handshake_failed": 1}


def test_unlisted_mesh_node_is_refused_by_name(mesh):
    server = mesh.channels("node-a")
    client = mesh.channels("node-c")
    server_channel, client_channel = handshake(
        server,
        {"peer_certs": {"node-b": mesh.certs["node-b"]}},  # node-c not vouched
        client,
        {"peer_cert": mesh.certs["node-a"]},
    )
    # The handshake itself succeeds (node-c is a genuine
    # mesh member); the binding refuses it — it is not the
    # peer this door was told to trust.
    assert not isinstance(client_channel, Exception), client_channel
    assert isinstance(server_channel, TlsError)
    assert "does not belong" in str(server_channel)
    report = server.report()
    assert report["refusals_by_reason"] == {"peer_mismatch": 1}
    client_channel.close()


def test_expected_peer_key_mismatch_is_refused(mesh):
    server = mesh.channels("node-b")
    client = mesh.channels("node-a")
    # The client opens to node-b but was handed node-c's
    # certificate as the expectation: the handshake proves
    # node-b's key, which is not the expected key.
    server_channel, client_channel = handshake(
        server,
        {"peer_certs": {"node-a": mesh.certs["node-a"]}},
        client,
        {"peer_cert": mesh.certs["node-c"]},
    )
    assert not isinstance(server_channel, Exception), server_channel
    assert isinstance(client_channel, TlsError)
    assert client.report()["refusals_by_reason"] == {"peer_mismatch": 1}
    server_channel.close()


def test_peer_certificate_from_another_root_refused_before_handshake(mesh):
    client = mesh.channels("node-a")
    other_root_keypair, other_root_cert = create_mesh_root(now=NOW)
    foreign = issue_node_certificate(
        other_root_keypair,
        other_root_cert,
        node_id="node-b",
        node_keypair=NodeKeypair.generate(),
        device_class="vm",
        agent_version="0.0.1",
        now=NOW,
    )
    left, _right = socket.socketpair()
    with pytest.raises(TlsError, match="does not verify"):
        client.wrap_client(left, peer_cert=foreign)
    assert client.report()["refusals_by_reason"] == {
        "peer_certificate_invalid": 1
    }


# --- (e) record bounds ------------------------------------------------------------


def test_record_bounds_bite_both_ways(mesh):
    small = {
        **default_policy(),
        "max_record_bytes": 1024,
    }
    server = mesh.channels("node-a", policy=small)
    client = mesh.channels("node-b", policy=small)
    server_channel, client_channel = handshake(
        server,
        {"peer_certs": {"node-b": mesh.certs["node-b"]}},
        client,
        {"peer_cert": mesh.certs["node-a"]},
    )
    with pytest.raises(TlsError, match="exceeds the policy bound"):
        client_channel.send_json({"blob": "x" * 5000})
    client_channel.send_json({"blob": "x" * 100})
    assert server_channel.recv_json() == {"blob": "x" * 100}
    with pytest.raises(TlsError, match="not JSON data"):
        client_channel.send_json({"bad": object()})
    server_channel.close()
    client_channel.close()
    with pytest.raises(TlsError, match="closed"):
        client_channel.send_json({"late": True})


def test_channels_are_the_only_door(mesh):
    # The module surface offers contexts and wraps only —
    # there is no plaintext or unverified variant to reach
    # for by accident.
    import yggdrasil.tls_channels as module

    public = {name for name in dir(module) if not name.startswith("_")}
    assert not any("plain" in name.lower() for name in public)
    assert not any("insecure" in name.lower() for name in public)
    assert not any("unverified" in name.lower() for name in public)


# --- (f) the founding document ------------------------------------------------------


def test_readme_unchanged():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
