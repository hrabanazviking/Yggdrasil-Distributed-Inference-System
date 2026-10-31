# Yggdrasil — The Growing of the Tree

*A massive build roadmap: take the Yggdrasil Distributed Inference System
from its formal specification (v3.1, design phase) to a living, local,
self-organising inference mesh — small specialised models pooled across
Volmarr's real devices, routed by past, present, and future, with no
cloud anywhere in it.*

Commissioned by Volmarr, 2026-10-30 ("draw up an advanced massive
roadmap for this project and then begin working on it"). Governing repo:
`hrabanazviking/Yggdrasil-Distributed-Inference-System`. Governing
specification: the repo's README (v3.1 Advanced Formal Specification)
plus `Yggdrasil_WorldNet_The_Web4.0_ Architecture.md`. This roadmap
builds what those documents describe; where it must choose what they
leave open, the choice is made as an explicit decision record in Part I,
never silently.

---

## 1. What stands today (measured, 2026-10-30)

- **A complete vision, zero code.** The repo holds the 61 KB v3.1
  specification (abstract architecture, pseudocode for the Node Agent,
  Urðr, Verðandi, Skuld, the fabric, the task lifecycle, the model
  registry, deployment, security), the WorldNet/Web4.0 addendum
  (planetary scale, avatar layer, council protocol, RATATOSKR overlay,
  Níðhöggr abuse prevention, incentive layer), the Visual Design
  Lawbook, and a licence. No source tree, no tests, no build.
- **The design's load-bearing ideas** (the spec's own, which this road
  exists to make real):
  1. **Temporal routing** — the Norns as an orchestration layer:
     Urðr (past: context, decisions, invariant ancestry), Verðandi
     (present: load, availability, topology), Skuld (future: predicted
     breakage, cascade risk, performance scoring). No rival routes on
     all three axes.
  2. **Heterogeneity as strength** — Jetson-class boards, Raspberry Pis
     (one with a Hailo-10), gaming laptops: any model on any device,
     quantised to fit; capability-aware placement.
  3. **Self-organisation** — nodes discover, register capabilities, and
     join routing without a master; no single point of failure; state
     replicated with CRDTs; health-checked self-healing.
  4. **The nine worlds as node roles** — Asgard (orchestration),
     Jotunheim (heavy lift), Svartalfheim (forge workers), Nidavellir
     (auditors), Alfheim (vision), Vanaheim (mapping), Niflheim (cold
     storage), Muspelheim (emergency fallback), Midgard (the human's
     door).
  5. **Fully local** — no external API calls; sovereignty is the point.
- **The workshop it must serve already exists**: Project Aesir (the
  sovereign per-node inference engine), the Hermes hack (whose
  cognition tiers — deterministic / local / cloud — are a routing
  contract waiting for this mesh), Mythic Agent Coder's crews, Samtal
  and Rúnmál (chat surfaces that need local inference), Kista (node
  credentials), Verdandi (event fabric the Hermes plugin already
  speaks), WYRD (world-state). Yggdrasil is the missing middle: the
  fabric that lets all of them draw on every device at once.

## 2. Decisions this roadmap makes (Part I ratifies them as records)

1. **Orchestration, not inference.** Yggdrasil never re-implements a
   model runner. Each node wraps the engines that exist — Aesir first,
   then Ollama, llama.cpp servers, and OpenAI-compatible local
   endpoints — behind one node contract. The mesh's genius is routing,
   memory, and verification, not another GEMM.
2. **Python control plane first.** Node agent, Norns, and fabric in
   Python (asyncio), matching the workshop's living code (Samtal,
   Hermes plugins, Heimdall) and its keepers. Hot paths earn compiled
   replacements (Mojo, then Rust/Go where the spec dreamed of them)
   only when Part XI's measurements name the cost — the spec's
   Rust/Tokio sketch is honoured as a direction, not obeyed as a
   starting tax on a one-human workshop.
3. **Transport by evidence.** The spec names MQTT + gossip + TLS.
   Part I benchmarks MQTT, NATS, and a minimal QUIC/TCP gossip against
   the mesh's real message shapes on Pi-class hardware and records the
   choice; the fabric's *contracts* are written transport-first so the
   loser can be swapped without surgery.
4. **CRDT state, honestly scoped.** Replicated state uses CRDTs where
   the spec demands (registry, Urðr ancestry); where a simpler
   single-writer log is provably enough, the record says so and why.
5. **No Docker, ever. Native services** (systemd) on every node class.
6. **Every claim graded.** A routing idea is PROVEN only by a measured
   run on named hardware; simulations are labelled SIMULATED; the
   WorldNet part (X) begins openly as research, most of it NOT PROVEN.

## 3. Shape of the whole

Eleven parts, **244 slices**.

| Part | Name | Slices |
|---|---|---|
| I | Roots — decisions, contracts, scaffolding | Y-001 – Y-020 |
| II | The Node Agent — every device a citizen | Y-021 – Y-048 |
| III | Ratatoskr — the message fabric | Y-049 – Y-074 |
| IV | Verðandi — scheduler & semantic router | Y-075 – Y-100 |
| V | Urðr — memory & the knowledge graph | Y-101 – Y-122 |
| VI | Skuld — verifier & temporal projection | Y-123 – Y-144 |
| VII | Ensembles — many models, one answer | Y-145 – Y-166 |
| VIII | The Nine Worlds on Real Hardware | Y-167 – Y-188 |
| IX | The Workshop Weave — serving Hermes, Aesir, the halls | Y-189 – Y-208 |
| X | WorldNet Horizon — the planetary research road | Y-209 – Y-230 |
| XI | The Proof Spine — chaos, benchmarks, release | Y-231 – Y-244 |

