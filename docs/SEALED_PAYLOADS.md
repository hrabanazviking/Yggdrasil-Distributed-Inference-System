# Encrypted Payloads (Y-070)

Y-051 seals the *channel*: every fabric connection
is mutual TLS, so a stranger on the wire reads
nothing. But a channel ends at the node that
terminates it — a task offered on the fabric,
relayed, claimed, or held store-and-forward
(Y-061) can be read by every hand it passes
through. This slice seals the *content* itself:
where a task's policy demands it, the payload
travels sealed to the executing node alone, as one
judged door kept by `PayloadSealer` in
`src/yggdrasil/sealed_payloads.py`.

## The law is constants

The `sealed_payload_policy` v1 fixes
`plaintext_for_sensitive_allowed: false` and one
named algorithm, `x25519-hkdf-sha256-hmac-v1`, as
schema constants. Which kinds are sensitive is the
operator's list (the default names `chat.private`,
`health.summary`, and `personal.memory`), and
`max_payload_bytes` (default 262,144 — the fabric's
own bound, so anything the fabric can carry can be
sealed), `max_sealed` (default 1,024), and
`max_nodes` (default 32) bound the door.

`judge_plain` is the door plain carriage must pass:

- a non-sensitive kind is accepted for plain
  carriage;
- a sensitive kind is refused `sealing_required` —
  counted, never quietly carried in the clear.

## Sealing is an ephemeral exchange

Each node enrols a **sealing keypair** — X25519,
deliberately distinct from its Y-011 Ed25519
signing identity, because agreeing a secret and
signing a statement are different acts with
different keys. Only the public half ever enters
the door; a node's key stands in the report only
as its SHA-256 fingerprint.

Each seal generates a fresh **ephemeral** keypair
and agrees a shared secret with the recipient's
public key. HKDF-SHA256 (salted by the seal's
16-byte nonce, the envelope header bound into the
info) separates an encryption key from a MAC key.
The payload — a Y-007 task's `payload`, validated
whole first, carried as its canonical JSON — is
encrypted with an HMAC-SHA256 counter keystream,
and the header + ciphertext are authenticated
encrypt-then-MAC. Two seals of one payload share
nothing but the payload: ephemeral key, nonce, and
ciphertext all differ. A task already sealed is
refused `duplicate_task`; sealing to oneself
`self_sealing`; a kind the policy does not name
sensitive `kind_not_sensitive`; an oversized
payload `payload_too_large`; unknown doors,
malformed keys, malformed tasks, duplicate and
full enrolments are all refused by name and
counted apart.

The sealed envelope (`sealed_payload_envelope`
v1) carries the header — task id, kind, both node
ids, the ephemeral public key, the nonce, the
plaintext's byte count, the seal time — in the
clear as metadata, and the content only as
ciphertext. No plaintext, no payload hash, and no
key material ever appears in an envelope, a
record, a report, or an event.

## Only the executing node can open

Opening is a standalone act — `open_sealed`, in the
executing node's own hands, with its own private
key, which never enters the door and is never
stored. It re-derives the shared secret from that
private key and the envelope's ephemeral public
key, verifies the MAC with a constant-time compare
**before** a single byte is decrypted, and only
then recovers the payload whole and exact.

- A wrong key — a third node's, or the sender's —
  fails `mac_invalid` and returns nothing.
- A flipped ciphertext byte fails the same way.
- A tampered header field (either end, the kind)
  fails the same way: the header is inside the MAC.
- An envelope addressed to another node can be
  refused `recipient_mismatch` before any key work;
  a small-order ephemeral key that would agree an
  all-zero secret is refused `invalid_key`.

The envelope is a plain JSON dict, so Y-050's
fabric carries it as an ordinary direct payload —
the suite proves the full road without rewiring:
seal → fabric → the recipient's inbox → open, with
the fabric's own report never holding the
plaintext.

## Honesty

Everything here is SIMULATED: `simulated: true` is
a schema constant on the envelope, the record, and
the report. The sealing construction itself is
real and proven in-process against the RFC 7748
test vectors — it is not simulated — but it is
pure Python in this module's own code (zero native
dependencies, Y-011's precedent), **not**
constant-time, and must never be presented as
side-channel resistant or externally audited. If a
measured need for hardened sealing appears, a
compiled path replaces this module behind its
interface, as ADR-001 governs for signing. No
socket was opened and no LAN was crossed in this
slice (real carriage was Y-062's own proof). An
envelope's header travels in the clear, so who
sent what kind of task to whom, and how large it
was, remains visible metadata — traffic analysis
(threat T-06's other half) is not answered here
and is not claimed.

## Not built here

- The audit tap — an operator-consented, read-only
  feed of fabric events, off by default — is built
  at Y-071 (`docs/AUDIT_TAP.md`).
- No node-agent seam: as with the Part III slices
  before it, the agent gains no sealing seam in
  this slice; deployment wiring enrols the nodes it
  starts and seals the sensitive tasks it offers.
