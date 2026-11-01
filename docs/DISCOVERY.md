# Discovery — how nodes find each other (Y-052)

Part III's third slice, in `src/yggdrasil/discovery.py`.
The roadmap's sentence: *nodes find each other on a LAN
(mDNS + a seed list) without any cloud rendezvous.* The
last clause is not a preference — it is schema law. The
`discovery_policy` v1 carries `cloud_rendezvous: false`
and `require_certificate: true` as **constants**: a
policy that opened either door fails validation.

## The two roads in

- **mDNS-shaped announcements.** A node announces
  itself with a validated `discovery_announcement` v1:
  its node id, device class and agent version, the TLS
  endpoint it may be reached at, a per-node rising
  sequence — and its whole Y-011 node certificate,
  carried as the hearer's evidence. Peers broadcast
  announcements and answer queries with fresh ones.
- **A seed list.** The operator names seed endpoints
  (host + port) — machines likely to be up, learned out
  of band. `discover_via_seeds(seeds, fetch)` dials each
  seed through the caller's `fetch(host, port)` (on a
  real LAN: a Y-051 TLS handshake asking for the node's
  announcement) and hears the answer through exactly
  the same door. An unreachable seed is counted in
  `seed_unreachable` and recorded as a
  `discovery.seed_unreachable` event — a dead seed is
  never silently skipped and never manufactures a peer.

## The one door of trust

An announcement becomes a `discovery_peer` v1 entry
only when:

1. its certificate **verifies against the mesh root**
   (Y-011 — chain, signature, validity window), and
2. the certificate **agrees with the announcement's own
   identity fields** — node id, device class, agent
   version, and the fingerprint of the certificate's
   key.

Every refusal is named, counted in the report's
`refused_reasons`, and emitted as a
`discovery.announcement_refused` event:
`malformed_announcement`, `own_announcement` (a node
never discovers itself), `certificate_invalid` (foreign
mesh, expired, badly signed), `identity_mismatch`,
`stale_sequence`, `peer_table_full`. Discovery yields
no plaintext endpoint, ever: an announcement's
transport is the schema constant `tls`.

## Freshness — the hearer's own clock

An entry is born when an announcement is **heard**,
refreshed by a repeat (same sequence) or newer
(sequence risen) announcement, and expires
`announcement_ttl_seconds` after it was last heard —
all on the hearing node's clock, because a peer's
clock is not evidence (clock discipline is Y-068's
subject). A quiet peer ages out; its expiry is a
`discovery.peer_expired` event, not a silence. The
table is bounded by the policy's `max_peers`: expired
entries are evicted first, and a fresh peer is never
evicted to make room for a new one — the new
announcement is refused `peer_table_full` by name.

`report()` returns the validated `discovery_report`
v1: the fresh known peers and every count — heard,
accepted, refused and why, expired, mDNS queries,
seed queries, seed unreachable. Names and counts
only, never payload content. Events are Y-013
envelopes from the `fabric` component.

## Honesty

The LAN in this slice is `SimulatedLan`, an in-process
model: attached services answer queries and broadcasts
with their real validated announcements and hear
through the real Y-011 verification — but no multicast
packet is sent, no socket is opened, and no LAN is
crossed. Announcements and reports carry `simulated:
true` as a schema constant. What is proven is the
discovery *rules*; a deployment transport that sends
and receives these same announcements over real mDNS
slots behind the same `DiscoveryService` shape.

## Not built here

- Real mDNS / real-LAN carriage is Y-062 (Pi + laptop
  + VM over the home network, discovery unaided);
  hardware proof is Part VIII's.
- The registry service is built at Y-053
  (`docs/REGISTRY_SERVICE.md`): nodes register census +
  models at any replica, and every replica holds the
  whole truth; heartbeats and present → suspect → gone
  liveness ageing are built at Y-054
  (`docs/LIVENESS.md`) — discovery expiry is *table*
  freshness, not the liveness verdict — and epidemic
  gossip of that registry and health state is built at
  Y-055 (`docs/GOSSIP.md`): the peers discovery finds
  are the peers gossip exchanges with, when real
  carriage (Y-062) wires the two together.
- No discovery seam on the node agent: as with the
  fabric (Y-050), the agent gained its peers' words at
  Y-054 as the *liveness* seam (`liveness_tracker=`).
  Components use `DiscoveryService` directly until
  real carriage at Y-062.
- Certificate revocation is still not built (Y-011's
  named residual): an expired certificate is refused,
  a revoked-but-in-window one is not yet detectable.
- Message-level signing of announcements is Y-059's
  road; here the carried certificate *is* the
  announcement's proof, verified at the door.