---

## Part I — Roots: decisions, contracts, scaffolding (Y-001 – Y-020)

*Goal: turn a specification into a buildable project. Every open choice
the spec leaves becomes a written decision; every component gets a
contract before it gets code.*

- **Y-001** Spec reconciliation: the v3.1 README + WorldNet addendum read
  whole; a concordance maps every spec section to the part of this road
  that builds it (or names it deferred, with the reason).
- **Y-002** ADR-001 Language: Python control plane first (Decision 2),
  with the compiled-path criteria recorded (measured thresholds that
  would justify a Mojo/Rust component).
- **Y-003** ADR-002 Transport: the fabric benchmark design (MQTT vs NATS
  vs minimal gossip on Pi-class hardware, the mesh's real message
  shapes) — the benchmark itself runs in Part III; the *criteria* are
  fixed here.
- **Y-004** ADR-003 State: CRDT scope per Decision 4 — which stores are
  CRDT-replicated, which are single-writer logs, and the merge rules.
- **Y-005** ADR-004 Inference engines: the node wraps Aesir, Ollama,
  llama.cpp, and OpenAI-compatible local endpoints behind one contract;
  engine capability reporting is honest and per-engine.
- **Y-006** The node contract v1: identity, capability census, model
  lifecycle, task execution, health — the interface every engine
  adapter and every Norn speaks; written before code.
- **Y-007** The task contract v1: task envelope (id, kind, payload,
  criticality, context refs, verification needs), result envelope,
  streaming shape, error taxonomy.
- **Y-008** The registry contract: model entries (name, sizes/quants,
  capabilities, device classes, measured speeds) — the spec §4.2 shape,
  as data schemas.
