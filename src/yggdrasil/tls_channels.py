"""TLS everywhere — Y-051.

The threat model's stance, built: *every channel is mTLS*
(T-01, T-06). This slice gives the fabric its carriage
layer — :class:`TlsNodeChannels`, one node's channel
factory — under three rules that are constants, not
options:

- **Node certificates (Y-011) on every channel.** A node's
  TLS identity *is* its Y-011 identity. The X.509
  certificates OpenSSL requires are issued here, in pure
  Python DER, from the mesh root's Y-011 keypair and
  carrying the node's Y-011 Ed25519 public key — the same
  key, the same fingerprint, the same validity window,
  the serial carried across. The root's X.509 is a CA
  certificate; a node's is a leaf usable for both server
  and client authentication. No second key, no second
  trust root, nothing to drift apart.
- **Mutual, always.** Both ends present a certificate and
  both ends verify: the TLS handshake chains the peer's
  X.509 to the mesh root, and after the handshake the
  public key read out of the peer's certificate must
  belong to a Y-011 node certificate that itself verifies
  against the mesh root (chain, fingerprint, window) and
  names the expected peer. A channel to an unnamed,
  unknown, or mismatched peer is refused by name,
  counted, and closed.
- **No plaintext mode exists.** The policy schema makes
  ``plaintext_allowed`` a constant ``false``,
  ``peer_certificate_required`` a constant ``true``, and
  the minimum version a constant TLS 1.3; the module
  exposes no function that builds an unverified or
  unencrypted context, and every context it builds sets
  ``CERT_REQUIRED`` under a 1.3 floor. A peer that speaks
  plaintext, caps itself below TLS 1.3, or carries
  another mesh's certificate fails the handshake itself.

**Key custody stays Y-011's.** Deriving X.509 material
needs the root's private key, so derivation is a
bootstrap act — :meth:`TlsNodeChannels.derive` is called
by whoever holds the root key at provisioning (the
operator's bootstrap, or the root node itself). A plain
node is constructed with the *derived public material*
(the two certificates' DER) plus its own keypair: it
verifies the DER's signature and key bindings against
the Y-011 certificates before use, and never holds the
root key. A node's own private key is written only into
its own material directory, mode 0600, as PKCS#8 derived
from its Y-011 seed — the seed itself is never written.

Sockets are the caller's: this module imports no socket
client (the gates' rule) — it wraps a connected socket
the deployment wiring hands it (``wrap_client`` /
``wrap_server``) and returns a :class:`TlsChannel` that
carries length-prefixed JSON records; fabric messages
ride it unchanged.

Honesty: the handshakes proven in the test suite are
**real** OpenSSL TLS 1.3, mutually authenticated, over
an in-process socketpair — the encryption and the
certificate verification are the platform TLS stack's,
not a model, so channel records carry ``simulated:
false``. What is not proven here is carriage across a
real LAN between machines: that is Y-062 (the fabric on
the real LAN, TLS on), and Part VIII takes it to
hardware. The Ed25519 signing underneath is Y-011's
pure-Python implementation, with its stated
not-constant-time limit.
"""

import base64
import json
import os
import ssl
import struct
import threading
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil import ed25519
from yggdrasil.identity import (
    IdentityError,
    NodeKeypair,
    fingerprint,
    verify_certificate,
)
from yggdrasil.observability import make_event

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

_ED25519_OID = (1, 3, 101, 112)


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


_POLICY_VALIDATOR = _validator("tls_policy.schema.json")
_CHANNEL_VALIDATOR = _validator("tls_channel.schema.json")
_REPORT_VALIDATOR = _validator("tls_report.schema.json")


