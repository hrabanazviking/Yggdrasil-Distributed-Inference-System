# Message Provenance (Y-059)

The fabric (Y-050) was built signed from its first
slice — a signer seam, a verifier seam, a digest for
signatures to cover. What it did not hold is the law
the threat model names (T-01 node impersonation,
T-03 gossip spam, T-09 replay): *every message
signed by its node; unsigned or wrongly-signed
traffic dropped and counted.* The provenance gate
(`src/yggdrasil/message_provenance.py`) is that law,
as a door. (Licence provenance — the register of
what the code is made of — is Y-018's separate
subject, in `docs/PROVENANCE.md`.)

## Enrolment is certificate-gated

A node joins the gate with its Y-011 node
certificate. The gate verifies the certificate
against the mesh root — chain, issuer fingerprint,
signature, validity window, Y-011's own checks —
and binds the node id to the certificate's public
key and its fingerprint
(`provenance.node_enrolled` events record each
join). Refused by name:

- `node_mismatch` — the certificate names another
  node;
- `certificate_invalid` — foreign root, broken
  signature, or outside its validity window (an
  expired certificate is refused);
- `key_mismatch` — a handed-in keypair that is not
  the certificate's own key;
- `already_enrolled` — the node already stands at
  the door.

Enrolment with the node's keypair lets the gate
sign for it. Enrolment without one is **verify-only**:
the gate can judge that node's traffic but can
never speak in its name (`cannot_sign`).

## Signing is real

The gate fills Y-050's seam with Y-011's keys: a
signature is the enrolled key's Ed25519 signature
over the UTF-8 bytes of the fabric message digest
— SHA-256 over the message's own canonical fields
(`message_digest`, Y-050; the signature never
signs itself). `gate.fabric()` returns a fabric
already wired with the gate's signer and verifier,
so ordinary traffic is signed at birth, and a
delivery whose signature is missing or wrong fails
at the fabric under its own name
(`signature_invalid`) — held for direct retry
there, as Y-050 holds every failed delivery.

## The door drops and counts

Raw traffic — any message dict, however it
arrived — is judged by `admit(message)`, in this
order, and the judgement is a validated
`message_provenance_record` v1:

1. fails the `fabric_message` v1 schema →
   dropped `malformed`;
2. carries no signature → dropped `unsigned`;
3. names a sender that is not enrolled →
   dropped `unknown_signer` (an honest signature
   from a stranger's own key proves nothing — the
   mesh's root never bound that key to that name);
4. the signature is not the enrolled key's
   signature over this exact message's digest →
   dropped `signature_invalid` (forged bytes,
   any signed field tampered with — payload,
   sender, sequence — or another node's signature
   presented in this sender's name);
5. verifies, but its per-sender sequence is not
   beyond the last sequence this door verified
   from that sender → dropped `replayed`.

What passes is `verified`: signed by the key the
mesh's own root bound to the named sender, over
exactly these fields, in sequence — and it
advances that sender's watermark. A failed forgery
never advances the watermark, however high a
sequence it claims, so a forgery cannot lock the
true sender out. `require_verified(message)` is
the same door for callers that want a drop raised
as a `ProvenanceError` carrying the reason.

Dropped traffic is delivered nowhere; the record
is all that crosses. `report()` answers a
validated `message_provenance_report` v1 — the
policy, every enrolled node with its fingerprint
and signing standing, every admission record in
order, and the totals: seen, verified, dropped,
with drops counted apart by reason. Events are
Y-013 envelopes from the `fabric` component
(`provenance.node_enrolled`,
`provenance.message_verified`,
`provenance.message_dropped`) — names, ids, and
counts only, never payload content.

## The law as data

`message_provenance_policy` v1 carries the
constants as schema law: `signing_required` is
always `true`; `unsigned_allowed`,
`wrongly_signed_allowed`, and `replay_allowed`
are always `false`; the algorithm is `ed25519`
and the digest `sha256`. A policy naming any
weaker law fails validation.

## Honesty

The cryptography is the real thing — Y-011's
pure-Python Ed25519, proven here against fixed
test seeds and verified by hand in the suite; it
is NOT constant-time, as Y-011 states. The gate
itself is an in-process door: `simulated: true`
is a schema constant on its report, no socket is
opened, and no LAN is crossed. What is proven is
the provenance law on the contract shapes.

## Not built here

- Per-node rate and size quotas — a flooding node
  throttled by its peers — are built at Y-060
  (`docs/RATE_DISCIPLINE.md`); this door
  judges *whose* traffic is real, Y-060 rations
  *how much* of it rides.
- Store-and-forward for a sleeping node, with a
  TTL, is built at Y-061
  (`docs/STORE_AND_FORWARD.md`); real carriage on
  the home LAN —
  this gate's door on a real wire — is Y-062;
  hardware proof is Part VIII's.
- Certificate revocation is still not built
  (Y-011's standing residual): a stolen node key
  signs as that node until its certificate
  expires or the mesh re-roots.
- No node-agent seam: as with the registry
  (Y-053), gossip (Y-055), CRDTs (Y-056), the
  task channel (Y-057), and partitions (Y-058),
  the agent gains provenanced traffic at real
  carriage (Y-062), not before.
