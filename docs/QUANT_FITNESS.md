# Quantisation Fitness — Y-038

*Per device class, which quants of a model are runnable
(registry data from Y-008) — enforced at placement time, not
discovered at crash time.*

Code: `src/yggdrasil/quant_fitness.py`. Shapes:
`contracts/schemas/quant_fitness_policy.schema.json`,
`quant_fitness_verdict.schema.json`,
`quant_fitness_report.schema.json` (all v1). Policy data:
`policies/quant_fitness.json` (`yggdrasil-quant-fitness-v1`,
registered in the Y-016 manifest as the first `policy` file).

## The two scopes

Fitness is judged in two scopes, and the difference between
them is the point:

- **Class scope** judges a quant against a device class's
  citizen-profile *floor* — the smallest machine the profile
  certifies (Y-033–Y-036), using the profile's own numbers,
  read from the profile files at judgement time: a CPU format
  must fit `min_ram_gb × governor max_memory_fraction` with
  headroom to spare; an accelerator format must match the
  accelerator kind the profile requires and fit its
  `min_vram_gb` floor; a profile's small-model ceiling
  (the phone's 1.5 GiB, Y-036) binds outright. The result is
  the registry's `runnable_on`: the conservative class
  guarantee. A class with no citizen profile (desktop,
  server, jetson, other) is judged `no_class_profile` —
  fitness not evaluated, never assumed.
- **Node scope** judges a quant against one node's measured
  census (Y-022): the accelerators actually present, the
  VRAM actually measured (no VRAM figure is
  `vram_unmeasured_on_node`, never read as plenty), the RAM
  actually measured. This is the placement-time enforcement
  scope — and it regularly disagrees with the class floor in
  the honest direction: Mimir's measured 15.828 GiB fits a
  7B Q4_K_M quant the Pi *class floor* refuses, because the
  floor certifies the smallest Pi, not this one.

## The rules are policy data

Two rules, both in `policies/quant_fitness.json`, labelled a
policy and not a fact:

- **Headroom** (`headroom_factor: 1.25` as shipped): a quant
  is fit only when `size_gb × headroom` fits the pool it
  would run in — weights are never the whole cost of running
  a model. Every verdict shows the arithmetic.
- **Format requirements**: gguf/ggml need no accelerator,
  awq/gptq/exl2 need CUDA, mlx needs Metal. A format the
  policy does not name is `format_unknown` and **not**
  runnable (`on_unknown_format: refuse`) — the mesh never
  assumes an unlisted format runs; the operator extends the
  policy when a format is adopted.

Every judgement is a validated `quant_fitness_verdict` v1:
`fit` is true only when `reasons` is empty, and every
refusal names its reason codes (`exceeds_class_ram_share`,
`exceeds_node_ram`, `accelerator_missing_on_node`,
`exceeds_class_model_ceiling`, `exceeds_class_vram_floor`,
`vram_unmeasured_on_node`, …). A model's class-scope
judgements summarise into a validated
`quant_fitness_report` v1.

## Enforcement, wired

- `QuantFitness` is one node's judge (policy + profiles +
  device class + a census dict or callable). The model
  lifecycle (Y-027) accepts it as its `fitness_checker`
  seam: after the store's hash verification and *before* any
  placement or loader call, an unfit verdict refuses the
  load by name with every reason, recorded as a
  `model.load_refused` event. The judge evaluates the store
  entry's own pinned quant — the quant the node would
  actually load, not a sibling the registry lists.
- The node agent (Y-021) accepts the same judge as
  `quant_fitness=`: `quant_verdicts(entry)` answers the
  pre-flight question for any registry entry against the
  node's census, and `status()` carries the judge's summary.
  Unwired, an agent judges nothing and carries `None`.
- All four citizen factories (Y-033–Y-036) wire the judge —
  built over the factory's own measured census — into their
  lifecycle and their agent, so a citizen refuses an unfit
  quant at its own door (proven for the Pi citizen: a gguf
  fixture loads; an AWQ fixture is refused
  `accelerator_missing_on_node` before the loader).
- The model store (Y-026) stamps registry entries only when
  a judge is handed in: `model_entries(quant_fitness=…)`
  fills `runnable_on` from the class-scope verdicts. The
  engine adapters (Y-023–Y-025) still leave it empty — the
  stamp is the judge's word, not an adapter's.

## Honesty

Fitness is arithmetic over data already in the tree —
registry entries, citizen profiles, a measured census. A
fit verdict says the numbers allow the quant on that class
floor or that node; it does **not** say an engine has run
it, and no hardware proof is claimed by any verdict. The
headroom factor is an operator's policy number, not a
measured constant. Nothing here fetches, loads, or routes:
fetching is Y-037, loading is Y-027 under this gate, and
Verðandi's use of `runnable_on` when placing *tasks* across
nodes is Part IV's word — this slice enforces at the one
placement point that exists today, the load.

## Not built here

No per-engine quant support tables (an engine that cannot
serve a format it discovered is an adapter honesty question,
Y-023–Y-025, unchanged); no fitness *measurement* (a quant
that fits but runs unusably slowly is a measured-speeds
question for the registry's graded speeds, Part IV); the
node journal has landed (Y-039 — see NODE_JOURNAL.md; it
journals runs, it does not judge them, and a load this
judge refuses before the loader is a refusal, not a run,
so it is not journaled); no graceful degradation on
accelerator loss (Y-040 — its census refresh will feed
this same judge through the callable census seam), and
no router enforcement (Part IV).
