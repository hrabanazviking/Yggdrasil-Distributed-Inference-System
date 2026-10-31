# Model Fetch Duty — Y-037

`src/yggdrasil/model_fetch.py` — a node can fetch a model it
lacks from a mesh peer or a named source, hash-verified, when
policy allows. The mesh carries its own supplies: a node that
needs weights no longer waits for an operator to carry them
by hand — but it never stocks a byte it has not verified.

## The pin travels with the ask

A fetch is requested as data shaped by
`contracts/schemas/model_fetch_request.schema.json` (v1):
the model's name and registry facts (family, quant, format,
capabilities — declared, never evidence), the source
(`source_kind` `peer` with a peer node id, or `named_source`
with the source's name), an opaque `locator` the transport
understands, and the **SHA-256 and byte size the fetched
file must measure**. There is no way to ask for a fetch
without a pin: unverifiable fetching is refused by the
shape itself, before any code runs.

`locator` is data for the transport — a peer-side handle, a
path on a named source. This module never interprets it,
never opens it, never executes anything it names.

## Policy is the gate, and it is data

`contracts/schemas/model_fetch_policy.schema.json` (v1):

- `allow_peer_fetch` — may mesh peers supply this node at
  all?
- `named_sources` — the names of the non-peer sources this
  node may use. A source the policy does not name does not
  exist for the fetcher, however reachable it claims to be.
  The default policy names none: an operator names sources
  deliberately.
- `max_model_bytes` — the largest model this node will
  fetch (null = no cap). The phone citizen's factory sets
  this from its profile's own small-model ceiling
  (1.5 GiB, Y-036): small models only binds at the fetch
  door too.
- `min_free_disk_bytes` — the disk reserve that must
  survive the fetch: a node never fills its own disk to
  stock a model.

Every gate is checked **before the transport is asked for a
single byte**, in that order, and every refusal is a record
naming its reason with the Y-007 code `scope_denied` —
never a silent no.

## Verified before stocked

The transport is an injected seam, exactly like the engine
adapters' (Y-023–Y-025): a callable taking the validated
request and returning an iterable of byte chunks, or an
object with `fetch_chunks(request)`. It streams into a
staging file under the store root's `incoming/` directory
while a SHA-256 is built chunk by chunk:

- a stream that runs past the expected size is cut off at
  the first chunk that crosses it (never drained, never
  stocked);
- only when the staged bytes measure **exactly** the pinned
  size and hash are they handed to the model store's own
  `add()` (Y-026) — which copies, re-hashes, and pins them
  under the store's rules — and the store's own verification
  is read back as the record's `store_status`;
- wrong bytes are deleted from staging and **never enter
  the store**: a hash mismatch leaves the node exactly as
  stocked as before, plus a record that says so
  (`model_hash_mismatch`).

Presence short-circuits: if the store already holds the
name with the pinned hash, verified right now, the answer
is `already_present` and the transport is never asked. If
the name is registered with *different* bytes the fetch is
refused by name — fetching is not a way to re-point a name
at other weights. If the registered file is corrupt or
missing, a fetch carrying the pin **restores** it.

## Every attempt is a record

Each outcome — `fetched`, `already_present`, `refused`,
`failed` — lands as a validated
`contracts/schemas/model_fetch_record.schema.json` (v1):
bytes actually received, the hash actually measured (or
null), the run time on an injected timer, the store's
status afterwards — and is spoken as a Y-013 EventEnvelope
from the `node_agent` component. `report()` summarises the
ledger as a validated
`contracts/schemas/model_fetch_report.schema.json` (v1).

Failures keep their Y-007 names: a source that cannot be
reached — before or mid-stream — fails
`engine_unreachable`; a transport's own accidents and
non-byte chunks fail `engine_error`; a fetcher built with
no transport fails `no_transport_wired` /
`engine_unreachable` rather than pretending a fetch
happened.

## The agent's door

The node agent grows a `model_fetcher=` seam:
`agent.fetch_model(request)` delegates (started or joined
only — a draining node stocks nothing new),
`agent.fetch_report()` and `status()["model_fetch"]` carry
the report, and an agent built without a fetcher refuses by
name. All four citizen factories (Y-033–Y-036) wire a
fetcher over their store, carrying their `simulated` flag
and accepting `fetch_transport=` / `fetch_policy=` from
deployment wiring.

## Honesty

There is **no network client in this module** — the Part I
gate's banned-import scan stays green — and no real peer or
source has carried a model for this tree yet. The suite's
transports are in-process fakes speaking chunk streams, and
a fetcher built over one is constructed `simulated=True`
and says so in every record and in the report. The real
transport is the fabric's (Part III); deployment wiring
that builds a fetcher over it is what sets `simulated`
false. Hash verification proves the bytes are the pinned
bytes — it does not prove the weights are good, nor that a
source was honest about anything else; the pin itself must
come from a trusted ask (the registry, Y-008 / Part IV),
and that trust is the caller's.

## Not built here

No real transport (the fabric is Part III — peer carriage,
discovery of *which* peer holds a model, and transfer
resumption live there); quantisation fitness has landed
(Y-038 — which quants a node may run is enforced at load /
placement there, not at fetch: a fetched model still lands
in the store whatever its quant, and the lifecycle's fitness
gate is what refuses to load an unfit one); no fetching
*for* another node
(relay duty is a fabric question, Part III); and the node
journal (Y-039) does not yet drink from these records.
