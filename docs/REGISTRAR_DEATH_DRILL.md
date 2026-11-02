# The Registrar's Death Drill (Y-067)

Y-053 built the registry replicated — no single
registrar to kill — and named this drill its own
acceptance shape. Y-054 built the peer's verdict
on a silent node: present → suspect → gone. This
slice runs the drill the roadmap orders — kill
the first node ever started; the mesh notices
nothing but a missing heartbeat — as one judged
exercise kept by `DeathDrill` in
`src/yggdrasil/registrar_death_drill.py`.

## The mesh, and the registrar

Nodes are started in order, and the order is
remembered. Every node gets one full Y-053
registry replica (`RegistryService`) and one
Y-054 liveness tracker (`LivenessTracker`),
composed, neither rewired. The first node ever
started is the drill's **registrar**: the node
the mesh first registered through and synced
from — and the drill's kill target.

## The kill is aimed by law

The `death_drill_policy` v1 fixes three schema
constants: `kill_target: "first_started"`,
`single_registrar_allowed: false`, and
`expected_effect: "missing_heartbeat_only"`. A
policy naming any other target, any other
expected effect, or a single registrar fails
validation. `kill` therefore accepts only the
first-started node:

- killing any other node is refused
  `not_first_node` — killing nodes mid-task in
  general is Y-073's failure drills, not this
  one;
- a second kill is refused `already_dead`;
- a kill with nothing started is refused
  `no_first_node`, and of a never-started node
  `unknown_node`.

The kill drops the node whole: its replica and
tracker objects are thrown away, it sends no
more heartbeats, and every later act aimed at
it — registering through it, syncing with it,
asking for its replica — is refused `node_down`
by name. Those expected refusals are **not**
disruptions: they are the mesh correctly naming
a dead door.

## What the survivors notice is measured

Each heartbeat round, every up node beats once
to every other up node's tracker, built by the
Y-030 door exactly as the liveness suite builds
them. After the kill the dead node's beats
simply stop, and each survivor's own tracker
ages it on the observer's clock: `missed_beats`
climbs (three whole intervals at 35 seconds of
silence under the default policy), and the
verdict walks present → suspect → gone. The
survivors keep beating *each other*, so their
verdicts on one another stay present — the only
standing that changes anywhere in the mesh is
the first node's. A node that joined after the
kill never heard the registrar beat at all, so
its tracker honestly holds **no** verdict on it
(a null in the report), while its registry
replica still holds the whole truth.

## The mesh must prove it kept working

The drill passes only on evidence gathered
after the kill, counted apart in the report:

- at least one **registration** accepted through
  a survivor (`registrations_after_kill`);
- at least one **sync** between survivors
  (`syncs_after_kill`);
- at least one **new node joined**
  (`joins_after_kill`) — and the suite proves
  that joiner is served the dead registrar's own
  registry entry whole, origin registrar and all,
  from a survivor's replica;
- the first node **gone** on every survivor
  tracker that ever heard it.

## Loss and disruption are counted, never smoothed

`entries_at_kill` is the registry union the mesh
held the moment before the kill, the victim's
own replica included. Any of it missing from
*every* survivor at report time is
`entries_lost`: an entry the registrar held that
was never replicated **is** lost when it dies,
and the suite proves the drill counts it and
fails. A registration or sync between survivors
refused after the kill is a `disruption`.
`effects_beyond_missing_heartbeat` is losses
plus disruptions, and the verdict is:

- **passed** — killed, aged gone, post-kill
  proofs done, effects at zero;
- **failed** — anything lost or disrupted;
- **incomplete** — the drill has not run that
  far yet.

## Every act is judged

Starts, registrations, syncs, heartbeat rounds,
and kills answer a validated `death_drill_record`
v1: `accepted`, or refused by name and counted
apart — `unknown_node`, `duplicate_node`,
`mesh_full`, `node_down`, `not_first_node`,
`already_dead`, `no_first_node`,
`registry_refused` (including a registration
whose census names another node than the act
does), `self_sync`, `no_peers`. API misuse —
asking for a dead or unknown node's replica or
tracker — raises `DeathDrillError` by name.

## Honesty

Everything here is SIMULATED: `simulated: true`
is a schema constant on the record and the
report. A "kill" is the in-process loss of one
node's replica and tracker objects; no process
was killed, no socket opened, no LAN crossed
(real carriage was Y-062's own proof; killing
real processes on real citizens is Part VIII's
hardware road). Y-066's restart of a dead node
is a different act from this drill's one-way
kill — the registrar does not come back here.

## Not built here

- Clock discipline — nodes reporting clock skew,
  temporal logic trusting no single node's clock
  — is built at Y-068 (`docs/CLOCK_DISCIPLINE.md`).
- Failure drills in general (node kill mid-task,
  partition mid-stream, corrupt message
  injection) are Y-073; Part III's gate, the
  three real citizens plus ten simulated nodes
  holding one registry truth through kills and a
  partition, is Y-074.
- No node-agent seam: as with the Part III
  slices before it, the agent gains no drill
  seam in this slice.
