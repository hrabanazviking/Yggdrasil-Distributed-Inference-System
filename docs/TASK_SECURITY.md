# Task Security — tasks are data, never code (Y-043)

The threat model's T-04 control, enforced at the agent's door
(`src/yggdrasil/task_security.py`): a task envelope (Y-007) is a
request in data — a prompt, some texts, references by name.
Nothing in a data task is ever executed by this tree. The sandbox
(Y-029) hands the envelope to a runner *as data*; the task guard
is what keeps it that way, refusing at the door any task that
tries to smuggle code in as data or to run code without its
papers.

## The policy is data

`task_security_policy` v1 names, per node:

- **`code_kinds`** — the task kinds declared code-bearing.
  Default: none. No kind may carry code until an operator's
  deployment names one here.
- **`executable_fields`** — payload field names that count as
  executable material wherever they appear, at any depth.
  Default vocabulary: `bytecode`, `code`, `command`, `commands`,
  `eval`, `exec`, `executable`, `javascript`, `python`, `script`,
  `shell`, `shell_command`, `source_code`, `wasm`.
- **`trusted_signers`** — node ids with their Y-011 Ed25519
  public keys, handed in as deployment wiring from the nodes'
  certificates. Default: none trusted.

## The judgement

Every task is judged before a lease is taken or a queue slot is
spent. The payload is walked recursively; every executable field
found is a **finding**, named by its dotted path (`findings` —
field names only; payload content never enters a verdict, an
event, or a report). A task is *code-bearing in fact* when its
kind is a declared code kind **or** its payload carries findings.

- A data task with no findings is **allowed as data**
  (`data_task`).
- A code-bearing task is allowed only when its kind is a
  declared code kind **and** it travels with a valid
  `task_signature` v1: an Ed25519 signature by a trusted signer
  over the SHA-256 digest of the task's own id, kind, and
  payload. The signature travels beside the envelope — the way
  the pin travels with a model fetch (Y-037) — because the
  Y-007 envelope itself carries no signature field.
- Code fields under a data kind are refused **even with a
  signature**: the *kind* must explicitly say so; papers alone
  never promote data into code.

Every judgement is a validated `task_security_verdict` v1.
Refusals carry their reason and Y-007 error name:

| Reason | Error code | Meaning |
|---|---|---|
| `code_in_data_task` | `scope_denied` | executable fields under an undeclared kind |
| `code_kind_unsigned` | `scope_denied` | a declared code kind arrived without a signature |
| `signer_untrusted` | `scope_denied` | the signer is not in the policy's trusted set |
| `signature_mismatch` | `verification_failed` | the block names another task, kind, or payload digest |
| `signature_invalid` | `verification_failed` | the signature does not verify against the signer's key |

Refusals are emitted as Y-013 events (`task_security.task_refused`)
from the `node_agent` component, and signed code tasks allowed are
emitted too (`task_security.code_task_allowed`) — code running is
loud, data running is ordinary. The `task_security_report` v1
keeps every verdict, counted.

`sign_task(keypair, task, signer_node_id=...)` mints a signature
block with a Y-011 keypair; `task_payload_digest(task)` is the
digest it signs — change any byte of the payload and the digest,
and so the signature, dies.

## The agent's door

The node agent grows the seam: `task_guard=` at construction.
`execute_task` and `queue_task` consult the guard before any
lease or queue slot is spent (a `signature=` travels with the
call); a refusal raises before the task exists as far as the node
is concerned — no lease taken, no queue entry, no journal line
(a refusal at the door is not a run, Y-039's rule). A task served
from the queue was judged when it was queued; its signature
travelled with that asking. `task_security_report()` and
`status()["task_security"]` carry the ledger. Without a guard
wired, the agent judges nothing, exactly as before.

All four citizen factories wire a guard under the **default
policy** (no code kinds, no trusted signers), overridable via
`task_security_policy=` — every citizen refuses code outright
until an operator's deployment names a code kind and its
signers.

## Not built here / honesty

- This guard is a door check on shapes and signatures, not an
  operating-system confinement: it does not sandbox a runner
  that disobeys the protocol. The sandbox's own bounds are
  Y-029; OS-level isolation is claimed by no slice.
- A verified signature proves *who signed these exact bytes*,
  never that the code is good. Key custody and revocation stay
  operator duties (Y-011; Kista integration is Y-196).
- The keys are Y-011's pure-Python Ed25519 — not constant-time,
  as Y-011 states. Fabric-carried signed messages are Y-059;
  provenance-chain checking for sensitive kinds is Y-139;
  payload encryption for sensitive kinds is Y-070.
- The signature check is proven in the suite against real
  generated keypairs — the cryptography is not simulated. No
  code task has run on named hardware, and none is claimed.
