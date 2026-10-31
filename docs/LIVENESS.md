# Heartbeats and liveness — the peer's ageing verdict (Y-054)

Part III's fifth slice, in `src/yggdrasil/liveness.py`.
The roadmap's sentence: *missed beats age a node from
present → suspect → gone, with the timings as data.* A
node reports **itself** present or gone in its Y-030
heartbeat and never reports itself suspect — suspect is
a peer's verdict on a silent node. This slice builds
the peer: a `LivenessTracker` held by one observing
node, fed the heartbeats it hears from its peers, and
ageing each silent one against the policy's lines.

## The timings are data

Ageing is judged against a validated `liveness_policy`
v1: the interval a peer is expected to beat at
(default 10 s), the silence after which it ages suspect
(default 30 s), the longer silence after which it ages
gone (default 90 s), and the tracker's peer bound
(default 256). The defaults are an operator's policy,
labelled as one — not facts about any LAN. On top of
the schema, the tracker enforces the ordering law
interval < suspect < gone: a policy whose lines cross
is refused by name, because under one no peer could
ever age in order.

## Ageing, measured honestly

- **The observer's clock rules.** A peer ages by the
  silence since its last accepted beat was *heard
  here* — never by the beat's own timestamp (clock
  discipline is Y-068's subject; no node's temporal
  logic trusts a peer's clock).
- **Freshness is by sequence.** Only a heartbeat whose
  `seq` is newer than the last accepted one refreshes
  a peer. A duplicate (same sequence) is counted apart
  in `beats_duplicate` and refreshes nothing — a
  recording of a dead node's heartbeat, gossiped round
  the mesh forever, must not keep the corpse present.
  An older sequence is refused `stale_sequence`.
- **The graceful goodbye is instant.** A peer whose
  own heartbeat reports `gone` (its agent stopped or
  retired; the Y-030 endpoint answering with a full
  beat) ages `gone` at once, on the basis
  `self_reported_gone`, without waiting out the silence
  lines. A later present beat with a newer sequence is
  the peer's return and ages it back to `present`.

Each peer's standing is a validated `liveness_record`
v1: verdict and basis (`fresh` / `silence` /
`self_reported_gone`), the last accepted beat's
sequence and own timestamp, when it was last heard on
this clock, the measured silence (`age_seconds`), the
beats that silence has swallowed at the expected
interval (`missed_beats`), and the two policy lines
the verdict was aged against — a record carries the
numbers its verdict rests on.

Verdicts are computed fresh on every asking. `sweep()`
walks every peer and records each verdict change as a
counted transition and a `liveness.verdict_changed`
Y-013 event (peer, from, to, measured age) — a peer's
falling silent is an event, not a silence; a plain
query (`liveness()`) records nothing. `report()`
sweeps first and returns the validated
`liveness_report` v1: the policy, every peer's record,
the present/suspect/gone counts of that moment, every
beat accepted, refused and why, duplicates, and
transitions — names and counts only.

## Refusals, named and counted

Every refused beat is named, counted in
`refused_reasons`, and emitted as a
`liveness.beat_refused` event: `malformed_beat` (not a
validated Y-030 heartbeat), `own_heartbeat` (a tracker
judges peers; the observer's own liveness is its
health endpoint's word), `suspect_self_report` (a beat
claiming suspect contradicts Y-030's rule and is
refused whole), `stale_sequence`, and
`peer_table_full` (a new peer heard while the table
holds the policy's bound — refused by name, never
silently dropped, never evicting a tracked peer). A
peer this tracker has never heard answers `None` from
`liveness()` — unknown is an honest answer, never a
manufactured verdict.

## The node agent's hand

The agent (Y-021) grows the optional `liveness_tracker=`
seam the fabric, discovery, and registry slices waited
for: `observe_peer_heartbeat()` hears a beat through
the tracker, `peer_liveness()` reads one verdict,
`sweep_liveness()` ages every peer, and `status()`
carries the tracker's report. Unwired, the agent judges
no peer and its status carries `None`, exactly as
before. No citizen factory wires a tracker in this
slice — which peers a node watches is deployment
wiring, stated plainly.

## Honesty

The tracker is an in-process judge: records and
reports carry `simulated: true` as a schema constant,
no socket is opened, no heartbeat crosses a LAN inside
this slice, and no peer is contacted. What is proven
is the ageing *rules* — freshness by sequence, ageing
by the observer's clock, the graceful-goodbye and
return paths, the counted refusals, and the transition
events, replayed deterministically in the suite.

## Not built here

- Epidemic gossip carrying heartbeats and registry
  state between nodes is Y-055; the CRDT formalisation
  of the registry and membership stores is Y-056.
- Real carriage of beats — heartbeats riding the
  fabric (Y-050) over TLS (Y-051) between the peers
  discovery finds (Y-052) — is Y-062 on the real LAN;
  hardware proof is Part VIII's.
- The registry (Y-053) still holds a gone peer's
  registered facts until they are superseded or
  withdrawn: a liveness verdict is not a withdrawal,
  and this tracker forgets nothing on the registry's
  behalf.
- Clock discipline — peers reporting their skew, and
  temporal logic distrusting any single clock — is
  Y-068.
