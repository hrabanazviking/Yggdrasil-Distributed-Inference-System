# Identity Foundation — Y-011

*The mesh's answer to threat T-01 (node impersonation): a node is
its key, not its claimed id. Built local-scale first — this is the
seed of WorldNet section 3 named in the concordance; that section's
global machinery (hardware attestation, W3C DIDs, DHT registration,
proof-of-useful-work) is Y-211 research, NOT built or claimed here.*

## The pieces

- **Per-node keypairs** (`src/yggdrasil/identity.py`,
  `src/yggdrasil/ed25519.py`): every node holds an Ed25519 keypair.
  The public key is the node's name in the mesh; the private seed
  never appears in any certificate or identity document (tested).
- **The mesh root**: at bootstrap the operator creates one root
  keypair whose self-signed certificate (`kind: mesh_root`,
  node_id `mesh-root`) is the trust anchor every node carries.
  There is exactly one root per mesh, and it is the operator's —
  whoever holds the root key *is* the issuing authority, so its
  custody is the operator's first duty (threat T-10).
- **Node certificates** (`contracts/schemas/node_certificate.schema.json`):
  the root signs a binding of node_id → public key, with device
  class, agent version, serial, and a validity window.
  `verify_certificate` re-checks the whole chain on every use:
  schema, issuer binding, issuer fingerprint (sha256 of the issuer
  key), Ed25519 signature over the canonical body, and the window
  (not-yet-valid and expired both refuse).
- **Identity from certificate**: `certificate_identity` projects a
  verified node certificate into the Y-006 `NodeIdentity` contract
  shape, so the rest of the tree speaks one identity language.

## Key custody

Private seeds sit behind a key-store interface. The local store
implemented here writes seeds mode `0600`, refuses to overwrite an
existing key, and refuses malformed seeds. Where a node supports
Kista, custody plugs into the same interface — that integration is
Y-196. Until it lands, `describe_custody()` reports the backend in
plain words and a node on the local store *says so*, per threat
T-10's terms: no node may silently pretend to stronger custody
than it has.

## Honest limits

- The Ed25519 implementation is pure Python (RFC 8032), proven
  against the RFC's published test vectors. It is **not**
  constant-time and claims no side-channel resistance; signing is
  join-time work, and ADR-001's compiled-path criteria govern any
  future replacement behind the same interface.
- Certificates expire, but **revocation is not built**: a stolen
  node key impersonates that node until its certificate lapses or
  the operator re-roots the mesh. That residual risk is T-01's own
  words in the threat register and stands until a later slice
  builds revocation.
- Sybil resistance at this scale rests on issuance discipline:
  only the operator's root issues certificates, so a key the root
  never signed is simply not a citizen.
