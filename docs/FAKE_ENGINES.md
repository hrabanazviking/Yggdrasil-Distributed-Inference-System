# Fake Engines — Y-015

*Deterministic stub inference engines, so routing can be proven
without models. Everything a fake engine produces is SIMULATED —
it performs no inference, sees no image, and measures nothing on
any device. Shapes, timings-by-formula, and failure behaviour are
proven here; model behaviour never is.*

## What stands

`src/yggdrasil/fake_engines.py` — three stub engines, each a
simulator handler (Y-014's plug point) with a validated
self-description shaped by
`contracts/schemas/fake_engine.schema.json`:

- **Echo (`EchoEngine`)** — the answer is the payload, reflected
  by a fixed rule: a generate task gets `SIMULATED echo: <prompt>`;
  an embedding task gets one fixture vector per text (SHA-256
  bytes mapped into [-1, 1), dimension 8 — a deterministic shape,
  **not** a semantic embedding; vector closeness says nothing
  about meaning); a vision task gets text stating plainly that
  no image was seen. A fake engine never claims a perception or
  a computation it did not perform.
- **Latency-shaped (`LatencyEngine`)** — echo's answers at a
  simulated latency that is a pure function of the task's text
  size: `base_ms + per_char_ms * chars` (defaults 10 ms + 0.5 ms
  per character). The simulator honours a handler's
  `latency_ms_for(task)`: the run's timings and the virtual
  clock advance by the shaped value instead of the node's
  constant, so a bigger task provably takes longer than a
  smaller one — in simulated milliseconds, on any machine,
  identically on every replay.
- **Failure-shaped (`FailureEngine`)** — echo's answers except
  where its declared rule fires, raising the tunnel's
  `SimulatedTaskFailure` with a named Y-007 error code (the
  simulator turns it into a `failed` ResultEnvelope). Modes:
  `always`; `on_trigger` (the task's text contains a trigger
  string); `every_nth` (every Nth call, N ≥ 2); `first_n` (the
  first N calls fail, the rest succeed — the transient shape a
  retry policy is proven against). An error code outside the
  Y-007 taxonomy is refused at construction, as are unknown
  modes, missing triggers, and negative latencies.

`make_fake_engine(kind, **config)` builds any of the three by
name and refuses unknown names. `add_engine_node(sim, node_id,
engine)` raises a simulated node running the engine, with the
Y-014 fixture census built from the engine's declared
capabilities so the tunnel's capability checks and the engine
agree.

## Determinism

No randomness, no wall clock, no ambient state anywhere in the
module. Echo output and embeddings are pure functions of the
payload; latency is a pure function of the task. The failure
engine's only state is its call count, so a fresh engine given
the same task sequence replays the same success/failure pattern
exactly (tested for every mode). This is what makes the engines
usable as routing evidence: Part IV's scheduler is graded
against timings and failures that cannot drift between runs.

## Honest limits

- Fake engines are handler fixtures, not census citizens: the
  capability census names the real wrapped engines of ADR-004
  (Aesir, Ollama, llama.cpp, OpenAI-compatible), and no fake
  engine appears there. The engine descriptor — not the census —
  is a fake engine's truth, and it says `simulated: true`.
- Shaped latency is a formula, not a measurement. It proves
  that routing logic responds to latency differences; it says
  nothing about how long any real model takes on any real
  device.
- No streaming shape of its own: stream events remain the
  simulator's (and later the fabric's) envelope discipline —
  an engine returns one result object per task, exactly as the
  Y-007 contract's terminal-result rule requires of handlers
  in the tunnel.
