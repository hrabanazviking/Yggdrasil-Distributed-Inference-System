# The Operator's Door — Midgard CLI v0 (Y-017)

*Humans are never locked out of their own tree. From the first
day of the build there is a door, and through it the operator
can see the mesh's nodes, the tasks it has carried, and the
health of every branch. In the nine-worlds role model (Y-012)
Midgard's function is quoted from the spec as "User Interface /
CLI" — this slice builds that door's first, read-only form.*

## The commands

```
yggdrasil nodes            # every known node, in node_id order
yggdrasil tasks            # every recorded task, in finish order
yggdrasil health           # every node's health report
yggdrasil health <node>    # one node's health report
python -m yggdrasil …      # the same door, module form
```

Every command takes `--json` for machine-readable output. The
console script is declared in `pyproject.toml`
(`[project.scripts]`); the module form works from any source
checkout without an install step.

- **nodes** — one row per node joining identity facts (device
  class, role hats) to present-tense truth from the validated
  HealthReport: liveness, queue depth, offered capabilities,
  models present. A stopped node reads `gone` with no models
  loaded; its census facts (models on disk) are still shown,
  as census facts.
- **tasks** — the recorded ResultEnvelopes (Y-007) and nothing
  else: task id, status, the provenance triple (node, engine,
  model), total simulated time, and the error code when one is
  named. A source that has carried no tasks says exactly that.
- **health** — the HealthReports (Y-006) as recorded. Asking
  after an unknown node is an error (exit 2, the node named on
  stderr) — never a blank report, never a traceback.

## What stands behind the door

`src/yggdrasil/cli.py`'s `OperatorDoor` builds its views only
from a source's contract-shaped outputs — registry snapshot,
recorded results, health reports. At Y-017 the only source
that exists is the loopback simulator (Y-014), so with no
caller-supplied door the CLI opens onto a small built-in demo
mesh: three fake nodes (alfheim/vision, jotunheim/heavy
generation, midgard/generation+stream), a two-task fixture
workload run through the simulator's own submit path, and one
node stopped afterwards so a `gone` branch shows beside the
present ones. The demo is deterministic — fixed task ids,
virtual clock, stand-in placement — so the door shows the
same tree every time it is opened.

When Part II's node agent and Part III's fabric exist, their
sources will produce the same shapes and stand behind the
same views; the door's commands do not change.

## Honest limits

- **Everything the door shows today is SIMULATED.** Text
  output carries the SIMULATED banner and JSON carries
  `"simulated": true`; no real node has been listed by this
  door, and nothing here is evidence about any device.
- **The door is read-only in v0.** It cannot submit, cancel,
  drain, or retire — verified by test: opening every view
  leaves the source's nodes, results, and events untouched.
  Task submission from the CLI grows at Y-093 and node
  operator tools (drain, pause, inspect, retire) at Y-042,
  each with the machinery it acts on.
- **No network path exists in this module.** The door reads
  its source in-process; a door over a real, distributed mesh
  arrives with the fabric (Part III), not before.
