# WAN-Shaped Simulation (Y-063)

The fabric was proven on the real LAN (Y-062).
The road beyond the LAN — WorldNet — is not
tested by hoping the LAN's numbers travel.
**It is tested as shapes, not hopes:** this slice
is the shaper, `WanSimulation` in
`src/yggdrasil/wan_simulation.py`, which carries
Y-050 fabric messages across directed links whose
latency and loss follow a declared profile.

## A profile is data

A `wan_profile` v1 names the shape every attempt
on a link takes:

- `base_latency_ms` plus a uniform jitter draw of
  up to `jitter_ms` — the attempt's shaped latency;
- `loss_rate` — each attempt is lost with that
  probability;
- `max_attempts` and `retry_backoff_ms` — the
  retry discipline: how many attempts a crossing
  may spend, and the simulated time between them.

Five named shapes stand in `NAMED_PROFILES`,
declared numbers — a policy, not a measurement
of any real network:

| shape | base | jitter | loss | attempts |
| --- | --- | --- | --- | --- |
| `lan` | 1 ms | 1 ms | 0 | 3 |
| `home_wan` | 25 ms | 10 ms | 0.1% | 3 |
| `congested` | 80 ms | 50 ms | 2% | 3 |
| `intercontinental` | 180 ms | 30 ms | 0.5% | 4 |
| `lossy_mobile` | 120 ms | 80 ms | 8% | 4 |

A profile outside its bounds — negative latency,
a loss rate outside 0–1, zero attempts — fails
validation by name. `simulated: true` is a
schema constant on the profile and the report.

## The draws are seeded, so a run replays

Each directed link draws from its own stream,
seeded from the run's seed, the profile's name,
and the link's two ends. The same seed, profile,
and traffic produce identical records, latencies,
and drops, byte for byte: a WAN failure found in
the tunnel can be replayed until it is
understood, never chased as a flake. A different
seed is different weather, and the report names
the seed it ran under.

## Loss is counted, never silent

Every crossing is a validated `wan_record` v1.
A message whose attempts are all lost is dropped
`lost_after_retries` — counted apart in the
report, evented (`wan.message_dropped`, with a
`wan.attempt_lost` event per lost attempt), and
delivered nowhere. The shaper manufactures no
delivery and hides no drop inside a retry.
Delivered messages wait in the recipient's
inbox, whole and exact, until `inbox(node)`
drains them — delivery is once.

A message that fails the Y-050 `fabric_message`
contract, or a direct message carried to a
recipient it does not name, is dropped
`malformed` before it enters the shape: zero
attempts, no latency, counted apart.

## Latency is simulated time

A record's `latency_ms` is the sum of its
attempts' shaped latencies plus the backoff
between them. `report()` answers a validated
`wan_report` v1: per-directed-link standings
(sent, delivered, dropped, attempts, retries,
and the mean and maximum latency of delivered
messages), every record in order, and the totals
with drops counted apart by reason. These
numbers are a shape's consequence on a seeded
draw — they prove how the mesh's logic behaves
*under* latency and loss, and nothing about any
real network's speed. The acceptance tests hold
the shapes to their own declarations: a one-shot
20%-loss shape drops within a band of its rate
over 300 messages, and an intercontinental
shape's mean delivered latency exceeds a LAN
shape's by more than fiftyfold over the same
traffic.

## Honesty

Everything here is SIMULATED: no socket is
opened, no packet leaves the process, and no
real WAN was crossed or measured. What is proven
is the shaping law on the contract shapes —
latency, loss, retry, and drop accounting the
later WorldNet road will be graded against.

## Not built here

- Fabric observability — message rates,
  convergence, and drops on the operator's
  door, over this shaper's crossings among the
  rest — is built at Y-064
  (`docs/FABRIC_OBSERVABILITY.md`): this
  shaper's validated report is one of the
  door's named feeds.
- Mixed-version meshes are built at Y-065
  (`docs/VERSIONED_PROTOCOL.md`); restart
  resilience is built at Y-066
  (`docs/RESTART_RESILIENCE.md`), and the remaining
  Part III drills stand on this shaper and on
  Y-062's real carriage; real WorldNet carriage
  itself is a later road, tested here as shapes
  first.
- No node-agent seam: as with the Part III
  slices before it, the agent gains no WAN seam
  in this slice; the shaper is wind-tunnel
  machinery the components are graded against.
