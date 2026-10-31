# TLS Everywhere (Y-051)

The fabric's carriage layer (`src/yggdrasil/tls_channels.py`):
every channel between nodes is **mutual TLS 1.3, carrying
Y-011 node certificates, with no plaintext mode in
existence**. This is the control the threat model named
for node impersonation (T-01) and eavesdropping (T-06).

## One identity, two encodings

A node's TLS identity *is* its Y-011 identity — there is
no second key and no second trust root:

- At bootstrap, whoever holds the mesh root's key derives
  X.509 certificates from the Y-011 certificates, in pure
  Python DER signed with the tree's own Ed25519: the
  root's X.509 is a CA certificate, each node's is a leaf
  (server + client authentication), carrying the node's
  Y-011 public key, its serial, and its exact validity
  window. Issuance refuses any identity Y-011 itself
  refuses (wrong root, expired, mismatched key).
- A node is constructed with the derived public material
  (the two certificates' DER), its Y-011 certificate
  pair, and its own keypair — **never the root key**. It
  verifies the DER before use: the certificates chain to
  the root in Y-011, the stored key is the certificate's
  key, the X.509s carry those same keys, and their
  signatures verify under the root's key.
- The node's private key reaches disk only as a PKCS#8
  file in its own material directory, mode 0600, derived
  from its Y-011 seed (the seed itself is never written).

## The rules are constants

`tls_policy` v1 is mostly constants, on purpose:
`protocol` is `TLS`, `minimum_version` is `1.3`,
`peer_certificate_required` is `true`, and
`plaintext_allowed` is `false` — a caller-supplied policy
that tries to open a plaintext or anonymous channel fails
its schema. The module builds exactly two contexts
(mutual server, mutual client), both `CERT_REQUIRED`
under a TLS 1.3 floor, and exposes no other door.

Establishment, both directions:

1. The peer's Y-011 certificate is verified against the
   mesh root (chain, fingerprint, window) — for a server,
   every vouched candidate in `peer_certs`.
2. OpenSSL runs the handshake: the peer's X.509 must
   chain to the mesh root. A plaintext speaker, a peer
   capped below TLS 1.3, or another mesh's node fails
   here, in the handshake itself.
3. The public key read out of the handshake certificate
   must be the expected Y-011 certificate's key (client
   side) or one of the vouched candidates' keys (server
   side). A genuine mesh member the door was not told to
   trust by name is refused here, as `peer_mismatch`.

Every refusal is counted in the `tls_report` v1 by reason
(`handshake_failed`, `peer_certificate_invalid`,
`peer_mismatch`, …), recorded as a `fabric.tls_refused`
Y-013 event, and raised by name — never a silent fallback
to anything weaker, because there is nothing weaker to
fall back to. Every established channel is a validated
`tls_channel` v1 record: both node names, the peer's
Y-011 fingerprint, and the TLS version and cipher the
handshake itself reported.

## Carriage

A `TlsChannel` carries length-prefixed JSON records,
bounded by the policy's `max_record_bytes` in both
directions (an oversize record is refused before it is
sent; a peer announcing one is refused before its bytes
are read). Fabric messages ride it unchanged — the suite
publishes through the Y-050 fabric and carries the exact
validated message across. Sockets are the caller's: the
module wraps a connected socket handed to it, so no
socket client enters `src/` and the gates stay green.

## Honesty

The handshakes proven here are **real** — OpenSSL TLS
1.3, mutually authenticated, certificates chained and
verified by the platform TLS stack — over an in-process
socketpair, so channel records carry `simulated: false`.
That proves the cryptography, the identity binding, and
the refusal behaviour. It does **not** prove carriage
across a real LAN between machines: that is Y-062, and
Part VIII takes the mesh to hardware. The Ed25519
signing underneath is Y-011's pure-Python implementation
with its stated not-constant-time limit.

## Not built here

- Discovery is Y-052: channels here are opened to peers
  the caller already names, over sockets the caller
  already holds.
- Wiring the node agent to open TLS channels itself
  arrives with the registry and heartbeat slices that
  give it fabric traffic of its own (Y-053/Y-054).
- Message-level signing of every fabric message is
  Y-059 (the seam is Y-050's); TLS here authenticates
  the *channel* and both its ends.
- Certificate revocation is still not built (Y-011's
  standing residual): a stolen node key impersonates
  that node until its certificate expires or the mesh
  re-roots.
