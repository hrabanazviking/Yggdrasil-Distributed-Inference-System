# The Fabric on the Real LAN — Y-062

*Part III's carriage slice: the pieces built in
Y-050–Y-061 stand up on real machines, over a real
network, TLS on, discovery unaided. The composition
lives in `src/yggdrasil/lan_fabric.py`; the wire
itself — sockets, dial plans, the run — is deployment
wiring in `deploy/lan_node.py`, because this tree's
standing rule is that `src/` holds no socket of its
own.*

## The composition

A `LanNode` holds exactly one of each piece, each
constructed by its own slice's rules and none of
them rewired: a Y-051 `TlsNodeChannels` door, a
Y-052 `DiscoveryService`, and a Y-050 `Fabric`. This
slice mints no key, weakens no check, and restamps
no other module's record — a fabric message is built
by the fabric itself, an announcement is built and
judged by discovery, a channel is established by the
TLS door.

A session (`LanNode.open_client` / `open_server`,
over a connected socket the caller supplies) is
discovery first, then traffic:

1. Mutual TLS 1.3 through the door — Y-011
   certificates both ways, a peer the node was not
   told to trust by name refused at the handshake.
2. Announcements exchanged *on the channel*: each
   side's Y-052 announcement is heard by the other
   through discovery's one door of trust. An
   announcement naming anyone but the channel's
   proven peer refuses the session
   (`announcement_mismatch`).
3. Y-050 fabric messages cross whole and exact as
   records on the channel. Reception is judged: a
   message naming a sender the channel did not prove
   is refused (`sender_mismatch`), a direct message
   for another node is refused (`not_for_me`), and
   every refusal is counted and evented.

A run is judged as a whole by `evaluate_lan_run`:
each established channel is reported as a
`lan_leg` v1 (the pair, the path — `lan`, `tailnet`,
or `loopback` — the discovery road, the TLS version
and cipher the handshake reported, the fingerprint
each side read out of the other's handshake
certificate, whether announcements were exchanged,
and the messages carried in each direction), and
judged against a `lan_policy` v1: TLS only at a 1.3
floor, `plaintext_allowed` always false,
`cloud_rendezvous` always false, discovery only by
seed or mDNS. The `lan_report` v1 verdict is
`proven` only when every required pair stands on
real carriage with fingerprints matching the
enrolled Y-011 certificates and traffic in both
directions; `simulated_only` for the same shape over
stand-in carriage; `incomplete` with every refusal
counted by name otherwise.

## The machines, measured

Read directly on 2026-11-02 before the run:

| Citizen | Machine | Address on the home LAN | Tailnet address | Python / OpenSSL |
|---|---|---|---|---|
| `mimir-1` | Raspberry Pi 5 (purpleviking1) | 192.168.50.66 | 100.101.39.30 | 3.11.2 / 3.0.22 |
| `purplelongship-1` | the laptop | 192.168.50.40 | 100.67.240.22 | 3.14.4 / 3.5.5 |
| `htch-runtime-1` | this VM | — none — | (via the runtime egress proxy) | 3.12 / — |