class TlsError(Exception):
    """Any TLS channel failure: a policy or record that
    fails its schema, identity material that does not
    verify, a handshake OpenSSL refused, or a peer whose
    certificate does not name the node the channel was
    opened to. Refusals at establishment are counted
    before they raise."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise TlsError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_policy(record: dict) -> dict:
    return _validate(_POLICY_VALIDATOR, record, "tls policy")


def validate_channel(record: dict) -> dict:
    return _validate(_CHANNEL_VALIDATOR, record, "tls channel")


def validate_report(record: dict) -> dict:
    return _validate(_REPORT_VALIDATOR, record, "tls report")


def default_policy() -> dict:
    """The channel security policy as data — and the only
    shape the schema admits: TLS, floor 1.3, a peer
    certificate always required, plaintext never allowed.
    ``max_record_bytes`` (1 MiB) is the one operator
    number: the largest single JSON record a channel
    carries, a policy not a fact."""
    return validate_policy(
        {
            "version": 1,
            "protocol": "TLS",
            "minimum_version": "1.3",
            "peer_certificate_required": True,
            "plaintext_allowed": False,
            "max_record_bytes": 1_048_576,
        }
    )


# --- DER encoding (X.509 issuance from Y-011 keys) -----------------------
#
# The smallest honest DER writer: just the tags an Ed25519
# certificate needs. OpenSSL is the reader and the judge —
# a certificate it will not chain or a signature it will
# not verify fails the handshake in the tests, in the
# open.


def _der_len(size: int) -> bytes:
    if size < 128:
        return bytes([size])
    body = size.to_bytes((size.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _tlv(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _der_len(len(content)) + content


def _seq(*parts: bytes) -> bytes:
    return _tlv(0x30, b"".join(parts))


def _set(*parts: bytes) -> bytes:
    return _tlv(0x31, b"".join(parts))


def _integer(value: int) -> bytes:
    body = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    if body[0] & 0x80:
        body = b"\x00" + body
    return _tlv(0x02, body)


def _base128(value: int) -> bytes:
    if value == 0:
        return b"\x00"
    digits = []
    while value:
        digits.append(value & 0x7F)
        value >>= 7
    digits.reverse()
    return bytes(
        digit | (0x80 if index < len(digits) - 1 else 0)
        for index, digit in enumerate(digits)
    )


def _oid(*numbers: int) -> bytes:
    body = bytes([numbers[0] * 40 + numbers[1]]) + b"".join(
        _base128(number) for number in numbers[2:]
    )
    return _tlv(0x06, body)


def _utf8(text: str) -> bytes:
    return _tlv(0x0C, text.encode("utf-8"))


def _utctime(moment: datetime) -> bytes:
    return _tlv(0x17, moment.strftime("%y%m%d%H%M%SZ").encode("ascii"))


def _octet(data: bytes) -> bytes:
    return _tlv(0x04, data)


def _bitstring(data: bytes, unused_bits: int = 0) -> bytes:
    return _tlv(0x03, bytes([unused_bits]) + data)


def _boolean(value: bool) -> bytes:
    return _tlv(0x01, b"\xff" if value else b"\x00")


def _explicit(number: int, inner: bytes) -> bytes:
    return _tlv(0xA0 + number, inner)


_ALGORITHM = _seq(_oid(*_ED25519_OID))


def _name(common_name: str) -> bytes:
    return _seq(_set(_seq(_oid(2, 5, 4, 3), _utf8(common_name))))


def _spki(public_key: bytes) -> bytes:
    return _seq(_ALGORITHM, _bitstring(public_key))


def _extension(numbers: tuple, value_der: bytes, *, critical: bool = False) -> bytes:
    parts = [_oid(*numbers)]
    if critical:
        parts.append(_boolean(True))
    parts.append(_octet(value_der))
    return _seq(*parts)


def _parse_cert_time(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _issue_x509(
    issuer_keypair: NodeKeypair,
    *,
    issuer_name: str,
    subject_name: str,
    subject_public_key: bytes,
    serial_hex: str,
    issued_at: str,
    expires_at: str,
    is_ca: bool,
) -> bytes:
    """One X.509 certificate, signed with the issuer's
    Ed25519 key (Y-011's own signer). The validity window
    and serial are the Y-011 certificate's, carried
    across unchanged."""
    extensions = [
        _extension(
            (2, 5, 29, 19),
            _seq(_boolean(True)) if is_ca else _seq(),
            critical=True,
        )
    ]
    if is_ca:
        # keyCertSign + cRLSign, nothing else.
        extensions.append(
            _extension((2, 5, 29, 15), _bitstring(b"\x06", 1), critical=True)
        )
    else:
        # digitalSignature only, plus server+client auth —
        # a mesh node is both ends of its channels.
        extensions.append(
            _extension((2, 5, 29, 15), _bitstring(b"\x80", 7), critical=True)
        )
        extensions.append(
            _extension(
                (2, 5, 29, 37),
                _seq(_oid(1, 3, 6, 1, 5, 5, 7, 3, 1), _oid(1, 3, 6, 1, 5, 5, 7, 3, 2)),
            )
        )
    tbs = _seq(
        _explicit(0, _integer(2)),
        _integer(int(serial_hex, 16)),
        _ALGORITHM,
        _name(issuer_name),
        _seq(
            _utctime(_parse_cert_time(issued_at)),
            _utctime(_parse_cert_time(expires_at)),
        ),
        _name(subject_name),
        _spki(subject_public_key),
        _explicit(3, _seq(*extensions)),
    )
    signature = issuer_keypair.sign(tbs)
    return _seq(tbs, _ALGORITHM, _bitstring(signature))


def mesh_root_x509_der(root_keypair: NodeKeypair, root_cert: dict) -> bytes:
    """The mesh root's X.509 CA certificate, derived from
    its Y-011 certificate and signed by its own key. The
    Y-011 certificate must itself verify first, and the
    keypair must be the certificate's key."""
    try:
        verify_certificate(root_cert, root_cert)
    except IdentityError as exc:
        raise TlsError(f"mesh root certificate does not verify: {exc}") from exc
    cert_public = base64.b64decode(root_cert["public_key"].encode("ascii"))
    if root_keypair.public_key != cert_public:
        raise TlsError("root keypair does not match the mesh root certificate")
    return _issue_x509(
        root_keypair,
        issuer_name=root_cert["node_id"],
        subject_name=root_cert["node_id"],
        subject_public_key=root_keypair.public_key,
        serial_hex=root_cert["serial"],
        issued_at=root_cert["issued_at"],
        expires_at=root_cert["expires_at"],
        is_ca=True,
    )


def node_x509_der(
    root_keypair: NodeKeypair, root_cert: dict, node_cert: dict
) -> bytes:
    """A node's X.509 leaf certificate, issued by the mesh
    root's key over the node's Y-011 public key, valid for
    exactly the Y-011 certificate's window. The Y-011
    certificate must verify against the root first — an
    X.509 is never minted for an identity Y-011 refuses."""
    try:
        verify_certificate(node_cert, root_cert)
    except IdentityError as exc:
        raise TlsError(f"node certificate does not verify: {exc}") from exc
    public_key = base64.b64decode(node_cert["public_key"].encode("ascii"))
    return _issue_x509(
        root_keypair,
        issuer_name=root_cert["node_id"],
        subject_name=node_cert["node_id"],
        subject_public_key=public_key,
        serial_hex=node_cert["serial"],
        issued_at=node_cert["issued_at"],
        expires_at=node_cert["expires_at"],
        is_ca=False,
    )


def certificate_pem(der: bytes) -> str:
    encoded = base64.b64encode(der).decode("ascii")
    lines = "\n".join(encoded[i : i + 64] for i in range(0, len(encoded), 64))
    return f"-----BEGIN CERTIFICATE-----\n{lines}\n-----END CERTIFICATE-----\n"


def private_key_pem(keypair: NodeKeypair) -> str:
    """The node's Ed25519 private key as PKCS#8 PEM,
    derived from the Y-011 seed (the standard encoding:
    an octet string wrapping an octet string of the
    seed). Written only to the node's own key file."""
    der = _seq(_integer(0), _ALGORITHM, _octet(_octet(keypair.seed)))
    encoded = base64.b64encode(der).decode("ascii")
    lines = "\n".join(encoded[i : i + 64] for i in range(0, len(encoded), 64))
    return f"-----BEGIN PRIVATE KEY-----\n{lines}\n-----END PRIVATE KEY-----\n"


# --- DER reading (peers' keys, and X.509 signature checks) ----------------


def _read_tlv(data: bytes, offset: int) -> tuple[int, int, int, int]:
    """Read one DER TLV at ``offset``: (tag, content_start,
    content_end, next_offset). Single-byte tags suffice
    for every field a certificate carries on this road."""
    if offset + 2 > len(data):
        raise TlsError("truncated DER")
    tag = data[offset]
    first_len = data[offset + 1]
    cursor = offset + 2
    if first_len & 0x80:
        count = first_len & 0x7F
        if count == 0 or cursor + count > len(data):
            raise TlsError("malformed DER length")
        size = int.from_bytes(data[cursor : cursor + count], "big")
        cursor += count
    else:
        size = first_len
    end = cursor + size
    if end > len(data):
        raise TlsError("truncated DER value")
    return tag, cursor, end, end


def _children(data: bytes, start: int, end: int) -> list[tuple[int, int, int]]:
    """The (tag, content_start, content_end) of each TLV
    inside data[start:end]."""
    children = []
    offset = start
    while offset < end:
        tag, content_start, content_end, offset = _read_tlv(data, offset)
        children.append((tag, content_start, content_end))
    if offset != end:
        raise TlsError("DER children overrun their parent")
    return children


def _cert_parts(cert_der: bytes) -> list[tuple[int, int, int]]:
    tag, start, end, next_offset = _read_tlv(cert_der, 0)
    if tag != 0x30 or next_offset != len(cert_der):
        raise TlsError("certificate is not a single DER sequence")
    parts = _children(cert_der, start, end)
    if len(parts) != 3 or parts[0][0] != 0x30:
        raise TlsError("certificate does not carry its three fields")
    return parts


def peer_public_key(cert_der: bytes) -> bytes:
    """The Ed25519 public key inside an X.509 certificate's
    DER, read structurally: certificate → TBSCertificate →
    subjectPublicKeyInfo → the bit string's key bytes.
    This is the binding between the TLS handshake and the
    Y-011 identity layer: the key the handshake proved is
    the key the node certificate names."""
    tbs_tag, tbs_start, tbs_end = _cert_parts(cert_der)[0]
    del tbs_tag
    fields = _children(cert_der, tbs_start, tbs_end)
    # [0] version, serial, algorithm, issuer, validity,
    # subject, then subjectPublicKeyInfo.
    if len(fields) < 7 or fields[6][0] != 0x30:
        raise TlsError("certificate carries no subjectPublicKeyInfo")
    _, spki_start, spki_end = fields[6]
    spki = _children(cert_der, spki_start, spki_end)
    if len(spki) != 2 or spki[1][0] != 0x03:
        raise TlsError("certificate carries no public key bit string")
    _, key_start, key_end = spki[1]
    key_bits = cert_der[key_start:key_end]
    if not key_bits or key_bits[0] != 0:
        raise TlsError("public key bit string is not byte-aligned")
    public_key = key_bits[1:]
    if len(public_key) != 32:
        raise TlsError("public key is not a 32-byte Ed25519 key")
    return public_key


def verify_x509_signature(cert_der: bytes, issuer_public_key: bytes) -> bool:
    """True when the certificate's Ed25519 signature
    verifies over its TBSCertificate under the issuer's
    public key — the check a node runs on provisioned
    DER before trusting it as its TLS identity."""
    parts = _cert_parts(cert_der)
    _, tbs_start, _ = parts[0]
    # The TBS raw bytes begin at its own TLV header, which
    # sits just before its content: re-read from the
    # certificate's first child offset.
    cert_tag, cert_start, _, _ = _read_tlv(cert_der, 0)
    del cert_tag
    _, _, _, tbs_end_offset = _read_tlv(cert_der, cert_start)
    tbs_raw = cert_der[cert_start:tbs_end_offset]
    del tbs_start
    _, sig_start, sig_end = parts[2]
    sig_bits = cert_der[sig_start:sig_end]
    if not sig_bits or sig_bits[0] != 0:
        return False
    return ed25519.verify(issuer_public_key, tbs_raw, sig_bits[1:])


# --- The channel ----------------------------------------------------------


class TlsChannel:
    """One established TLS channel: a validated
    ``tls_channel`` v1 record plus framed JSON carriage.
    Records are length-prefixed (4-byte big-endian) and
    bounded by the policy's ``max_record_bytes`` in both
    directions — an oversize record is refused by name
    before it is sent, and a peer announcing one is
    refused before its bytes are read."""

    def __init__(self, tls_socket, record: dict, *, max_record_bytes: int):
        self._socket = tls_socket
        self.record = record
        self._max_record_bytes = max_record_bytes
        self._closed = False

    @property
    def peer_node(self) -> str:
        return self.record["peer_node"]

    def send_json(self, value) -> None:
        if self._closed:
            raise TlsError("channel is closed")
        try:
            encoded = json.dumps(value, sort_keys=True).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise TlsError(f"record is not JSON data: {exc}") from exc
        if len(encoded) > self._max_record_bytes:
            raise TlsError(
                f"record of {len(encoded)} bytes exceeds the policy bound "
                f"{self._max_record_bytes} bytes; refused, not sent"
            )
        self._socket.sendall(struct.pack(">I", len(encoded)) + encoded)

    def _read_exactly(self, size: int) -> bytes:
        chunks = []
        remaining = size
        while remaining:
            chunk = self._socket.recv(remaining)
            if not chunk:
                raise TlsError("channel closed by the peer mid-record")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def recv_json(self):
        if self._closed:
            raise TlsError("channel is closed")
        header = self._read_exactly(4)
        (size,) = struct.unpack(">I", header)
        if size > self._max_record_bytes:
            raise TlsError(
                f"peer announced a record of {size} bytes, past the policy "
                f"bound {self._max_record_bytes} bytes; refused unread"
            )
        return json.loads(self._read_exactly(size).decode("utf-8"))

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._socket.close()
            except OSError:
                pass  # closing a broken channel is not a new failure

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False


# --- One node's channel factory --------------------------------------------


class TlsNodeChannels:
    """A node's TLS door: builds the only two contexts
    that exist (mutual server, mutual client) and wraps
    caller-supplied sockets into established channels.

    Construction verifies the node's identity the way the
    node agent verifies it at start (Y-021): the Y-011
    certificate chains to the mesh root and is in window,
    the stored keypair is the certificate's key, and the
    provisioned X.509 DER binds to those same keys under
    the root's signature. Material is then written into
    ``material_dir`` — the certificate chain, and the key
    mode 0600.
    """

    def __init__(
        self,
        *,
        node_id: str,
        keypair: NodeKeypair,
        node_cert: dict,
        root_cert: dict,
        node_der: bytes,
        root_der: bytes,
        material_dir: Path,
        policy: dict | None = None,
        now=None,
    ):
        self._policy = validate_policy(
            dict(policy) if policy is not None else default_policy()
        )
        self._now = now or (lambda: datetime.now(UTC))
        if node_cert.get("node_id") != node_id:
            raise TlsError(
                f"node certificate names {node_cert.get('node_id')!r}, "
                f"not this node {node_id!r}"
            )
        try:
            verify_certificate(node_cert, root_cert)
        except IdentityError as exc:
            raise TlsError(f"node certificate does not verify: {exc}") from exc
        cert_public = base64.b64decode(node_cert["public_key"].encode("ascii"))
        if keypair.public_key != cert_public:
            raise TlsError("keypair does not match the node certificate's key")
        root_public = base64.b64decode(root_cert["public_key"].encode("ascii"))
        if peer_public_key(root_der) != root_public:
            raise TlsError("root X.509 does not carry the mesh root's Y-011 key")
        if not verify_x509_signature(root_der, root_public):
            raise TlsError("root X.509 is not signed by the mesh root's key")
        if peer_public_key(node_der) != cert_public:
            raise TlsError("node X.509 does not carry this node's Y-011 key")
        if not verify_x509_signature(node_der, root_public):
            raise TlsError("node X.509 is not signed by the mesh root's key")
        self.node_id = node_id
        self._root_cert = root_cert
        self._root_der = root_der
        self._material_dir = Path(material_dir)
        self._material_dir.mkdir(parents=True, exist_ok=True)
        self._cert_path = self._material_dir / f"{node_id}.tls.crt.pem"
        self._key_path = self._material_dir / f"{node_id}.tls.key.pem"
        self._cert_path.write_text(
            certificate_pem(node_der) + certificate_pem(root_der)
        )
        fd = os.open(self._key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(private_key_pem(keypair))
        os.chmod(self._key_path, 0o600)
        self._server_context: ssl.SSLContext | None = None
        self._client_context: ssl.SSLContext | None = None
        self._lock = threading.RLock()
        self._established = 0
        self._refusals = 0
        self._refusals_by_reason: dict[str, int] = {}
        self._peers: set[str] = set()
        self._events: list[dict] = []
        self._event_seq = 0

    @classmethod
    def derive(
        cls,
        *,
        root_keypair: NodeKeypair,
        **kwargs,
    ) -> "TlsNodeChannels":
        """Bootstrap derivation: the root key's holder
        mints the X.509 DER from the Y-011 certificates
        and constructs the node's door in one act. Nodes
        provisioned by someone else are constructed
        directly with the derived DER."""
        root_cert = kwargs["root_cert"]
        node_der = node_x509_der(root_keypair, root_cert, kwargs["node_cert"])
        root_der = mesh_root_x509_der(root_keypair, root_cert)
        return cls(node_der=node_der, root_der=root_der, **kwargs)

    # -- events -------------------------------------------------

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

    def _refuse(self, peer: str, reason: str, detail: str) -> TlsError:
        with self._lock:
            self._refusals += 1
            self._refusals_by_reason[reason] = (
                self._refusals_by_reason.get(reason, 0) + 1
            )
            self._emit(
                "fabric.tls_refused",
                {"node": self.node_id, "peer": peer, "reason": reason},
            )
        return TlsError(detail)

    # -- contexts -------------------------------------------------

    def _base_context(self, protocol) -> ssl.SSLContext:
        context = ssl.SSLContext(protocol)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cadata=certificate_pem(self._root_der))
        context.load_cert_chain(
            certfile=str(self._cert_path), keyfile=str(self._key_path)
        )
        return context

    def server_context(self) -> ssl.SSLContext:
        """The mutual-TLS server context: TLS 1.3 floor, a
        peer certificate required and chained to the mesh
        root. The only server context this module builds."""
        if self._server_context is None:
            self._server_context = self._base_context(ssl.PROTOCOL_TLS_SERVER)
        return self._server_context

    def client_context(self) -> ssl.SSLContext:
        """The mutual-TLS client context: the same floor
        and the same demand made of the server. Hostname
        checking is off because mesh identity is the
        certificate's node name, not a DNS name — the peer
        binding is checked against Y-011 after the
        handshake, by key."""
        if self._client_context is None:
            context = self._base_context(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            self._client_context = context
        return self._client_context

    # -- establishment ---------------------------------------------

    def _check_peer_cert(self, peer_cert: dict, peer_label: str) -> bytes:
        """A peer's Y-011 certificate must chain to this
        node's mesh root and be in window; returns its
        public key."""
        try:
            verify_certificate(peer_cert, self._root_cert)
        except IdentityError as exc:
            raise self._refuse(
                peer_label,
                "peer_certificate_invalid",
                f"peer certificate for {peer_label} does not verify: {exc}",
            ) from exc
        return base64.b64decode(peer_cert["public_key"].encode("ascii"))

    def _wrap(self, raw_socket, *, server_side: bool, resolve_peer, peer_label: str):
        context = self.server_context() if server_side else self.client_context()
        try:
            tls_socket = context.wrap_socket(raw_socket, server_side=server_side)
        except (ssl.SSLError, OSError) as exc:
            raise self._refuse(
                peer_label,
                "handshake_failed",
                f"TLS handshake with {peer_label} failed: {exc}",
            ) from exc
        peer_der = tls_socket.getpeercert(binary_form=True)
        if not peer_der:
            tls_socket.close()
            raise self._refuse(
                peer_label,
                "peer_certificate_missing",
                f"{peer_label} presented no certificate",
            )
        try:
            handshake_key = peer_public_key(peer_der)
        except TlsError as exc:
            tls_socket.close()
            raise self._refuse(peer_label, "peer_certificate_invalid", str(exc)) from exc
        peer_node = resolve_peer(handshake_key)
        if peer_node is None:
            tls_socket.close()
            raise self._refuse(
                peer_label,
                "peer_mismatch",
                f"the handshake key does not belong to {peer_label}",
            )
        tls_version = tls_socket.version() or ""
        if tls_version != "TLSv1.3":
            tls_socket.close()
            raise self._refuse(
                peer_node,
                "tls_version_refused",
                f"negotiated {tls_version or 'no version'}, below the "
                "policy floor TLS 1.3",
            )
        cipher = tls_socket.cipher()
        record = validate_channel(
            {
                "version": 1,
                "local_node": self.node_id,
                "peer_node": peer_node,
                "peer_fingerprint": fingerprint(handshake_key),
                "role": "server" if server_side else "client",
                "tls_version": tls_version,
                "cipher": cipher[0] if cipher else "unknown",
                "handshake": "completed",
                "simulated": False,
                "established_at": _fmt(self._now()),
            }
        )
        with self._lock:
            self._established += 1
            self._peers.add(peer_node)
            self._emit(
                "fabric.tls_channel_established",
                {
                    "node": self.node_id,
                    "peer": peer_node,
                    "tls_version": tls_version,
                    "cipher": record["cipher"],
                },
            )
        return TlsChannel(
            tls_socket,
            record,
            max_record_bytes=self._policy["max_record_bytes"],
        )

    def wrap_client(self, raw_socket, *, peer_cert: dict) -> TlsChannel:
        """Open a channel to one named peer: its Y-011
        certificate is verified first, the handshake must
        chain to the same root, and the key the handshake
        proves must be that certificate's key."""
        peer_node = peer_cert.get("node_id", "unknown")
        expected_key = self._check_peer_cert(peer_cert, peer_node)

        def resolve(handshake_key: bytes):
            return peer_node if handshake_key == expected_key else None

        return self._wrap(
            raw_socket, server_side=False, resolve_peer=resolve, peer_label=peer_node
        )

    def wrap_server(self, raw_socket, *, peer_certs: dict) -> TlsChannel:
        """Accept a channel from any mesh peer whose Y-011
        certificate the caller vouches for in
        ``peer_certs`` (node_id → certificate): every
        candidate is verified against the root, and the
        handshake key must belong to one of them — an
        unknown key, however well its X.509 chains, is a
        peer this node was not told to trust by name."""
        candidates: dict[bytes, str] = {}
        for node_id, cert in sorted(peer_certs.items()):
            if cert.get("node_id") != node_id:
                raise self._refuse(
                    node_id,
                    "peer_certificate_invalid",
                    f"peer certificate filed under {node_id} names "
                    f"{cert.get('node_id')!r}",
                )
            candidates[self._check_peer_cert(cert, node_id)] = node_id

        def resolve(handshake_key: bytes):
            return candidates.get(handshake_key)

        return self._wrap(
            raw_socket,
            server_side=True,
            resolve_peer=resolve,
            peer_label="unknown peer",
        )

    # -- report ------------------------------------------------------

    def report(self) -> dict:
        """The node's counted channel state as a validated
        ``tls_report`` v1 — names and counts only."""
        with self._lock:
            return validate_report(
                {
                    "version": 1,
                    "at": _fmt(self._now()),
                    "node": self.node_id,
                    "channels_established": self._established,
                    "refusals": self._refusals,
                    "refusals_by_reason": dict(self._refusals_by_reason),
                    "peers": sorted(self._peers),
                }
            )
