# The Fabric Core (Y-050)

Part III opens on the transport ADR-002 chose at Y-049: the
**minimal signed gossip** — one direct hop, a store-and-forward
outbox as its persistence, no broker, no extra service. The fabric
core (`src/yggdrasil/fabric.py`) is that transport as the one API
every other component speaks to the mesh through.

## The one API

A `Fabric` is the mesh medium. Nodes `attach(node_id, handler=None)`
and `detach(node_id)`; every other verb is one of two shapes:

- **Publish/subscribe** — `publish(from_node, topic, payload)`
  carries one message to every current subscriber of the topic;
  `subscribe` / `unsubscribe` manage a node's topics. Topics are
  dotted lowercase names (`registry.updates`, `tasks.offers`). A
  node subscribed to a topic it publishes to receives its own
  publication, as on any pub/sub fabric.
- **Direct channels** — `send(from_node, to_node, payload)` carries
  one message to one named node.

Delivery reaches a node's handler if it has one, else its `inbox`
(read with `inbox(node_id)`, which drains it). Every message is a
validated `fabric_message` v1: per-sender `seq` rising in order,
an injectable clock's stamp, a JSON-copied payload (a sender
mutating its dict afterwards cannot rewrite a carried message),
and the policy's size bound enforced before anything travels —
non-JSON and oversize payloads are refused by name.

## Delivery rules — the ones Y-049 benchmarked

- Reachable and attached: delivered at once.
- Detached, never-attached, or cut off by a partition: **held in
  the sender's store-and-forward outbox** — direct messages
  whole, one held copy per unreachable subscriber for a
  publication — and flushed on attach, on `heal()`, or on an
  explicit `flush(node_id)`. Nothing is silently dropped: a full
  outbox (the `fabric_policy` v1 bound, as data) refuses the
  *new* message by name, and a publication that would overflow
  the outbox is refused whole before anything is delivered.
- A partition is modelled in the open: `cut(nodes)` splits the
  named nodes away (each side still delivers within itself),
  `heal()` reunites the mesh and flushes every outbox across
  the seam.
- A handler that raises fails that one delivery: counted in the
  report's `failed_deliveries`, recorded as a
  `fabric.delivery_failed` Y-013 event with the reason named,
  the rest of a topic still served — and for a direct message,
  returned to the outbox for a later flush rather than lost.

## Signing

The chosen transport is *signed* gossip, and the seam is here
from the first slice: pass a `signer` and every message carries
its signature over `message_digest()` — the SHA-256 of the
message's own canonical fields (the signature never signs
itself). Pass a `verifier` and a message whose signature is
missing or fails is a failed delivery named
`signature_invalid`, held for direct retry, never quietly
carried. The keys are Y-011's; this module holds no key material
and mints no trust.

## Counted state

`report()` answers a validated `fabric_report` v1: attached
nodes, per-topic subscriber counts, and the message totals —
published, sent direct, delivered, currently queued,
failed — plus each sender's outbox depth and whether the mesh
stands partitioned. Lifecycle and trouble are Y-013 events from
the `fabric` component (`node_attached`, `node_detached`,
`message_queued`, `outbox_flushed`, `delivery_failed`), names
and counts only, never payload content.

## Honesty

Everything here is **SIMULATED** — `simulated: true` and
`transport: minimal-gossip` are schema constants. The fabric is
an in-process model of the chosen transport: no socket is
opened, no LAN is crossed, and no delivery here proves anything
about a real network. What is proven is the API's shape and the
transport's delivery rules, exactly as Y-049 benchmarked them.

## Not built here

- TLS carriage of these channels, with Y-011 node certificates
  on every connection and no plaintext mode, is built at
  Y-051 (`docs/TLS_CHANNELS.md`): real mutual TLS 1.3 with
  the peer binding checked against Y-011 by key.
- Discovery (mDNS + seed list, no cloud rendezvous) is
  built at Y-052 (`docs/DISCOVERY.md`): nodes announce
  with their Y-011 certificates and are admitted only
  when the certificate verifies. Nodes here still attach
  by name the caller already knows — discovery feeds
  the registry service at Y-053, not this core.
- The registry service riding these topics is Y-053,
  heartbeats and liveness Y-054, epidemic gossip of registry
  and health state Y-055, and the CRDT stores of ADR-003
  Y-056.
- No node-agent seam: the agent joins the fabric when the
  registry and heartbeat slices give it something to say
  (Y-053/Y-054). Components use `Fabric` directly until then.
- The real wire — sockets under TLS between machines — is
  carried by Y-051's TLS channels and proven on the real
  LAN at Y-062 and on hardware in Part VIII; the
  losers of Y-049's race stay swappable through the Y-069
  fabric conformance kit.
