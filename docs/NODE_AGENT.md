# The Node Agent Skeleton — Y-021

*Part II opens: every device a citizen. This slice is the
skeleton each citizen wears — identity, config, and lifecycle —
in `src/yggdrasil/node_agent.py`. The capability census (Y-022),
the engine adapters (Y-023 – Y-025), execution (Y-029), the
health endpoint (Y-030), and the queue (Y-031) hang on this frame
as their own slices; none of them is claimed here.*

## Identity first (Y-011, enforced at start)

The agent never starts on a say-so. `start()`:

1. loads the node's keypair and certificates from the key store
   named in the config (`<node_id>.key`, `<node_id>.cert.json`,
   `mesh-root.cert.json` — Y-011's `LocalKeyStore` layout);
2. verifies the node certificate against the mesh-root
   certificate with Y-011's `verify_certificate` — schema, chain,
   issuer fingerprint, signature, validity window;
3. checks the stored key *is* the certificate's key;
4. checks the certificate agrees with the config on `node_id`
   and `device_class` — the config may not rename the node its
   certificate names, and the filename is not the identity, the
   signed body is.

Any failure is a `NodeAgentError` naming the cause, and the agent
stays down. Provisioning (creating the mesh root, issuing the
certificate, storing both) is the operator's act with Y-011's
functions, done before the service starts; the agent mints no
trust of its own. Key custody is reported in every status via
Y-011's `describe_custody()` — a local-store node says so.

## Config

One operator-written JSON file, shaped by
`contracts/schemas/node_config.schema.json` (v1):

```json
{
  "version": 1,
  "node_id": "mimir-1",
  "device_class": "pi",
  "key_store_dir": "/var/lib/yggdrasil/keys",
  "display_name": "Mimir",
  "log_file": "/var/lib/yggdrasil/agent.jsonl"
}
```

`node_id`, `device_class`, and `key_store_dir` are required;
`display_name` and `log_file` are optional. Relative paths are
read against the config file's own directory. Unknown keys,
unknown device classes, and other versions are refused — later
slices add their knobs to this schema, versioned, rather than
letting config grow wild.

## Lifecycle

```
created ──start──▶ started ──join──▶ joined ──drain──▶ draining ──stop──▶ stopped
   ▲                                                                          │
   └──────────────────────────── start (identity re-verified) ◀───────────────┘
```

- **start** — from `created` or `stopped`; identity verified
  first, every time.
- **join** — from `started`. In this slice joining is a *local*
  act: there is no fabric yet (Part III), so the node records
  its readiness in its own ledger and says so in data —
  `fabric_connected` is `false` in every status and event, and
  no peer has heard of the node. When Ratatoskr lands, `join()`
  is where the real announcement plugs in.
- **task leases** — a joined node may hold leases
  (`begin_task`/`end_task`): the lifecycle gate that execution
  (Y-029) and the queue (Y-031) will sit behind. A draining node
  takes no new leases; duplicate and unknown leases are refused.
- **drain** — from `started` or `joined`; the node sheds: no new
  leases, health names the governor shedding.
- **stop** — from `started`, `joined`, or `draining`; refused
  while leases are in flight, so a node finishes or hands off
  its work before it leaves — it never vanishes mid-task by
  accident.

Illegal moves raise `NodeAgentError` naming the current state
and the states the verb is allowed from.

## Health and status

`health()` answers the Y-006 `HealthReport` shape from lifecycle
truth: `present` while started/joined/draining, `gone`
otherwise; `governor: {shedding: true, reason: "draining"}`
while draining. It deliberately claims nothing early —
`models_loaded` comes from the model lifecycle (Y-027) when
one is wired and is empty until then,
`queue_depth` is 0 until the local queue exists (Y-031), and
the heartbeat-carrying endpoint is Y-030.

`status()` is the agent's local summary: lifecycle state, the
verified identity (once started), key fingerprint, custody,
in-flight leases, timestamps — and the honesty fields
`fabric_connected: false`, `transport: "local-only"`,
`simulated: false`.

## Observability

Every lifecycle move is a Y-013 `EventEnvelope` from the
`node_agent` component (`node.started`, `node.joined`,
`node.draining`, `node.stopped`, per-source sequence numbers) —
the first real emitter in the tree. When the config names a
`log_file`, events are also appended there through Y-013's
`LocalJsonlSink`: a local file, the whole transport.

## The native service

`deploy/yggdrasil-node.service` (native systemd, no Docker —
standing law) runs:

```
ExecStart=/usr/bin/python3 -m yggdrasil.node_agent --config /etc/yggdrasil/node.json
```

The same command with `--check` runs the whole lifecycle once
(start → join → health → drain → stop), prints the status and
health as JSON, and exits 0 — the operator's preflight before
enabling the service, and the acceptance run this slice's tests
execute as a real process. Without `--check`, the agent starts,
joins, and serves until SIGTERM/SIGINT, then drains and stops
before exiting. A config or identity failure exits 2 with the
cause on stderr — no traceback, no half-started node.

## Not built here

The model store has landed (Y-026 — an agent built with
`model_store=` lists only hash-verified models in its census),
and the model lifecycle has landed (Y-027 — an agent built
with `model_lifecycle=` loads and unloads store-verified
models into budgeted slots and reports them in `health()`);
no governor beyond drain's shedding flag (Y-028), no
task execution (Y-029), no heartbeat endpoint (Y-030), no queue
(Y-031), no fabric join (Part III), and no real hardware citizen
(Y-033 – Y-035). The skeleton has run in tests on this VM only;
nothing here is a hardware claim.