The VM has no LAN interface at all (its only
interface is the runtime's NAT address), so its two
legs necessarily ride the tailnet through the egress
proxy's CONNECT tunnel and are recorded as
`tailnet` legs — real carriage between real
machines, honestly *not* the home LAN segment. The
Pi↔laptop leg is the home LAN segment itself
(192.168.50.0/24, sub-millisecond ping both ways,
measured before the run).

## The run, 2026-11-02 ~09:03 UTC

Provisioned from this VM (the bootstrap act: mesh
root created, one Y-011 certificate per citizen,
Y-051 material derived); each node's bundle — its
own seed, its certificates, its dial plan — travelled
to its own machine only, over the same tunnel/HTTP
roads, every transfer sha256-verified. All three
nodes then ran concurrently: the laptop dialled the
Pi over the LAN, the VM dialled both citizens over
the tailnet; each node also fired one plaintext probe
at a peer mid-run.

| Leg | Path | TLS | Messages | Announcements |
|---|---|---|---|---|
| purplelongship-1 ↔ mimir-1 | **lan** | TLSv1.3, TLS_AES_256_GCM_SHA384 | 3 + 3 | exchanged |
| htch-runtime-1 ↔ mimir-1 | tailnet | TLSv1.3, TLS_AES_256_GCM_SHA384 | 3 + 3 | exchanged |
| htch-runtime-1 ↔ purplelongship-1 | tailnet | TLSv1.3, TLS_AES_256_GCM_SHA384 | 3 + 3 | exchanged |

Every node's discovery table ended holding both
peers; every server's session counters mirrored its
client's exactly (`mirror_ok` on all three pairs);
18 fabric messages crossed in total. **Judged on the
Pi from the three nodes' own result records:
`lan_report` verdict `proven` — 3 of 3 pairs, 0
refusals, `simulated: false`.** (Evidence:
`~/workspace/.verify-tmp/y062-evidence/` — the judge
output byte-identical by sha256 to the Pi's own
`~/yggdrasil-lan/judge.json`, the Pi's and the VM's
result records beside it; the laptop's result record
stands on the laptop and on the Pi as
`laptop_result.json`, sha256-verified at transfer.)

The plaintext probes were refused everywhere they
landed: the Pi's TLS door counted **2**
`handshake_failed` refusals (the laptop's direct
probe and the VM's tunnelled probe), the laptop's
door counted **1** (the Pi's probe, which was
answered with a 7-byte TLS alert record and a closed
connection — no plaintext service exists to answer).

Two runner bugs were found and fixed on the way —
the keeper law: never leave a discovered bug
unfixed. Run 1 failed empty because the runner set a
timeout on a plain socket *after* wrapping it (a
wrapped socket is detached; the call raised and the
session was never registered), and the message pump
originally ended at its first idle receive timeout
instead of riding it out. Both are fixed in
`deploy/lan_node.py`; run 2 above is the fixed
runner's.

## Honesty

- The mDNS *wire protocol* was not used: discovery
  rode Y-052's seed road (operator-named endpoints
  on the same machines, learned out of band) — one
  of the two unaided roads the discovery slice
  blesses, no cloud rendezvous anywhere. Announcements
  themselves are Y-052's, exchanged on the channels.
- Fabric envelopes carry the Y-050 factory's own
  `simulated: true` stamp, as they do everywhere in
  this tree — the core is transport-agnostic and this
  slice does not relabel another module's records.
  What is proven real here is the *carriage*:
  discovery, mutual TLS, and delivery between three
  real machines. The leg records' `simulated` speaks
  only of carriage, and says `false`.
- The three citizens held sessions and exchanged
  fabric traffic; they did not yet hold one registry
  truth through kills and a partition — that is the
  Part III gate, Y-074. Gossip, registry, and CRDT
  state do not ride this wire yet (Y-063–Y-073 stand
  between here and the gate).
- Each node's bundle, including its seed, remains on
  its own machine under `~/yggdrasil-lan/` — a real
  deployment's provisioned material, held the way
  Y-011's local key store holds it, and nowhere else.
- The mesh promises the Hailo chip nothing beyond
  Project Aesir's own ledger; nothing in this slice
  ran on the chip or claims it did.

## Not built here

- WAN-shaped profiles are built at Y-063
  (`docs/WAN_SIMULATION.md`) as a seeded simulator
  shaper over fabric messages — shapes, not a
  rewiring of this wire; fabric observability on
  the operator's door is
  Y-064; mixed-version behaviour is Y-065; rejoin
  and re-sync Y-066; the registrar's death drill
  Y-067 — all standing on this carriage.
- No node-agent seam in this slice: the agent joins
  the fabric through this composition when a later
  slice wires it, exactly as the earlier Part III
  slices ruled.