- **Y-009** Repo scaffolding: package layout, pyproject, test harness,
  lint, the house no-Docker native-service pattern (systemd units as
  data), CI as manual-dispatch only (Volmarr's standing default).
- **Y-010** Threat model: node impersonation, poisoned results, gossip
  spam, malicious tasks — the mesh's enemies named before its doors
  are built.
- **Y-011** Identity foundation: per-node keypairs, node certificates
  issued by the mesh's own root (the WorldNet §3 seed, local scale),
  keys kept via Kista where a node supports it.
- **Y-012** The nine-worlds role model as data: role definitions
  (capabilities expected, criticality ceilings) assignable per node,
  changeable by the operator — roles are hats, not hardware.
- **Y-013** Observability contract: what every component logs and
  meters (structured, local, no phone-home), the Verdandi event shapes
  reused where they fit.
- **Y-014** The simulator: a loopback multi-node simulator (many fake
  nodes on one machine) — the road's wind tunnel; every later part
  tests in it before touching hardware.
- **Y-015** Fake engines: deterministic stub inference engines (echo,
  latency-shaped, failure-shaped) so routing can be proven without
  models.
- **Y-016** Data discipline: all registries, profiles, and policies are
  versioned data files with schemas and validators.
- **Y-017** The operator's door (Midgard v0): a CLI that can list
  nodes, tasks, and health from day one — humans are never locked out
  of their own tree.
- **Y-018** Licence & provenance check: the repo's licence read and
  honoured; third-party pieces noticed; nothing vendored silently.
- **Y-019** The campaign file: this roadmap as machine data (the
  Mythic Coder M-201 pattern) so keepers and boards can run it.
- **Y-020** Part I gate: contracts + ADRs + simulator skeleton merged;
  the concordance published in-repo; zero inference code yet — the
  roots hold before the trunk rises.

## Part II — The Node Agent: every device a citizen (Y-021 – Y-048)

*Goal: one agent that runs on the Pi, the laptop, and anything between —
censusing what it truly has, hosting models through the engines that
exist, and minding its own resources like a good neighbour.*

- **Y-021** Node agent skeleton: identity (Y-011), config, lifecycle
  (start, join, drain, stop), native systemd service.
- **Y-022** Capability census: CPU, RAM, accelerators (CUDA, Hailo,
  NPU, none), disk, thermals — measured, not assumed; refreshed on
  change.
- **Y-023** Engine adapter I — Ollama: discovery, model list, generate
  (streaming), honest capability mapping into the node contract.
- **Y-024** Engine adapter II — Aesir: Aesir's API as it stands; its
  capability ledger (what is PROVEN on that device) feeds the census —
  the mesh never promises what Aesir itself marks partial.
- **Y-025** Engine adapter III — llama.cpp server + generic
  OpenAI-compatible local endpoints.
- **Y-026** Model store: on-node model files with hashes; a model is
  "present" only when its hash verifies (the Aesir wave discipline).
- **Y-027** Model lifecycle: load/unload into slots by VRAM/RAM budget;
  swap costs measured and reported to Verðandi as facts.
- **Y-028** The resource governor: per-node ceilings (memory, thermal,
  CPU share) with graceful refusal — a node sheds load before it cooks
  (the accelerator-first law's politeness, as code).
- **Y-029** Task execution sandbox: tasks run bounded (time, tokens,
  output), cancellable, with streamed partial results.
- **Y-030** Node health endpoint: liveness, load, models, governor
  state — the shape Part III's heartbeats carry.
- **Y-031** Local queue: tasks held per node with priorities; a busy
  node says how busy, in numbers.
- **Y-032** Failure shapes: engine crash, OOM, thermal throttle, model
  corrupt — each detected, contained, and reported distinctly.
- **Y-033** The Pi citizen: the full agent on Mimir-class ARM,
  coexisting with Samtal and the Hailo road (the kiosk/Aesir
  coexistence rules honoured).
- **Y-034** The laptop citizen: purplelongship-class x86 + CUDA census,
  Aesir GPU line as its engine.
- **Y-035** The VM citizen: this VM as a CPU-only node — honest speeds,
  no pretence.
- **Y-036** Phone-class profile: the agent's shape on a phone (small
  models, battery governor) — built and simulated; on-device proof
  recorded as NOT PROVEN until a phone runs it.
- **Y-037** Model fetch duty: a node can fetch a model it lacks from a
  mesh peer or a named source, hash-verified, when policy allows —
  the mesh carries its own supplies.
- **Y-038** Quantisation fitness: per device class, which quants of a
  model are runnable (registry data from Y-008) enforced at placement
  time, not discovered at crash time.
- **Y-039** Node journal: every task the node ran with engine, model,
  timings, outcome — the raw material Urðr ancestry is built from.
- **Y-040** Graceful degradation: a node losing its accelerator keeps
  serving on CPU at declared reduced capability — census updates, no
  silent lies.
- **Y-041** Idle generosity: an idle node advertises spare capacity;
  a busy one withdraws it — the present-tense truth Verðandi routes on.
- **Y-042** Node operator tools: drain, pause, inspect, and retire a
  node from the CLI (Y-017 grown).
- **Y-043** Security of the agent: tasks are data, never code, unless a
  signed task kind explicitly says so (threat model Y-010 enforced).
- **Y-044** Power and thermal telemetry on Pi + laptop; the governor's
  decisions cite them.
- **Y-045** Multi-engine node: one node hosting Aesir and Ollama side
  by side, slots accounted across engines.
- **Y-046** Node conformance kit: a test pack any new engine adapter
  must pass to join (contract, streaming, errors, census honesty).
- **Y-047** Soak: one node, 1,000 tasks, mixed engines — leak and
  drift check with numbers.
- **Y-048** Part II gate: three real citizens (Pi, laptop, VM) run the
  conformance kit green against their real engines.

## Part III — Ratatoskr: the message fabric (Y-049 – Y-074)

*Goal: the squirrel that never stops running — discovery, registry,
heartbeats, and task/result flow between nodes, secure by default,
honest under partition.*

- **Y-049** The transport benchmark (ADR-002 executed): MQTT vs NATS vs
  minimal gossip on Pi-class + LAN shapes; latency, chatter, memory —
  the record decides.
- **Y-050** Fabric core: publish/subscribe + direct node channels over
  the chosen transport, one API for all components.
- **Y-051** TLS everywhere: node certificates (Y-011) on every channel;
  no plaintext mode exists to accidentally use.
- **Y-052** Discovery: nodes find each other on a LAN (mDNS + a seed
  list) without any cloud rendezvous.
- **Y-053** The registry service: nodes register census + models;
  the registry is itself replicated (no single registrar to kill).
- **Y-054** Heartbeats and liveness: missed beats age a node from
  present → suspect → gone, with the timings as data.
- **Y-055** Gossip of state: registry and health propagate epidemically;
  convergence time measured in the simulator at 3, 10, and 30 nodes.
- **Y-056** CRDT stores land (ADR-003): registry + membership as CRDTs;
  merge correctness property-tested.
- **Y-057** Task channel: task offers, claims, and result streams ride
  the fabric with the Y-007 envelopes; backpressure is explicit.
- **Y-058** Partition behaviour: a split mesh keeps working in halves;
  on healing, CRDTs merge and duplicate task executions are reconciled
  by task id (never silently double-answered).
- **Y-059** Message provenance: every message signed by its node;
  unsigned or wrongly-signed traffic is dropped and counted.
- **Y-060** Rate and size discipline: per-node quotas on gossip and
  tasks; a flooding node is throttled by its peers (Níðhöggr's first
  small tooth, local scale).
- **Y-061** Store-and-forward: a task for a sleeping node waits in the
  fabric with a TTL; expiry is an event, not a silence.
- **Y-062** The fabric on the real LAN: Pi + laptop + VM over the home
  network, TLS on, discovery unaided.
- **Y-063** WAN-shaped simulation: latency/loss profiles in the
  simulator (the road to WorldNet is tested as shapes, not hopes).
- **Y-064** Fabric observability: message rates, convergence, drops —
  on the operator's door.
- **Y-065** Versioned protocol: fabric messages carry versions; mixed-
  version meshes degrade by contract, not by crash.
- **Y-066** Restart resilience: a node rejoins and re-syncs its CRDT
  state without a full reload.
- **Y-067** The registrar's death drill: kill the first node ever
  started; the mesh notices nothing but a missing heartbeat.
- **Y-068** Clock discipline: nodes report clock skew; temporal logic
  (Part VI) never trusts a single node's clock.
- **Y-069** Fabric conformance kit: any future transport swap must pass
  the same suite (Decision 3's promise kept).
- **Y-070** Encrypted payloads for sensitive task kinds: content sealed
  to the executing node where the task's policy demands.
- **Y-071** Audit tap: an operator-consented, read-only feed of fabric
  events for debugging — off by default, loud when on.
- **Y-072** Load shapes: heartbeat/gossip overhead measured per node
  count; the mesh's own cost is a published number.
- **Y-073** Failure drills: node kill mid-task, partition mid-stream,
  corrupt message injection — recovery proven per drill.
- **Y-074** Part III gate: the three real citizens + 10 simulated nodes
  hold one registry truth through kills and a partition, evidence
  banked.

---

## Part IV — Verðandi: scheduler & semantic router (Y-075 – Y-100)

*Goal: the present-tense Norn. Given a task, choose where it runs —
by capability, by load, by measured fitness — and own its whole
lifecycle from the spec §4.1 state machine.*

- **Y-075** Task lifecycle state machine (spec §4.1) implemented as the
  single source of truth for task state; every transition journaled.
- **Y-076** Capability matching: task requirements (model, context,
  vision, tools, quant floor) matched against registry truth — a task
  is never dealt to a node that cannot truly run it.
- **Y-077** Load-aware placement: current queue depth, slot pressure,
  and governor state weigh the choice; the reasoning is recorded per
  decision (why this node — in plain data).
- **Y-078** Fitness memory (Urðr's first gift): measured per
  (node, model, task-kind) speeds and success rates feed placement —
  yesterday's runs steer today's.
- **Y-079** Semantic routing: task kind classification (code, chat,
  vision, embedding, reasoning…) routes to models registered for that
  kind; unknown kinds fall back by declared rules.
- **Y-080** Criticality classes: a critical task buys the most reliable
  node + verification (Part VI); a casual one buys the cheapest
  adequate — criticality is in the envelope (Y-007), set by callers.
- **Y-081** Deadline-aware scheduling: tasks with deadlines are placed
  by predicted completion (fitness × queue), and a deadline that
  cannot be met is refused early, honestly.
- **Y-082** Streaming placement: long generations pin to their node;
  mid-stream node death hands the partial + state to Skuld's rules
  (Part VI) for resume-or-restart judgement.
- **Y-083** Batch and fan-out: a task that splits (embed a corpus, map
  a repo) fans out across nodes and gathers — scatter/gather as a
  first-class lifecycle.
- **Y-084** Priority and fairness: per-caller shares so one greedy
  caller can't starve the mesh; the operator's own work preempts by
  his standing priority.
- **Y-085** Placement dry-run: "where would this go and why" as an API
  — the router explains itself before it acts.
- **Y-086** Locality preference: prefer the caller's own device, then
  the LAN, then further — data rarely travels farther than it must.
- **Y-087** Model-warmth awareness: a node with the model already
  loaded beats a faster node that must swap (swap costs from Y-027
  are in the math).
- **Y-088** Router as a service any node can host: Verðandi is a role
  (Y-012), not a machine; its state replicates over the fabric.
- **Y-089** Router failover: the role moves when its host dies, mid-
  queue, with the lifecycle journal as the handover.
- **Y-090** Anti-flapping: placement hysteresis so borderline tasks
  don't bounce between nodes.
- **Y-091** The Muspelheim trigger (routing half): when the mesh
  saturates, Verðandi sheds by criticality — casual work waits or
  refuses; critical work is defended.
- **Y-092** Caller SDK (Python): submit, stream, cancel, await — the
  small API every consumer in Part IX uses.
- **Y-093** CLI door grows: submit a task from the command line, watch
  it route, read why.
- **Y-094** Routing benchmarks: placement quality vs naive round-robin
  and vs single-best-node on scripted workloads — numbers, not claims.
- **Y-095** Simulator league: routing policies compete in the wind
  tunnel across load shapes; the league table is data for Part VII.
- **Y-096** Policy as data: placement weights, preferences, and
  criticality rules are operator-editable data with validators.
- **Y-097** Multi-tenant honesty: two households' meshes never mix —
  mesh identity is cryptographic (Y-011), not a config string.
- **Y-098** Task provenance chain: every result carries (task id, node,
  engine, model, quant, timings) — the receipt Part VI verifies.
- **Y-099** Cancellation done right: a cancelled task stops everywhere,
  frees its slot, and the cancellation is a journal fact.
- **Y-100** Part IV gate: a scripted mixed workload (chat + code +
  embeddings + one critical) routes across the three real citizens
  with placement reasons recorded; benchmark vs round-robin banked.

## Part V — Urðr: memory & the knowledge graph (Y-101 – Y-122)

*Goal: the past-tense Norn. The mesh remembers — decisions, contexts,
outcomes — as a versioned knowledge graph any node can ask, so the
whole tree is wiser than any branch.*

- **Y-101** Urðr store design: entities (nodes, models, tasks,
  decisions, contexts, outcomes) and relations, versioned; the schema
  is data (Y-016).
- **Y-102** Decision ancestry: every routing decision (Y-077) lands in
  Urðr with its reasons — "why did this run there" is answerable
  forever.
- **Y-103** Outcome ledger: task outcomes (success, quality signals,
  failures) join the graph — the fitness memory (Y-078) is a view
  over this, not a separate pile.
- **Y-104** Context service: conversation/project contexts stored as
  versioned objects; a task references context by id + version, never
  by copying megabytes around.
- **Y-105** Context injection: Verðandi attaches the right context
  slice to a task (by reference resolution at the executing node).
- **Y-106** Shared knowledge bases: curated corpora (a project's docs,
  a household's notes) as Urðr collections with owners and scopes.
- **Y-107** Retrieval over the graph: structured queries + embedding
  search (embeddings computed by mesh tasks themselves — the tree
  feeds its own memory).
- **Y-108** Replication: Urðr replicates by scope — household graph
  everywhere, private scopes only where their owner allows.
- **Y-109** Versioning and time travel: any graph state as of a moment
  can be read (the past is a place you can visit, carefully).
- **Y-110** Provenance law: every fact in Urðr carries its source
  (which node, which task, when); unsourced assertions cannot enter.
- **Y-111** Forgetting: facts decay or are deleted on policy/owner
  word; deletion propagates through replicas and is itself a fact.
- **Y-112** Contradiction handling: conflicting facts coexist with
  their sources; consumers choose by trust policy — Urðr never
  silently picks a winner.
- **Y-113** The persona seed (WorldNet §6, local scale): a character's
  canon (prompts, habits, voice notes — the Kista expansion's kin)
  can live in Urðr as a versioned context any node can embody.
- **Y-114** Session memory for chat consumers: Samtal/Rúnmál-shaped
  conversation memory as a standard Urðr pattern (Part IX consumes).
- **Y-115** Graph size discipline: compaction, summarisation tasks
  (run on the mesh), and cold storage to Niflheim-role nodes.
- **Y-116** Backup and export: the whole graph as portable files —
  the household's memory is ownable, like everything else here.
- **Y-117** Privacy scopes enforced at read: a task sees only the
  scopes its caller holds; scope checks are in the store, not in
  callers' good intentions.
- **Y-118** Urðr query door: the CLI/API can ask the graph plainly
  (decisions for a task, a node's history, a model's record).
- **Y-119** Cold start: a new node syncs the graph slices it needs
  and no more; sync cost measured by graph size.
- **Y-120** Consistency drills: kill an Urðr host mid-write; replicas
  converge; no fact is half-born.
- **Y-121** Quality signals intake: consumer feedback (accepted,
  corrected, rejected answers) lands as outcome facts for Part VII.
- **Y-122** Part V gate: a week-in-a-day simulation — decisions,
  outcomes, contexts accumulate; ancestry questions answered from the
  graph alone; forgetting drill passes.

## Part VI — Skuld: verifier & temporal projection (Y-123 – Y-144)

*Goal: the future-tense Norn. Verify what the mesh produces, project
what it is about to do, and price risk before it is paid — the spec's
most original organ, built with the most honesty about its limits.*

- **Y-123** Verification contracts: per task kind, what "checked"
  means (schema, invariants, cross-check, second-model review) — as
  data.
- **Y-124** Invariant checker service: results validated against
  declared invariants before delivery; violations quarantined, never
  silently passed.
- **Y-125** Shadow verification: critical tasks (Y-080) are duplicated
  to a second node/model; agreement is evidence, disagreement is an
  event with both answers preserved.
- **Y-126** The Auditor pattern: Nidavellir-role nodes run review
  tasks (code review, fact check) as ordinary mesh citizens — the
  workshop's Auditor office, mechanised.
- **Y-127** Temporal projection v1: from Urðr's fitness history +
  current load, predict a task's completion time and failure risk —
  with the prediction's own past accuracy tracked.
- **Y-128** Cascade risk: a task whose failure would strand dependents
  (fan-out parents, campaign slices) is scored; high cascade risk
  buys verification and checkpointing.
- **Y-129** Breakage prediction for nodes: thermal trends, error
  rates, disk pressure → a node's near-future reliability score feeds
  Verðandi (the future steering the present, as designed).
- **Y-130** Calibration court: Skuld's predictions are graded against
  outcomes monthly-in-simulation; a miscalibrated predictor is
  retrained or demoted to advisory — honesty enforced on the prophet
  herself.
- **Y-131** Canary tasks: synthetic probe tasks continuously measure
  nodes/models (the mesh takes its own pulse); canary results are
  Urðr facts.
- **Y-132** Anomaly flags: a node suddenly slow, a model suddenly
  wrong-shaped — flagged with evidence, quarantined by policy if
  severe.
- **Y-133** Result attestation: verified results carry Skuld's mark
  (what was checked, how, outcome) — consumers can demand the mark.
- **Y-134** Temporal debt: tasks deferred by Muspelheim shedding are
  remembered with their age; the mesh knows what it owes.
- **Y-135** What-if door: "if I add this workload, what breaks" —
  projection over the simulator seeded from live Urðr state.
- **Y-136** Verification budgets: checking costs mesh capacity;
  Skuld spends it by criticality and says what it skipped and why.
- **Y-137** Disagreement tribunal: shadow-verification conflicts are
  resolved by declared rules (third node, stronger model, human) —
  never by coin flip, never silently.
- **Y-138** Projection honesty in the API: every prediction ships with
  its confidence and its track record; a young Skuld says "I don't
  know yet" in so many words.
- **Y-139** Security verification: tasks claiming sensitive kinds get
  their provenance chain (Y-098) checked link by link.
- **Y-140** Long-task checkpoints: streaming tasks checkpoint partial
  state into Urðr so a node death loses seconds, not the whole task.
- **Y-141** The Norns together: a single decision trace showing past
  (Urðr facts), present (Verðandi state), future (Skuld scores) for
  one task — the trinity made visible, the project's signature demo.
- **Y-142** Failure-injection league: Skuld graded on catching
  planted faults (wrong results, slow nodes, lying engines) in the
  simulator.
- **Y-143** Advisory mode: Skuld can run observe-only on a young mesh,
  learning calibration before it is trusted with gates.
- **Y-144** Part VI gate: the fault-injection league passed at a
  stated catch rate; calibration report published; the Y-141 trace
  demonstrated on the real three-node mesh.

---

## Part VII — Ensembles: many models, one answer (Y-145 – Y-166)

*Goal: the mesh's reason to exist — tasks no single small model on a
single small device could serve, answered by the tree together:
ensembles, role crews, learned routing, and overload grace.*

- **Y-145** Ensemble patterns as data: parallel-vote, draft-and-review,
  decompose-and-delegate, cascade (small first, escalate on doubt) —
  patterns are named, reusable task shapes.
- **Y-146** The cascade: a small local model attempts; its confidence
  (and Skuld's rules) decide acceptance or escalation to a bigger
  node — the cost shape that makes small models mighty.
- **Y-147** Draft-and-review: a Forge-role node drafts, an Auditor-role
  node reviews against invariants, a revision loop bounded by policy.
- **Y-148** Decompose-and-delegate: a planner splits a task into
  sub-tasks dealt across worlds (the Mythic crew pattern as mesh
  behaviour), gathering with the Y-083 lifecycle.
- **Y-149** Voting with honesty: multi-model answers compared;
  agreement statistics land in Urðr; a vote is evidence, not truth.
- **Y-150** Role ensembles from the nine worlds: a named configuration
  (Architect on Jotunheim, Forges on Svartalfheim…) summonable as one
  virtual worker.
- **Y-151** Learned routing v1 (spec §4.3, staged honestly): routing
  policy parameters tuned from Urðr outcome data by offline analysis;
  the "RL" claim is made only for what is actually implemented, and
  labelled with its method.
- **Y-152** A/B routing trials: new policies run in shadow against the
  champion policy on live shapes; promotion needs measured wins.
- **Y-153** Specialisation drift: models that keep winning a kind get
  more of it; the mesh develops experts the way a village does.
- **Y-154** Cross-device context carry: a conversation moving between
  nodes keeps its Urðr context (Y-105) — the user never repeats
  themselves to a new branch of the tree.
- **Y-155** Long-horizon tasks: research-and-write, codebase mapping —
  tasks that live for hours across many nodes, checkpointed (Y-140),
  resumable, reportable.
- **Y-156** Muspelheim mode complete: overload doctrine end-to-end —
  shed, degrade (smaller models, shorter contexts), defend criticals —
  entered and exited as mesh events with the operator told plainly.
- **Y-157** Scarcity manners: on battery/thermal limits the mesh
  prefers the plugged-in and the cool — the physical world is a
  routing input, always.
- **Y-158** Ensemble cost accounting: every pattern reports its true
  cost (node-seconds, energy estimate) against single-model baselines.
- **Y-159** Quality league: ensemble patterns graded on the Part XI
  task pack vs the best single node — patterns that don't earn their
  complexity are demoted in the registry.
- **Y-160** The Council pattern (WorldNet §5, local scale): two
  persona contexts (Y-113) debate a question through mesh tasks, a
  third summarises — avatars talking, mechanically modest, honestly
  labelled.
- **Y-161** Human-in-the-loop ensembles: a pattern step can be a
  person's approval (routed to Midgard) — the mesh knows when to ask.
- **Y-162** Deterministic tier (Hermes contract): tasks marked
  deterministic are answered without models at all where a registered
  deterministic handler exists — the mesh respects the cheapest truth.
- **Y-163** Embedding fabric: corpus embedding as a standing ensemble
  service for Urðr and consumers (Part IX's RAG draws here).
- **Y-164** Speech and vision kinds: audio/vision task kinds routed to
  capable models (Alfheim) as engines appear — contracts first,
  claims when hardware proves them.
- **Y-165** Ensemble failure honesty: a pattern that fails delivers
  its best partial with the failure named — never a confident whole
  stitched from a broken run.
- **Y-166** Part VII gate: the cascade + draft-and-review patterns beat
  the best single node on the graded pack at lower or equal cost —
  or the gate reports exactly where they didn't, with numbers.

## Part VIII — The Nine Worlds on Real Hardware (Y-167 – Y-188)

*Goal: the mesh leaves the bench. Each world-role proven on the class
of device it names — or honestly marked NOT PROVEN where the hardware
isn't in the house.*

- **Y-167** One-script install per device class (native, systemd,
  no Docker): Pi, Debian/Ubuntu x86, the VM; the script is idempotent
  and its failures are plain.
- **Y-168** Mimir as Niflheim + Hailo citizen: the Pi hosts cold
  storage and its Hailo-10 (via Aesir's road, as it matures) — the
  mesh never promises the chip beyond Aesir's own ledger.
- **Y-169** purplelongship as Jotunheim: the laptop's GPU takes the
  heavy-lift role; thermals under sustained load recorded.
- **Y-170** The VM as the everyman node: CPU-only, modest — proof the
  mesh values even a weak branch.
- **Y-171** A second Pi-class node (any spare board) as Svartalfheim
  forge worker — parallel small-model labour proven.
- **Y-172** Jetson-class: NOT PROVEN at road's start (no Jetson in the
  house); the device-class profile is written, the claim waits for
  hardware or a borrowed board.
- **Y-173** The Z13 arrival: when the ROG Flow Z13 lands, it joins as
  the mesh's strongest node; its census + a placement shift are
  recorded as the mesh visibly rebalances.
- **Y-174** Home-LAN soak: the real mesh, 72 hours, mixed workload —
  stability, memory, and gossip overhead over days, not minutes.
- **Y-175** Power-cut drills: nodes lose power mid-task (the Pi
  unplugged, honestly); the mesh's story afterward is measured.
- **Y-176** Thermal summer: sustained load on the hottest device;
  governor behaviour (Y-028) proven against real heat.
- **Y-177** Disk-full and disk-slow drills on the smallest node.
- **Y-178** The travel mesh: laptop + phone-class node on a hotspot —
  the mesh off-grid (Volmarr's nomad hedge, served).
- **Y-179** New-node welcome: a fresh device joins the home mesh from
  the one script in under a stated time, timed with a watch.
- **Y-180** Node retirement done kindly: drain, hand off roles,
  remove — no orphaned state.
- **Y-181** Hardware registry: every real device, its class, roles,
  measured fitness — the house's own page in Urðr.
- **Y-182** Cost-of-the-tree report: electricity-shaped honesty —
  node-hours and estimated watts per workload class.
- **Y-183** Accessibility of operations: the operator's door (CLI)
  usable screen-reader-first; dashboards (if any) obey the Design
  Lawbook.
- **Y-184** Field repair: a node rebuilt from its backup joins with
  identity intact (keys via Kista).
- **Y-185** Version fleet: mixed agent versions across nodes during a
  rolling upgrade — the mesh upgrades without a flag day.
- **Y-186** The neighbour test: a second household mesh on the same
  LAN stays perfectly separate (Y-097 proven in the wild).
- **Y-187** Failure anthology: every drill's story written into the
  ops book — the tree's scar tissue, kept.
- **Y-188** Part VIII gate: the five-device home mesh (Pi, laptop, VM,
  spare board, phone-profiled) holds a week of evening workloads;
  the soak + drills evidence banked; Jetson honestly still open.

## Part IX — The Workshop Weave (Y-189 – Y-208)

*Goal: the tree serves the house that grew it. Each consumer binds by
contract, and each binding is proven by that consumer doing real work
through the mesh instead of around it.*

- **Y-189** Hermes cognition binding: the Hermes hack's cognition
  contract (deterministic / local / deep tiers, forced modes) maps
  onto mesh criticality + ensemble patterns; the loopback A.E.S.I.R.
  endpoint becomes a mesh citizen.
- **Y-190** Hermes health + routing CLI parity: `hermes volmarr
  cognition` answers from mesh truth when a mesh is present, and
  says so when it isn't.
- **Y-191** Mythic Coder over the mesh: a coder crew's sub-tasks
  (research, review, grunt edits' drafting) can be dealt to mesh
  nodes; the coder's own keeper law governs the whole.
- **Y-192** Aesir fleet duty: multiple Aesir instances (Pi road,
  laptop line) presented as one capability pool with per-device
  ledgers respected.
- **Y-193** Samtal inference: hall agents draw inference through the
  mesh (text first), with per-seat scopes (Y-117) as the privacy
  spine.
- **Y-194** Rúnmál binding: the chat app's inference settings gain
  "the mesh" as a provider; personas + lorebook RAG ride Y-163.
- **Y-195** Heimdall awareness: the Second Life bridge's cognition
  (scene digest reasoning, reflex-vs-deliberation split) routes by
  latency class — reflexes local, deep thought up the tree.
- **Y-196** Kista for nodes: node keys and consumer credentials
  resolve through Kista; a node without Kista uses its protected
  local store and says so.
- **Y-197** Verdandi events: mesh lifecycle events published in the
  Verdandi shapes the Hermes plugin already emits — one event
  language for the whole workshop.
- **Y-198** The astrology engine's heavy charts and the Scribing
  Foundry's drafts as mesh consumers (background cognition tenants).
- **Y-199** Sagnaskemma's table: engine-run NPC turns and narration
  (its Part V shapes) as mesh tasks — the game drinks from the tree.
- **Y-200** Consumer conformance kit: any new consumer binds by
  passing the same contract suite (auth, scopes, provenance,
  cancellation).
- **Y-201** Priority constitution: when consumers contend, Volmarr's
  standing order decides (interactive > crews > background) — as
  data in the router policy (Y-096).
- **Y-202** The one-dashboard truth: the operator's door shows every
  consumer's traffic and the mesh's health in one place.
- **Y-203** Degradation compacts: each consumer declares its
  mesh-down behaviour (queue, local-only, refuse) — no consumer is
  surprised by a sick tree.
- **Y-204** Cross-project memory courtesy: Urðr scopes per project;
  the tree never leaks one project's past into another's present
  uninvited.
- **Y-205** The demo that matters: an evening in the house — Samtal
  chat, a coder crew building, a game NPC thinking — all served by
  the home mesh at once, metered.
- **Y-206** Consumer cost court: per-consumer mesh usage reported
  (node-seconds by device) in the operator's door.
- **Y-207** Binding documents: one integration page per consumer,
  written for the next agent who must maintain it.
- **Y-208** Part IX gate: Y-205's evening run green; every bound
  consumer passes conformance; the mesh is the house's default road
  for local inference from here.

## Part X — WorldNet Horizon (Y-209 – Y-230)

*Goal: the addendum's planetary vision, worked as honest research.
Most slices here end in findings, prototypes, or NOT PROVEN verdicts —
that is their success shape, named in advance.*

- **Y-209** Hierarchical Norns study: leaf/region/root orchestration
  simulated at 100/1,000 nodes in the wind tunnel; where the design
  bends is reported, not hidden.
- **Y-210** RATATOSKR overlay prototype: the global overlay protocol
  as a simulator-first design with interoperability notes.
- **Y-211** Cryptographic identity at scale: onboarding without a
  central registrar (web-of-trust vs transparency logs) — a decision
  paper with a prototype of the winner.
- **Y-212** Global load balancing study: surplus-to-need routing
  across households; fairness definitions argued in writing first.
- **Y-213** Planetary Urðr: federated knowledge graphs — what may be
  shared, what must never be; the privacy constitution drafted.
- **Y-214** Privacy-preserving inference research: split inference,
  redaction layers, and their true costs measured on local shapes.
- **Y-215** Níðhöggr: abuse prevention at scale — threat catalogue
  and countermeasure prototypes (rate, reputation, attestation).
- **Y-216** The Mead of Poetry: incentive-layer design study —
  written as economics, with the failure modes (gaming, plutocracy)
  named before any token exists.
- **Y-217** Avatar layer prototype (WorldNet §4): identity manifests
  + session state for a persona served by many nodes.
- **Y-218** Council protocol prototype: avatar-to-avatar structured
  dialogue over the mesh (Y-160 grown), with human moderation hooks.
- **Y-219** Creation protocol study: seeding a new personality into
  Urðr — consent, canon, and custody questions answered in writing.
- **Y-220** Federation between two real meshes: two households (or
  one household + a friend's) exchange a bounded task under the
  federation rules — the first inter-tree act, if a partner exists;
  else SIMULATED and labelled.
- **Y-221** Latency realism: inter-household shapes measured over the
  real internet (no private data) — the physics WorldNet must obey.
- **Y-222** Governance sketch: who decides in a forest of sovereign
  trees — the Heathen Third Path's autonomy ethic as protocol
  principles (villages autonomous; no federation control dynamics),
  reconciled with Samtal's federation refusal.
- **Y-223** Standards watch: MCP, AAIF, and inference-protocol
  movements tracked; the mesh speaks standards where they exist.
- **Y-224** The hostile internet threat model: WorldNet-scale
  enemies (sybil, eclipse, poisoning) catalogued with defences
  graded by evidence.
- **Y-225** Energy and equity audit: what a planetary tree would
  cost the planet and who it would serve — written plainly.
- **Y-226** A public paper (internal first): "Temporal Routing in
  Local Inference Meshes" — the project's ideas in citable form,
  Volmarr's name on it, published only at his word.
- **Y-227** Prototype gate: the WorldNet prototype mesh (simulator +
  two real nodes across a WAN-shaped link) demonstrates hierarchical
  routing once, end to end.
- **Y-228** What we refuse: a written list of WorldNet features this
  house will not build (surveillance shapes, coercive incentives) —
  refusal as a design document.
- **Y-229** Horizon review with Volmarr: which research threads earn
  a second road; the rest are banked as knowledge.
- **Y-230** Part X gate: every study delivered with its verdict class
  (PROVEN / SIMULATED / NOT PROVEN / REFUSED) — no horizon claim
  masquerades as a capability.

## Part XI — The Proof Spine (Y-231 – Y-244)

*Goal: close the way this house closes — with evidence. The tree's
claims, all of them, cashed or corrected in one final accounting.*

- **Y-231** The graded task pack: chat, code, retrieval, reasoning,
  vision-shaped tasks with rubrics — the mesh's examination paper.
- **Y-232** Baselines court: mesh vs best-single-node vs cloud-shaped
  reference numbers (cost, latency, quality) — the honest table the
  project is judged by.
- **Y-233** Full chaos battery: every drill from Parts III/VIII re-run
  as one gauntlet on the release candidate.
- **Y-234** Security audit: the threat models (Y-010, Y-224-local)
  tested by attack attempts, findings fixed or roaded with owners.
- **Y-235** Privacy audit: packet-level proof that a local mesh task
  never leaves the LAN unencrypted or at all, per its policy.
- **Y-236** Performance budgets published: routing overhead, gossip
  cost, registry scale limits — measured, versioned, regression-
  guarded.
- **Y-237** The platform matrix: every device class × role, PROVEN or
  NOT PROVEN, in the release itself (Jetson included, honestly).
- **Y-238** Docs completion: operator book, consumer binding guides,
  protocol contracts, the Norns' decision-trace explainer — all
  reconciled with shipped code.
- **Y-239** README care: the repo README extended **by hand** with
  the built system's truth — the v3.1 spec preserved as the founding
  document, nothing of Volmarr's removed.
- **Y-240** The one-evening proof: Y-205's evening re-run on the
  release candidate, start to finish, as the release's witness.
- **Y-241** Release shaping: version 1.0 of the mesh, tagged, with a
  changelog that tells the truth (proven / partial / NOT PROVEN).
- **Y-242** The roadmap's own audit: every slice marked DONE with
  evidence, PARTIAL with its road, or NOT PROVEN with its reason.
- **Y-243** Retrospective: lessons to the workshop's ledger; patterns
  worth naming as skills.
- **Y-244** Final report to Volmarr: plain language first — what the
  tree can do, where it is proven, where it is not — evidence index
  attached.

---

## 4. Order and dependencies (the short truth)

Part I before everything (contracts are the roots). Part II and III can
grow in parallel once the node and fabric contracts exist — one writer
per tree, per house law. Part IV needs II+III. Part V needs III's
replication. Part VI needs IV+V (it verifies their work). Part VII
needs IV–VI. Part VIII is continuous from Part II on (hardware proof
accrues) with its gate late. Part IX binds as consumers are ready —
Hermes first (its contract already exists). Part X is research:
anytime after Part IV, never blocking the trunk. Part XI closes.
Keeper discipline throughout: contract, build, test, bank, continue —
and every discovered bug fixed in its slice or commissioned on the
spot, never merely noted.

## 5. What this roadmap deliberately does not do

- No cloud inference, no external API dependency at runtime — the
  spec's first goal is the project's soul.
- No new inference engine: Aesir/Ollama/llama.cpp are the hearts;
  Yggdrasil is the tree that connects them.
- No Docker on any node. No accounts, no billing, no telemetry home.
- No promising the Hailo chip beyond Project Aesir's own ledger.
- No federation control dynamics: Part X's governance work begins
  from Volmarr's autonomy ethic, the same ruling that keeps Samtal's
  villages sovereign.

## 6. First steps when the word is given

1. File this roadmap into the repo (`docs/` beside the founding
   documents), extending — never overwriting — what stands there;
   the v3.1 README untouched.
2. Open the campaign: state file + a 10-minute keeper in the house
   pattern.
3. Strike Y-001 (spec reconciliation) and Y-006/Y-007 (the node and
   task contracts) first — the roots before the trunk.

*Carved 2026-10-30, at Volmarr's word, by Yrsa.*
