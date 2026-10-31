"""Identity foundation — Y-011.

Per-node Ed25519 keypairs; node certificates issued by the mesh's
own root (a self-signed mesh-root keypair the operator creates at
bootstrap — the local-scale seed of WorldNet section 3; the global
attestation/DID machinery of that section is Y-211, NOT built here).

A node *is* its key: the certificate binds node_id to a public key
under the root's signature, and verification re-checks that chain —
schema, fingerprint, signature, validity window — every time.

Key custody: private seeds live behind the KeyStore interface.
The local store (implemented here) writes seeds mode 0600 and
refuses to overwrite. Where a node supports Kista, a Kista store
plugs into the same interface — that integration is Y-196; until
then ``describe_custody`` says plainly which backend a node uses,
and a node on the local store *says so* (threat T-10's terms).
"""

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jsonschema import Draft202012Validator

from yggdrasil import ed25519

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "schemas"
    / "node_certificate.schema.json"
)
_CERT_SCHEMA = json.loads(_SCHEMA_PATH.read_text())
_CERT_VALIDATOR = Draft202012Validator(_CERT_SCHEMA)

ROOT_NODE_ID = "mesh-root"


class IdentityError(Exception):
    """Any identity, certificate, or key-custody failure."""


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def fingerprint(public_key: bytes) -> str:
    """A public key's name in evidence: sha256 hex of the raw key."""
    return hashlib.sha256(public_key).hexdigest()


def _canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _now() -> datetime:
    return datetime.now(UTC)


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


@dataclass(frozen=True)
class NodeKeypair:
    """A node's private seed and its derived public key."""

    seed: bytes

    @classmethod
    def generate(cls) -> "NodeKeypair":
        return cls(seed=ed25519.generate_seed())

    @property
    def public_key(self) -> bytes:
        return ed25519.public_key(self.seed)

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.public_key)

    def sign(self, message: bytes) -> bytes:
        return ed25519.sign(self.seed, message)


def _signed_body(keypair: NodeKeypair, body: dict) -> dict:
    cert = dict(body)
    cert["signature"] = _b64(keypair.sign(_canonical(body)))
    errors = sorted(_CERT_VALIDATOR.iter_errors(cert), key=str)
    if errors:
        raise IdentityError(f"minted certificate fails its own schema: {errors[0].message}")
    return cert


def create_mesh_root(
    keypair: NodeKeypair | None = None,
    *,
    lifetime_days: int = 3650,
    now: datetime | None = None,
) -> tuple[NodeKeypair, dict]:
    """Create the mesh root: a keypair plus its self-signed certificate."""
    keypair = keypair or NodeKeypair.generate()
    moment = now or _now()
    body = {
        "version": 1,
        "kind": "mesh_root",
        "serial": hashlib.sha256(keypair.public_key + b"root").hexdigest()[:32],
        "node_id": ROOT_NODE_ID,
        "public_key": _b64(keypair.public_key),
        "issued_at": _fmt(moment),
        "expires_at": _fmt(moment + timedelta(days=lifetime_days)),
        "issuer_node_id": ROOT_NODE_ID,
        "issuer_fingerprint": keypair.fingerprint,
    }
    return keypair, _signed_body(keypair, body)


def issue_node_certificate(
    root_keypair: NodeKeypair,
    root_cert: dict,
    *,
    node_id: str,
    node_keypair: NodeKeypair,
    device_class: str,
    agent_version: str,
    lifetime_days: int = 365,
    now: datetime | None = None,
) -> dict:
    """Issue a node certificate under the mesh root's signature."""
    verify_certificate(root_cert, root_cert, now=now)  # root must itself hold
    moment = now or _now()
    body = {
        "version": 1,
        "kind": "node",
        "serial": hashlib.sha256(
            node_keypair.public_key + moment.isoformat().encode()
        ).hexdigest()[:32],
        "node_id": node_id,
        "public_key": _b64(node_keypair.public_key),
        "device_class": device_class,
        "agent_version": agent_version,
        "issued_at": _fmt(moment),
        "expires_at": _fmt(moment + timedelta(days=lifetime_days)),
        "issuer_node_id": ROOT_NODE_ID,
        "issuer_fingerprint": root_keypair.fingerprint,
    }
    return _signed_body(root_keypair, body)


