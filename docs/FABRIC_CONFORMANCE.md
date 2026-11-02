# Fabric Conformance Kit (Y-069)

ADR-002's promise, kept: *the loser stays swappable
via the Y-069 conformance kit.* Y-049's benchmark
chose the minimal signed gossip and Y-050 built the
fabric core on it — but until this slice, a future
transport swap (MQTT, NATS, or something not yet
named) had nothing but Y-050's own acceptance suite
to copy from and no single bar to clear. This slice
(`src/yggdrasil/fabric_conformance.py`) is that bar:
`FabricConformanceKit` examines any candidate
transport before it may carry the mesh, and
`require_fabric_conformant()` is the swapping gate
in one call — a candidate that fails the kit does
not carry the mesh, however promising.

## How a run goes

The caller hands in a **factory** — a callable
`factory(signer=None, verifier=None)` returning a
fresh, empty fabric — and names what is under
examination:

- `candidate` — the transport's name, carried into
  the report;
- `simulated` — the caller's declaration about the
  wiring (see Honesty, below).

The `fabric_conformance_policy` v1 fixes the kit's
law as schema constants: `all_checks_required:
true` (a candidate may not pick the checks it
likes) and `fresh_instance_per_check: true` (every
check runs on a fresh instance from the factory, so
no check can pass on another check's leftovers).

A check the factory cannot reach — a factory that
will not accept the signing seam's wiring — is
`skipped` in the report, with the reason in its
detail: a stated absence, never a quiet pass. A
candidate failure inside a check is that check's
finding, with the exception named; a check that
itself crashes is a failed check naming the crash.
The kit raises only on its own misuse. The verdict
is a validated `fabric_conformance_report` v1
(`fabric_conformance_policy` / `fabric_conformance_check`
/ `fabric_conformance_report` schemas): conformant
only when no check failed, with one Y-013 event per
run (names and counts only).

## The fourteen checks

**Interface** — `interface_surface` (all fifteen
fabric methods callable), `attach_detach_discipline`
(a duplicate attach, a detach of a node never
attached, and traffic from an unattached sender all
refuse by raising, never manufactured into a
delivery).

**Delivery** — `direct_delivery_whole_and_exact`
(one direct message, validated as `fabric_message`
v1, identical to the message `send()` returned,
delivered exactly once), `publish_reaches_subscribers_only`
(every subscriber served, no one else; a subscribed
publisher hears its own publication),
`payload_carried_as_a_copy` (a sender mutating its
dict afterwards rewrites nothing),
`unsubscribe_stops_delivery`.

**Ordering** — `per_sender_sequence_order` (one
sender's `seq` rises from 1 in the order sent; a
second sender's sequence is its own line from 1).

**Partition** — `held_for_detached_flushed_on_attach`
(mail for a sleeping peer waits in the sender's
outbox, then flushes whole and exactly once on
attach), `partition_cut_heal_zero_loss` (traffic
across a cut is held; heal delivers it all, exactly
once), `failed_delivery_held_for_retry` (a handler
that raises is a counted failure, the message held,
and an explicit `flush` retries it whole).

**Discipline** — `payload_bounds_refused` (a
payload past the policy bound, and one that is not
JSON data, refuse before any delivery and are
counted as no traffic), `outbox_bound_refused_not_dropped`
(a full outbox refuses the *new* message while
everything already held stays held, intact — proven
against Y-050's own bound at 1,024 held messages).

**Signing** — `signing_seam_honest` (with a signer
wired, a delivered message carries the signature
over `message_digest()`; with a verifier that
refuses, the delivery fails, is counted, and the
message is held for retry — never quietly carried).

**Report** — `report_honest_counts` (the candidate's
own `report()` matches the traffic the kit itself
drove: published / sent / delivered totals, attached
nodes, topic counts, no phantom partition, and a
`simulated` word equal to the caller's declaration).

Acceptance drives the kit over Y-050's fabric
itself — 14/14, the transport the mesh runs today
clearing the bar set for its own replacement — plus
candidates broken one named way at a time (a heal
that drops held traffic, a factory that ignores the
signing seam, a report that inflates its delivered
count, a bottomless outbox) each failing exactly
the check that names the break, a bare factory
leaving the signing check a stated skip, and a
non-fabric object failing thirteen checks (the
signing check a stated skip — its factory accepts
no wiring at all) without killing the kit.

## Honesty

The kit judges the *contract*, in-process.
`simulated` on the report is the caller's
declaration about the candidate's wiring, carried
verbatim — the kit cannot see past the factory
seam. Y-050's own fabric is `simulated: true`, so
its conformance proves the API's shape and the
delivery rules only: no socket was opened, no LAN
crossed. Real carriage remains Y-062's proof, and
a swapped transport earns its own hardware proof
in Part VIII before it carries production traffic.
The MQTT and NATS configurations Y-049 recorded
remain candidates for that road, not deployments.

## Not built here

- Encrypted payloads for sensitive task kinds are
  built at Y-070 (`docs/SEALED_PAYLOADS.md`).
- No candidate transport was built or installed in
  this slice: the kit is the gate a future swap
  must pass, not the swap itself.
- No node-agent seam: as with the Part III slices
  before it, the agent gains no conformance seam;
  deployment wiring runs the kit over the transport
  it is about to carry.
