# The Simulator — Y-014

*The road's wind tunnel: many fake nodes on one machine, so every
later part is tested in simulation before it touches hardware.
Everything the simulator produces is SIMULATED — shapes and logic
are proven here; device behaviour never is.*

## What stands

`src/yggdrasil/simulator.py` — an in-process loopback simulator
(`transport == "loopback-simulated"`, `network_access is False`;
there are no sockets, threads, or network code paths in the
module):

- **Fake nodes.** A `SimulatedNode` is a validated NodeIdentity
  (Y-006) plus a validated CapabilityCensus whose values are
  fixture data, plus a task handler. `make_fake_identity` /
  `make_fake_census` build the fixtures; `Simulator.spawn(n)`
  raises many nodes at once (the acceptance tests run 30).
  Fixture public keys are derived from the node id and are NOT
  cryptographic identities — real keypairs are Y-011.
- **Registry and discovery.** Nodes join, stop, and restart;
  `registry_snapshot()` returns every node's identity, census,
  and validated HealthReport as data; `peers(node)` is the
  discovery view — the present nodes a node can see. A stopped
  node reports `liveness: gone` in its health, drops out of every
  peer view, and refuses targeted tasks as unreachable.
- **Tasks end to end.** A validated TaskEnvelope (Y-007) travels
  to one node and comes back as validated StreamEvents
  (`accepted → started → completed|failed`) plus exactly one
  terminal ResultEnvelope with full provenance. Placement is the
  first capable present node in node_id order — a deterministic
  stand-in so tests are reproducible, explicitly NOT Verðandi
  routing (that intelligence is Part IV). Capability checks are
  exact matches against census truth: the required model must be
  present by its full census name and every required capability
  must be offered by a reachable engine.
- **Honest endings.** A targeted node that cannot run the task
  returns a `refused` result naming its error (`model_not_present`
  / `no_capable_node`). A handler that raises
  `SimulatedTaskFailure` produces a `failed` result with that
  Y-007 error code — shaped failures travel as results, not
  crashes. When NO node can take a task, `submit` raises
  `SimulatorError` naming `no_capable_node` instead of forging a
  result, because a ResultEnvelope's mandatory provenance cannot
  honestly name a node that never saw the task.
- **A virtual clock.** Simulated time starts at a fixed epoch and
  advances only by hand or by each node's declared simulated
  latency, so the same workload on a fresh simulator produces
  identical placements, timings, and timestamps (tested). Result
  timings are the simulated latency — measured on nothing.
- **One event language.** Joins, stops, and task outcomes are
  recorded as Y-013 EventEnvelopes from the `simulator`
  component, carrying ids and error names, never payload content.

## How later parts use it

- Y-015's fake engines (echo, latency-shaped, failure-shaped —
  `docs/FAKE_ENGINES.md`) plug in as node handlers, replacing the
  plain default echo; a handler offering `latency_ms_for(task)`
  sets each run's simulated latency in place of the node's
  constant.
- Part III tests fabric behaviour by comparing it against this
  tunnel's registry/discovery shapes; Part IV replaces the
  stand-in placement with real routing and grades it here first;
  Parts V–VII develop against simulated nodes before any
  hardware run, per the roadmap's order.

## Honest limits

- No transport, no TLS, no gossip, no partitions: a stopped node
  is the only failure shape at this slice, and "unreachable"
  means "not in this process's registry", nothing more. The real
  fabric is Part III.
- No resource model: queue depth is reported as 0, latency is a
  declared constant per node (or a declared per-task formula
  when a Y-015 fake engine handles the task), and no memory,
  thermal, or load behaviour is simulated. Governors are
  Part II; load shapes are later wind-tunnel work (Y-063,
  Y-072).
- Nothing in this slice has run on hardware, and no simulator
  result may be cited as evidence about a real device.