def verify_certificate(cert: dict, root_cert: dict, *, now: datetime | None = None) -> None:
    """Raise IdentityError unless cert chains to root_cert and is in window.

    Checks, in order: schema; the root's own self-signature (when
    cert *is* the root) or the issuer binding to the root; the
    issuer fingerprint against the issuer key; the signature over
    the canonical body; and the validity window.
    """
    errors = sorted(_CERT_VALIDATOR.iter_errors(cert), key=str)
    if errors:
        raise IdentityError(f"certificate fails schema: {errors[0].message}")
    root_errors = sorted(_CERT_VALIDATOR.iter_errors(root_cert), key=str)
    if root_errors:
        raise IdentityError(f"root certificate fails schema: {root_errors[0].message}")
    if root_cert["kind"] != "mesh_root" or root_cert["node_id"] != ROOT_NODE_ID:
        raise IdentityError("root certificate is not a mesh root")

    if cert["kind"] == "mesh_root":
        issuer_key = _unb64(cert["public_key"])
        if cert["issuer_node_id"] != cert["node_id"]:
            raise IdentityError("root certificate is not self-issued")
    else:
        if cert["issuer_node_id"] != root_cert["node_id"]:
            raise IdentityError("certificate was not issued by this mesh root")
        issuer_key = _unb64(root_cert["public_key"])
    if cert["issuer_fingerprint"] != fingerprint(issuer_key):
        raise IdentityError("issuer fingerprint does not match the issuer key")

    body = {k: v for k, v in cert.items() if k != "signature"}
    if not ed25519.verify(issuer_key, _canonical(body), _unb64(cert["signature"])):
        raise IdentityError("certificate signature does not verify")

    moment = now or _now()
    if moment < _parse(cert["issued_at"]):
        raise IdentityError("certificate is not yet valid")
    if moment >= _parse(cert["expires_at"]):
        raise IdentityError("certificate has expired")


def certificate_identity(cert: dict) -> dict:
    """The NodeIdentity (contract Y-006) a node certificate carries."""
    if cert["kind"] != "node":
        raise IdentityError("the mesh root has no node identity")
    for field in ("device_class", "agent_version"):
        if field not in cert:
            raise IdentityError(f"certificate lacks {field}; cannot form a node identity")
    return {
        "node_id": cert["node_id"],
        "public_key": cert["public_key"],
        "device_class": cert["device_class"],
        "agent_version": cert["agent_version"],
    }


# --- Key custody -------------------------------------------------------


def kista_available() -> bool:
    """True only when this node actually exposes a Kista key store.

    Detection is evidence, not hope: the YGGDRASIL_KISTA_SOCKET path
    must exist. Full Kista custody integration is Y-196.
    """
    socket = os.environ.get("YGGDRASIL_KISTA_SOCKET")
    return bool(socket) and Path(socket).exists()


def describe_custody() -> dict:
    """Say plainly where this node's keys live (threat T-10's terms)."""
    if kista_available():
        return {
            "backend": "kista",
            "note": "Kista socket detected; custody integration lands at Y-196.",
        }
    return {
        "backend": "local",
        "note": "Protected local store (mode 0600 seeds); this node says so.",
    }


class LocalKeyStore:
    """Private seeds on local disk: mode 0600, never overwritten."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _seed_path(self, node_id: str) -> Path:
        return self.directory / f"{node_id}.key"

    def save_seed(self, node_id: str, seed: bytes) -> Path:
        if len(seed) != 32:
            raise IdentityError("seed must be 32 bytes")
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._seed_path(node_id)
        if path.exists():
            raise IdentityError(f"refusing to overwrite existing key for {node_id}")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(seed)
        return path

    def load_keypair(self, node_id: str) -> NodeKeypair:
        path = self._seed_path(node_id)
        if not path.exists():
            raise IdentityError(f"no key stored for {node_id}")
        seed = path.read_bytes()
        if len(seed) != 32:
            raise IdentityError(f"stored key for {node_id} is not a 32-byte seed")
        return NodeKeypair(seed=seed)

    def save_certificate(self, cert: dict) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{cert['node_id']}.cert.json"
        path.write_text(json.dumps(cert, indent=2, sort_keys=True) + "\n")
        return path

    def load_certificate(self, node_id: str) -> dict:
        path = self.directory / f"{node_id}.cert.json"
        if not path.exists():
            raise IdentityError(f"no certificate stored for {node_id}")
        return json.loads(path.read_text())
