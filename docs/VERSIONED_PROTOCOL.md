# Versioned Protocol (Y-065)

Every fabric message already carries a
`version` (Y-050), but until this slice nothing
said what happens when the two ends of a channel
do not speak the same one. The failure removed
here is the quiet kind: an old node meeting a new
message and crashing, or — worse — half-
understanding it. **A mixed-version mesh degrades
by contract, not by crash:** `ProtocolGate` in
`src/yggdrasil/versioned_protocol.py`.

## Versions are data

A validated `protocol_policy` v1 names:

- `known_versions` — the protocol versions this
  mesh has heard of (default `[1, 2]`);
- `current_version` — the version the mesh
  prefers (default 2);
- `minimum_version` — the floor below which a
  version is retired (default 1).

The policy's law is schema constants:
`downgrade_allowed` is always `true` and
`unknown_version_action` is always
`refuse_and_count` — a policy naming crash-on-
unknown or no-downgrade fails validation. The
current and minimum versions must themselves be
known, and the minimum must not pass the current.

## Nodes declare what they speak

A node enrols with the versions it speaks and
the one it prefers (default: its highest).
Enrolling in a version the policy does not know
(`version_not_known`), preferring a version the
node does not speak (`preferred_not_spoken`),
enrolling with no versions (`versions_empty`),
or enrolling twice (`already_enrolled`) is
refused by name.

## Negotiation picks the highest common word

`negotiate(a, b)` settles a pair's session
version: the highest version both nodes speak,
the policy knows, and the floor admits. A pair
with no common word gets a **refused
negotiation** — counted, evented, returned as
`None`, never raised in the mesh's face. A
session below a node's preferred version names
that node in the session's `degraded_nodes`: it
works, at the older word, and the door says so.

## Every message is judged, none crashes the gate

`carry` takes a validated `protocol_message` v1
and answers a validated `protocol_record` v1:

- **delivered** — the receiver speaks the
  message's version: the message lands in its
  inbox whole and exact;
- **degraded** — the receiver speaks only an
  older version both ends speak: a copy lands at
  that older version with the fields the older
  version does not have stripped and *named* in
  the record's `dropped_fields`. Protocol v2
  added `priority` and `hop_limit`; a v1 copy
  carries neither. The payload is never touched;
- **refused** — by name, counted apart in the
  report, delivered nowhere:
  `unknown_version` (the policy has never heard
  of it), `version_retired` (below the floor),
  `version_not_spoken` (the sender claims a
  version it never enrolled in),
  `no_common_version` (no older shared word to
  degrade to), `unknown_sender` /
  `unknown_receiver`, and `malformed` (the
  message fails its schema — or carries a field
  ahead of the version it claims: a "v1" message
  carrying v2's `priority` is not a v1 message).

`report()` answers a validated
`protocol_report` v1: the policy, every enrolled
node with its spoken and preferred versions,
every negotiated session with its version
(`null` where a pair has no common word) and its
degraded nodes, and the carriage totals —
delivered, degraded, refused — with refusals
counted apart by reason and stripped fields
counted apart by name. Records and reports name
versions and counts only; the delivered payload
itself waits in the receiver's inbox, never in a
record (Y-013's rule).

## Real fabric messages feed the gate

`carry_fabric_message` takes a validated Y-050
`fabric_message` v1 direct message as it stands
and carries it at its own version — the fabric
message's `version` *is* its protocol version,
and it carries no newer fields. A publication
has no single receiver and is refused
`malformed` at this door; topic fan-out applies
this same judgement per subscriber. The gate
composes the fabric; it does not rewire it.

## Honesty

Everything here is SIMULATED: `simulated: true`
is a schema constant on the message, the record,
and the report; the gate judges and holds
messages in-process, opens no socket, and
crosses no LAN (real carriage was Y-062's own
proof). The field map names v2's additions
because v2 is the newest word the default policy
knows; a future version registers its own fields
in `FIELDS_BY_VERSION` when it lands — until
then a version the policy does not know is
refused, not guessed at.

## Not built here

- Restart resilience and re-sync are built at
  Y-066 (`docs/RESTART_RESILIENCE.md`); the
  registrar's death drill is built at Y-067
  (`docs/REGISTRAR_DEATH_DRILL.md`); clock
  discipline Y-068.
- No node-agent seam: as with the Part III
  slices before it, the agent gains no protocol
  seam in this slice; deployment wiring enrols
  the nodes it starts with the versions they
  speak.
