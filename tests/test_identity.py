"""Identity foundation tests — Y-011's acceptance.

Cryptography is proven against the RFC 8032 test vectors (real
published values, not self-consistency). Certificates are proven
as a chain: root self-signs, root issues, verification accepts the
honest chain and rejects tampering, a foreign root, and expired
or not-yet-valid windows. Custody is proven on disk: seeds land
mode 0600, are never overwritten, and a node without Kista says
its custody is local. All keys here are throwaway test keys.
"""

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from yggdrasil import ed25519
from yggdrasil.identity import (
    IdentityError,
    LocalKeyStore,
    NodeKeypair,
    certificate_identity,
    create_mesh_root,
    describe_custody,
    fingerprint,
    issue_node_certificate,
    verify_certificate,
)

ROOT = Path(__file__).resolve().parents[1]
IDENTITY_SCHEMA = json.loads(
    (ROOT / "contracts" / "schemas" / "node_identity.schema.json").read_text()
)

NOW = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)

# RFC 8032 section 7.1, TEST 1 and TEST 2 (secret, public, message, signature).
RFC_VECTORS = [
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        (
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
            "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
        ),
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        (
            "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
            "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"
        ),
    ),
]


@pytest.mark.parametrize("seed_hex,pk_hex,msg_hex,sig_hex", RFC_VECTORS)
def test_ed25519_matches_rfc8032_vectors(seed_hex, pk_hex, msg_hex, sig_hex):
    seed, pk, msg, sig = (bytes.fromhex(x) for x in (seed_hex, pk_hex, msg_hex, sig_hex))
    assert ed25519.public_key(seed) == pk
    assert ed25519.sign(seed, msg) == sig
    assert ed25519.verify(pk, msg, sig)


def test_ed25519_rejects_wrong_message_and_key():
    kp = NodeKeypair.generate()
    sig = kp.sign(b"join the mesh")
    assert ed25519.verify(kp.public_key, b"join the mesh", sig)
    assert not ed25519.verify(kp.public_key, b"join the other mesh", sig)
    assert not ed25519.verify(NodeKeypair.generate().public_key, b"join the mesh", sig)
    assert not ed25519.verify(kp.public_key, b"join the mesh", sig[:32] + b"\x00" * 32)


def test_keypairs_are_unique_and_fingerprints_stable():
    a, b = NodeKeypair.generate(), NodeKeypair.generate()
    assert a.public_key != b.public_key
    assert fingerprint(a.public_key) == a.fingerprint
    assert len(a.fingerprint) == 64


def _mesh():
    root_kp, root_cert = create_mesh_root(now=NOW)
    node_kp = NodeKeypair.generate()
    cert = issue_node_certificate(
        root_kp,
        root_cert,
        node_id="forge-pi-01",
        node_keypair=node_kp,
        device_class="pi",
        agent_version="0.0.1",
        now=NOW,
    )
    return root_kp, root_cert, node_kp, cert


def test_root_certificate_is_self_signed_and_verifies():
    _, root_cert = create_mesh_root(now=NOW)
    verify_certificate(root_cert, root_cert, now=NOW)


def test_issued_node_certificate_verifies():
    _, root_cert, _, cert = _mesh()
    verify_certificate(cert, root_cert, now=NOW)


def test_tampered_certificate_fails():
    _, root_cert, _, cert = _mesh()
    forged = dict(cert, device_class="server")
    with pytest.raises(IdentityError):
        verify_certificate(forged, root_cert, now=NOW)


def test_foreign_root_cannot_verify():
    _, _, _, cert = _mesh()
    _, other_root = create_mesh_root(now=NOW)
    with pytest.raises(IdentityError):
        verify_certificate(cert, other_root, now=NOW)


def test_validity_window_is_enforced():
    _, root_cert, _, cert = _mesh()
    with pytest.raises(IdentityError):
        verify_certificate(cert, root_cert, now=NOW - timedelta(seconds=1))
    with pytest.raises(IdentityError):
        verify_certificate(cert, root_cert, now=NOW + timedelta(days=366))


def test_certificate_identity_matches_node_contract():
    _, _, _, cert = _mesh()
    identity = certificate_identity(cert)
    v = Draft202012Validator(IDENTITY_SCHEMA)
    assert list(v.iter_errors(identity)) == []
    assert identity["node_id"] == "forge-pi-01"


def test_private_key_never_appears_in_certificate():
    _, root_cert, node_kp, cert = _mesh()
    blob = json.dumps([cert, root_cert])
    assert node_kp.seed.hex() not in blob
    assert node_kp.seed not in json.dumps(cert).encode()


def test_local_keystore_roundtrip_permissions_and_no_overwrite(tmp_path):
    store = LocalKeyStore(tmp_path / "keys")
    kp = NodeKeypair.generate()
    path = store.save_seed("forge-pi-01", kp.seed)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert store.load_keypair("forge-pi-01").public_key == kp.public_key
    with pytest.raises(IdentityError):
        store.save_seed("forge-pi-01", NodeKeypair.generate().seed)
    with pytest.raises(IdentityError):
        store.load_keypair("no-such-node")


def test_certificate_store_roundtrip(tmp_path):
    _, _, _, cert = _mesh()
    store = LocalKeyStore(tmp_path / "keys")
    store.save_certificate(cert)
    assert store.load_certificate("forge-pi-01") == cert


def test_custody_is_honest_without_kista(monkeypatch):
    monkeypatch.delenv("YGGDRASIL_KISTA_SOCKET", raising=False)
    report = describe_custody()
    assert report["backend"] == "local"
    assert "local" in report["note"].lower()
