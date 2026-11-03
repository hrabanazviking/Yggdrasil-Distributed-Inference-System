# Audit Tap (Y-071)

Every Part III door counts what it does, and
Y-064 gives the operator those counted standings.
What debugging sometimes needs instead is the
events themselves, in order. A feed of fabric
events is a sensitive thing to leave lying around
— it is the mesh's memory of who said what to
whom — so this slice builds it as one judged
door, `AuditTap` in `src/yggdrasil/audit_tap.py`,
whose dangers are answered in its shape.

## Off by default, on only by consent

The `audit_tap_policy` v1 fixes the law as schema
constants: `default_state: "off"`,
`consent_required: true`, `read_only: true`, and
`announce_when_on: true`. Which event types are
retained is the operator's `type_filter` (empty
retains every type), and `max_events` (default
1,024) bounds the feed.

The tap is born off. While it is off, an event
handed to it is refused `tap_off` — counted, and
retained *nowhere*: an off tap holds no feed and
learns nothing. Turning it on is a consent act:
it takes a named operator and a stated reason,
and either missing is refused `consent_missing`
with the tap staying off. For as long as the tap
is on, the consent stands in its report — the
enabling operator, the moment, and the reason —
so a tap left on (the threat model's T-10
careless-operator case) is visible at a glance,
not discoverable only by audit.

## Loud when on

Enabling and disabling are judged by named
record like every act at the door, and both are
evented (`audit_tap.enabled`,
`audit_tap.disabled`): the tap's own switching
can never be silent, and its state stands first
in its report. Any named operator of the
household may turn the tap off; the record names
who did. Turning it off does not erase the
retained feed — it stays readable, as the
debugging evidence it was gathered to be — and
the records keep the whole consent history even
after the report's standing consent is cleared.

## Read-only, in both directions

The tap holds no handle on any fabric and has no
road back into one. Events are copied in —
validated whole as Y-013 EventEnvelopes first,
a malformed one refused `malformed_event` —
held as copies, and copied out on every read,
so neither a source mutating its own dict nor a
reader mutating a returned event can alter the
feed. Nothing observed can be injected, edited,
or deleted from outside. The feed reads in
arrival order, optionally by exact event type
and with a limit; every read is counted in the
report, with the number of events it handed out.

## Bounded, and honest about its bounds

The retained feed is a ring at `max_events`:
past the bound the oldest event is dropped and
counted (`overflow_dropped`), never quietly
overwritten. A type outside the policy's filter
is refused `type_filtered` and counted apart.
Real events feed the tap without rewiring: a
component's own event list — the Y-050 fabric's
above all — is handed in with `observe` /
`observe_all`, and the suite proves the road on
a living fabric's events.

## Honesty

Everything here is SIMULATED: `simulated: true`
is a schema constant on the record and the
report. The tap judges events handed to it
in-process; it opens no socket, crosses no LAN,
and sniffs no wire — it sees exactly the events
a component's keeper hands it, no more, and
claims no more. Event payloads are whatever the
emitting component put in its Y-013 envelope
(ids, counts, and names by that contract's own
discipline); the tap adds no content of its own
beyond an event's type and its source's name in
its records.

## Not built here

- Load shapes — heartbeat and gossip overhead
  measured per node count — are built at Y-072
  (`docs/LOAD_SHAPES.md`).
- No node-agent seam: as with the Part III
  slices before it, the agent gains no tap seam
  in this slice; deployment wiring hands the tap
  its components' events and works its switch.
