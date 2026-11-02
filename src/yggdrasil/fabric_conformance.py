"""Fabric conformance kit — Y-069.

ADR-002's promise, kept: *the loser stays swappable
via the Y-069 conformance kit.* Y-049's benchmark
chose the minimal signed gossip and Y-050 built the
fabric core on it; until this slice, a future
transport swap — MQTT, NATS, or something not yet
named — would have had nothing but Y-050's own
acceptance suite to copy from and no single bar to
clear. This slice is that bar — one runner,
:class:`FabricConformanceKit`, that any candidate
transport is put through before it may carry the
mesh, checking the contract Y-050's components
actually depend on, in seven families:

- **interface** — the candidate has the fabric's
  whole surface (``attach`` / ``detach`` /
  ``attached`` / ``set_handler`` / ``subscribe`` /
  ``unsubscribe`` / ``subscribers`` / ``publish`` /
  ``send`` / ``inbox`` / ``outbox`` / ``flush`` /
  ``cut`` / ``heal`` / ``report``), and membership
  discipline holds: a duplicate attach, a detach of
  a node never attached, and traffic from an
  unattached sender are refused by a raised error,
  never quietly manufactured into a delivery.
- **delivery** — a direct message arrives whole and
  exact as a validated ``fabric_message`` v1 in the
  receiver's inbox; a publication reaches every
  subscriber and no one else, and a subscribed
  publisher hears its own publication; a payload is
  carried as a copy, so a sender mutating its own
  dict afterwards cannot rewrite a message already
  carried; an unsubscribed node hears nothing more.
- **ordering** — per-sender sequence: one sender's
  messages arrive in the order sent with ``seq``
  rising from 1, and a second sender's sequence is
  its own line, starting again at 1.
- **partition** — nothing is silently dropped: a
  message for a detached or never-attached peer is
  held in the sender's outbox and flushed whole,
  exactly once, when the peer attaches; traffic
  across a ``cut`` is held and ``heal`` delivers it
  exactly once; a direct delivery whose handler
  raises is counted as failed and held for an
  explicit ``flush`` retry, never lost.
- **discipline** — the policy's bounds bite: a
  payload past ``max_payload_bytes`` and a payload
  that is not JSON data are refused before any
  delivery, and a full outbox refuses the *new*
  message by name while everything already held
  stays held, intact.
- **signing** — the Y-011 seam holds: with a signer
  wired, a delivered message carries the signature
  over :func:`yggdrasil.fabric.message_digest`; with
  a verifier that refuses, the delivery fails,
  is counted, and the message is held for retry —
  never quietly carried unsigned-in-effect.
- **report** — the candidate's ``report()`` tells
  the truth about traffic the kit itself drove:
  attached nodes, topic counts, and the published /
  sent / delivered totals match what was observed,
  and the report's ``simulated`` word matches the
  caller's declaration.

How a run goes: the caller hands in a **factory** —
a callable ``factory(signer=None, verifier=None)``
returning a fresh, empty fabric — and names the
candidate and the honesty declaration
(``simulated``). Every check runs on a *fresh
instance* from that factory (the policy fixes this
as a schema constant, so no check can pass on
another check's leftovers), and every check is
required (also a constant — a candidate may not
pick the checks it likes). A check the factory
cannot reach — a factory that will not accept the
signing seam's wiring — is ``skipped`` with its
reason stated, never a quiet pass. A candidate
failure inside a check is that check's finding,
with the exception named; the kit raises only on
its own misuse, and :func:`require_fabric_conformant`
is the swapping gate in one call: a candidate that
fails the kit does not carry the mesh.

Honesty: the kit judges the *contract*, in-process.
``simulated`` on the report is the caller's
declaration about the candidate's wiring, carried
verbatim — the kit cannot see past the factory
seam, and a ``simulated: true`` candidate (Y-050's
own fabric included) proves the API's shape and
delivery rules only: no socket is opened, no LAN
is crossed, and nothing here proves a real wire.
Real carriage remains Y-062's proof; a swapped
transport earns its own hardware proof in Part
VIII before it carries production traffic.
"""

import copy
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.fabric import message_digest, validate_message
from yggdrasil.observability import make_event

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

_SURFACE = (
    "attach",
    "detach",
    "attached",
    "set_handler",
    "subscribe",
    "unsubscribe",
    "subscribers",
    "publish",
    "send",
    "inbox",
    "outbox",
    "flush",
    "cut",
    "heal",
    "report",
)


def _load_schema(name: str) -> dict:
    return json.loads((_SCHEMAS / name).read_text())


def _validator(name: str):
    schema = _load_schema(name)
    resources = []
    for path in sorted(_SCHEMAS.glob("*.json")):
        other = json.loads(path.read_text())
        resource = Resource.from_contents(other)
        resources.append((path.name, resource))
        resources.append((other["$id"], resource))
        resources.append((other["$id"].rsplit("/", 1)[0] + "/" + path.name, resource))
    registry = Registry().with_resources(resources)
    return validators.validator_for(schema)(schema, registry=registry)


_POLICY_VALIDATOR = _validator("fabric_conformance_policy.schema.json")
_CHECK_VALIDATOR = _validator("fabric_conformance_check.schema.json")
_REPORT_VALIDATOR = _validator("fabric_conformance_report.schema.json")


class FabricConformanceError(Exception):
    """The kit's own failure: a policy, check, or
    report that fails its schema, or — from
    :func:`require_fabric_conformant` — a candidate
    that failed the kit, with every failed check
    named. (A candidate's failures inside a run are
    data in the report, not this exception.)"""


class _SkipCheck(Exception):
    """One check cannot be reached through the
    candidate's factory; its reason is the check's
    detail, and the check is skipped, never passed."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record, kind: str):
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise FabricConformanceError(
            f"{kind} fails its schema: {errors[0].message}"
        )
    return record


def validate_policy(record: dict) -> dict:
    return _validate(_POLICY_VALIDATOR, record, "fabric conformance policy")


def validate_check(record: dict) -> dict:
    return _validate(_CHECK_VALIDATOR, record, "fabric conformance check")


def validate_report(record: dict) -> dict:
    return _validate(_REPORT_VALIDATOR, record, "fabric conformance report")


def default_policy(candidate: str = "minimal-gossip", simulated: bool = True) -> dict:
    """The kit's law as data: the candidate under
    examination, the caller's honesty declaration,
    and the two constants — every check required,
    a fresh instance for every check. A policy,
    labelled as one."""
    return validate_policy(
        {
            "version": 1,
            "candidate": candidate,
            "all_checks_required": True,
            "fresh_instance_per_check": True,
            "simulated": bool(simulated),
        }
    )


def _raises(fn) -> bool:
    """True when calling ``fn`` raises — the fabric's
    way of refusing at the door. Returning normally
    where the contract says refuse is a failure,
    however polite the return value looks."""
    try:
        fn()
    except Exception:  # noqa: BLE001 — any raised refusal is the door working; what matters is that it raised, never what it raised
        return True
    return False


class FabricConformanceKit:
    """The bar every transport candidate must clear.
    ``factory`` builds a fresh fabric per check;
    ``candidate`` names the transport and
    ``simulated`` is the caller's declaration about
    its wiring, carried verbatim into the report.
    ``now`` is injectable so reports are testable."""

    def __init__(self, factory, *, candidate="minimal-gossip",
                 simulated=True, policy=None, now=None):
        if not callable(factory):
            raise FabricConformanceError("factory must be callable")
        self._factory = factory
        self._policy = validate_policy(
            copy.deepcopy(policy)
            if policy is not None
            else default_policy(candidate, simulated)
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._events: list[dict] = []
        self._event_seq = 0

    @property
    def policy(self) -> dict:
        return copy.deepcopy(self._policy)

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    # -- candidate plumbing ------------------------------------

    def _fresh(self, signer=None, verifier=None):
        """One fresh instance from the factory. A
        factory that takes no wiring kwargs is called
        bare for ordinary checks; for the signing
        check, wiring it will not accept means the
        seam cannot be reached — a stated skip."""
        try:
            return self._factory(signer=signer, verifier=verifier)
        except TypeError:
            if signer is None and verifier is None:
                return self._factory()
            raise _SkipCheck(
                "the candidate's factory will not accept signer/verifier "
                "wiring, so the signing seam cannot be reached"
            ) from None

    @staticmethod
    def _mesh(fabric, *node_ids):
        for node_id in node_ids:
            fabric.attach(node_id)
        return fabric

    # -- interface ----------------------------------------------

    def _check_interface_surface(self):
        fabric = self._fresh()
        missing = [name for name in _SURFACE if not callable(getattr(fabric, name, None))]
        if missing:
            return "failed", f"missing fabric surface: {', '.join(missing)}"
        return "passed", f"all {len(_SURFACE)} fabric methods are callable"

    def _check_attach_detach_discipline(self):
        fabric = self._fresh()
        fabric.attach("node-a")
        if not fabric.attached("node-a"):
            return "failed", "attached() denies a node attach() just brought on"
        if not _raises(lambda: fabric.attach("node-a")):
            return "failed", "a duplicate attach returned normally instead of refusing"
        if not _raises(lambda: fabric.detach("node-ghost")):
            return "failed", "detaching a never-attached node returned normally"
        if not _raises(lambda: fabric.send("node-ghost", "node-a", {"x": 1})):
            return "failed", "a send from an unattached node returned normally"
        if not _raises(lambda: fabric.publish("node-ghost", "mesh.news", {"x": 1})):
            return "failed", "a publish from an unattached node returned normally"
        fabric.detach("node-a")
        if fabric.attached("node-a"):
            return "failed", "attached() still claims a detached node"
        return "passed", "duplicate attach, unknown detach, and unattached traffic all refuse by raising"

    # -- delivery -------------------------------------------------

    def _check_direct_delivery_whole_and_exact(self):
        fabric = self._mesh(self._fresh(), "node-a", "node-b")
        payload = {"text": "hello mesh", "n": [1, 2, 3], "nested": {"ok": True}}
        sent = fabric.send("node-a", "node-b", payload)
        validate_message(sent)
        inbox = fabric.inbox("node-b")
        if len(inbox) != 1:
            return "failed", f"receiver's inbox holds {len(inbox)} messages, expected exactly 1"
        got = inbox[0]
        validate_message(got)
        if got != sent:
            return "failed", "the delivered message differs from the message send() returned"
        if got["payload"] != payload or got["kind"] != "direct":
            return "failed", "the delivered payload or kind is not the sender's, whole and exact"
        if got["from_node"] != "node-a" or got["to_node"] != "node-b":
            return "failed", "the delivered message names the wrong ends"
        if fabric.inbox("node-b") != []:
            return "failed", "a drained inbox delivered the same message twice"
        return "passed", "one direct message, validated, whole and exact, delivered exactly once"

    def _check_publish_reaches_subscribers_only(self):
        fabric = self._mesh(self._fresh(), "node-a", "node-b", "node-c", "node-d")
        fabric.subscribe("node-b", "mesh.news")
        fabric.subscribe("node-c", "mesh.news")
        fabric.publish("node-a", "mesh.news", {"headline": "the tree grows"})
        for node_id in ("node-b", "node-c"):
            inbox = fabric.inbox(node_id)
            if len(inbox) != 1 or inbox[0]["kind"] != "publish" or inbox[0]["topic"] != "mesh.news":
                return "failed", f"subscriber {node_id} did not receive the publication whole"
            validate_message(inbox[0])
        if fabric.inbox("node-d") != []:
            return "failed", "a node subscribed to nothing received the publication"
        if fabric.inbox("node-a") != []:
            return "failed", "an unsubscribed publisher heard its own publication"
        fabric.subscribe("node-a", "mesh.news")
        fabric.publish("node-a", "mesh.news", {"headline": "second"})
        if len(fabric.inbox("node-a")) != 1:
            return "failed", "a subscribed publisher did not hear its own publication"
        return "passed", "publications reach every subscriber and no one else; a subscribed publisher hears its own"

    def _check_payload_carried_as_a_copy(self):
        fabric = self._mesh(self._fresh(), "node-a", "node-b")
        payload = {"items": ["one"], "count": 1}
        snapshot = copy.deepcopy(payload)
        fabric.send("node-a", "node-b", payload)
        payload["items"].append("two")
        payload["count"] = 99
        inbox = fabric.inbox("node-b")
        if len(inbox) != 1 or inbox[0]["payload"] != snapshot:
            return "failed", "mutating the sender's dict after send() rewrote the carried message"
        return "passed", "the carried payload is a copy; the sender's later mutations rewrite nothing"

    def _check_unsubscribe_stops_delivery(self):
        fabric = self._mesh(self._fresh(), "node-a", "node-b")
        fabric.subscribe("node-b", "mesh.news")
        fabric.unsubscribe("node-b", "mesh.news")
        if fabric.subscribers("mesh.news") != []:
            return "failed", "subscribers() still lists a node that unsubscribed"
        fabric.publish("node-a", "mesh.news", {"headline": "silence"})
        if fabric.inbox("node-b") != []:
            return "failed", "an unsubscribed node still received the topic's traffic"
        return "passed", "after unsubscribe, the topic's traffic passes the node by"

    # -- ordering ---------------------------------------------------

    def _check_per_sender_sequence_order(self):
        fabric = self._mesh(self._fresh(), "node-a", "node-b", "node-c")
        for i in range(5):
            fabric.send("node-a", "node-b", {"i": i})
        for i in range(2):
            fabric.send("node-c", "node-b", {"i": i})
        inbox = fabric.inbox("node-b")
        if len(inbox) != 7:
            return "failed", f"receiver holds {len(inbox)} messages, expected 7"
        first = inbox[:5]
        if [m["seq"] for m in first] != [1, 2, 3, 4, 5]:
            return "failed", "one sender's seq does not rise from 1 in delivery order"
        if [m["payload"]["i"] for m in first] != [0, 1, 2, 3, 4]:
            return "failed", "one sender's messages arrived out of the order sent"
        second = inbox[5:]
        if [m["seq"] for m in second] != [1, 2]:
            return "failed", "a second sender's sequence is not its own line from 1"
        return "passed", "per-sender seq rises from 1 in order; each sender's line is its own"

    # -- partition / store-and-forward --------------------------------

    def _check_held_for_detached_flushed_on_attach(self):
        fabric = self._mesh(self._fresh(), "node-a")
        fabric.send("node-a", "node-b", {"for": "a sleeping peer"})
        held = fabric.outbox("node-a")
        if len(held) != 1:
            return "failed", "a message for an unattached peer was not held in the sender's outbox"
        fabric.attach("node-b")
        inbox = fabric.inbox("node-b")
        if len(inbox) != 1 or inbox[0]["payload"] != {"for": "a sleeping peer"}:
            return "failed", "the held message was not flushed whole when the peer attached"
        if fabric.outbox("node-a") != [] or fabric.inbox("node-b") != []:
            return "failed", "the flushed message was delivered more than once or stayed held"
        return "passed", "mail for a sleeping peer is held, then flushed whole and exactly once on attach"

    def _check_partition_cut_heal_zero_loss(self):
        fabric = self._mesh(self._fresh(), "node-a", "node-b")
        fabric.subscribe("node-b", "mesh.news")
        fabric.cut(["node-b"])
        fabric.send("node-a", "node-b", {"across": "the seam"})
        fabric.publish("node-a", "mesh.news", {"across": "the seam"})
        if fabric.inbox("node-b") != []:
            return "failed", "traffic crossed a cut that should have held it"
        if len(fabric.outbox("node-a")) != 2:
            return "failed", "traffic across the cut was not held in the sender's outbox"
        delivered = fabric.heal()
        inbox = fabric.inbox("node-b")
        if delivered != 2 or len(inbox) != 2:
            return "failed", f"heal delivered {delivered}, inbox holds {len(inbox)}, expected 2 and 2"
        if fabric.report()["partitioned"]:
            return "failed", "the report still claims a partition after heal()"
        return "passed", "a cut holds traffic, a heal delivers it all, exactly once — zero lost"

    def _check_failed_delivery_held_for_retry(self):
        fabric = self._fresh()
        received = []

        def bad_handler(message):
            raise RuntimeError("the receiver is having a bad day")

        fabric.attach("node-a")
        fabric.attach("node-b", handler=bad_handler)
        fabric.send("node-a", "node-b", {"retry": "me"})
        if fabric.report()["failed_deliveries"] != 1:
            return "failed", "a handler that raised was not counted as a failed delivery"
        if len(fabric.outbox("node-a")) != 1:
            return "failed", "a failed direct delivery was lost instead of held for retry"
        fabric.set_handler("node-b", received.append)
        if fabric.flush("node-a") != 1 or len(received) != 1:
            return "failed", "flush() did not retry the held delivery into the new handler"
        if received[0]["payload"] != {"retry": "me"}:
            return "failed", "the retried delivery arrived altered"
        return "passed", "a raising handler is a counted failure; the message is held and a flush retries it whole"

    # -- discipline ------------------------------------------------------

    def _check_payload_bounds_refused(self):
        fabric = self._mesh(self._fresh(), "node-a", "node-b")
        oversized = {"blob": "x" * 300_000}
        if not _raises(lambda: fabric.send("node-a", "node-b", oversized)):
            return "failed", "a payload past the policy bound was carried instead of refused"
        if not _raises(lambda: fabric.send("node-a", "node-b", object())):
            return "failed", "a payload that is not JSON data was carried instead of refused"
        if fabric.inbox("node-b") != []:
            return "failed", "a refused payload was delivered anyway"
        report = fabric.report()
        if report["sent_direct"] != 0 or report["delivered"] != 0:
            return "failed", "the report counts refused payloads as traffic"
        return "passed", "oversized and non-JSON payloads refuse at the door; nothing is delivered or counted"

    def _check_outbox_bound_refused_not_dropped(self):
        fabric = self._mesh(self._fresh(), "node-a")
        sent = 0
        refused = False
        for i in range(4096):
            try:
                fabric.send("node-a", "node-b", {"i": i})
                sent += 1
            except Exception:  # noqa: BLE001 — the refusal's shape is the candidate's; that it refused is the check
                refused = True
                break
        if not refused:
            return "failed", f"an outbox accepted {sent} held messages with no bound refusing"
        held = fabric.outbox("node-a")
        if len(held) != sent:
            return "failed", "refusing the new message dropped messages already held"
        if held[0]["payload"] != {"i": 0} or held[-1]["payload"] != {"i": sent - 1}:
            return "failed", "the held messages were altered by the refusal at the bound"
        return "passed", f"the outbox bound bites at {sent} held messages: the new one refuses, the held ones stay intact"

    # -- signing ------------------------------------------------------------

    def _check_signing_seam_honest(self):
        def signer(node_id, digest):
            return "sig:" + digest[:16]

        def verifier(node_id, digest, signature):
            return signature == "sig:" + digest[:16]

        fabric = self._mesh(
            self._fresh(signer=signer, verifier=verifier), "node-a", "node-b"
        )
        fabric.send("node-a", "node-b", {"signed": True})
        inbox = fabric.inbox("node-b")
        if len(inbox) != 1:
            return "failed", "a properly signed message was not delivered"
        expected = "sig:" + message_digest(inbox[0])[:16]
        if inbox[0]["signature"] != expected:
            return "failed", "the delivered message does not carry the signer's signature over its digest"
        refusing = self._mesh(
            self._fresh(signer=signer, verifier=lambda n, d, s: False),
            "node-a", "node-b",
        )
        refusing.send("node-a", "node-b", {"signed": True})
        if refusing.inbox("node-b") != []:
            return "failed", "a message the verifier refused was delivered anyway"
        if refusing.report()["failed_deliveries"] < 1:
            return "failed", "a refused signature was not counted as a failed delivery"
        if len(refusing.outbox("node-a")) != 1:
            return "failed", "a refused signature's message was lost instead of held for retry"
        return "passed", "signed traffic carries its digest signature; a refused signature fails counted and is held, never carried"

    # -- report -----------------------------------------------------------------

    def _check_report_honest_counts(self):
        fabric = self._mesh(self._fresh(), "node-a", "node-b", "node-c")
        fabric.subscribe("node-b", "mesh.news")
        fabric.send("node-a", "node-b", {"one": 1})
        fabric.send("node-a", "node-c", {"two": 2})
        fabric.publish("node-a", "mesh.news", {"three": 3})
        report = fabric.report()
        expected = {"published": 1, "sent_direct": 2, "delivered": 3,
                    "queued": 0, "failed_deliveries": 0}
        for key, value in expected.items():
            if report.get(key) != value:
                return "failed", f"report counts {key}={report.get(key)!r}, the kit observed {value}"
        if report.get("attached_nodes") != ["node-a", "node-b", "node-c"]:
            return "failed", "the report's attached nodes are not the nodes the kit attached"
        if report.get("topics") != {"mesh.news": 1}:
            return "failed", "the report's topic counts are not the subscriptions the kit made"
        if report.get("simulated") != self._policy["simulated"]:
            return "failed", "the report's simulated word differs from the caller's declaration"
        if report.get("partitioned"):
            return "failed", "the report claims a partition the kit never cut"
        return "passed", "the report's counts, nodes, topics, and honesty word all match what the kit drove"

    _CHECKS = (
        ("interface_surface", "interface", "_check_interface_surface"),
        ("attach_detach_discipline", "interface", "_check_attach_detach_discipline"),
        ("direct_delivery_whole_and_exact", "delivery", "_check_direct_delivery_whole_and_exact"),
        ("publish_reaches_subscribers_only", "delivery", "_check_publish_reaches_subscribers_only"),
        ("payload_carried_as_a_copy", "delivery", "_check_payload_carried_as_a_copy"),
        ("unsubscribe_stops_delivery", "delivery", "_check_unsubscribe_stops_delivery"),
        ("per_sender_sequence_order", "ordering", "_check_per_sender_sequence_order"),
        ("held_for_detached_flushed_on_attach", "partition", "_check_held_for_detached_flushed_on_attach"),
        ("partition_cut_heal_zero_loss", "partition", "_check_partition_cut_heal_zero_loss"),
        ("failed_delivery_held_for_retry", "partition", "_check_failed_delivery_held_for_retry"),
        ("payload_bounds_refused", "discipline", "_check_payload_bounds_refused"),
        ("outbox_bound_refused_not_dropped", "discipline", "_check_outbox_bound_refused_not_dropped"),
        ("signing_seam_honest", "signing", "_check_signing_seam_honest"),
        ("report_honest_counts", "report", "_check_report_honest_counts"),
    )

    # -- the run -----------------------------------------------------------------

    def run(self) -> dict:
        """Run every check in the kit's fixed order, each
        on a fresh instance, and answer the validated
        ``fabric_conformance_report`` v1. A check that
        itself crashes is a failed check naming the
        crash — the kit never dies on the transport it
        is examining."""
        with self._lock:
            checks: list[dict] = []
            for name, category, method_name in self._CHECKS:
                try:
                    status, detail = getattr(self, method_name)()
                except _SkipCheck as skip:
                    status, detail = "skipped", str(skip)
                except Exception as exc:  # noqa: BLE001 — the kit catches every candidate failure as its check's data (the kit survives its own check's crash)
                    status, detail = (
                        "failed",
                        f"the check itself crashed: {type(exc).__name__}: {exc}",
                    )
                checks.append(
                    validate_check(
                        {
                            "version": 1,
                            "name": name,
                            "category": category,
                            "status": status,
                            "detail": detail,
                        }
                    )
                )
            totals = {
                "checks": len(checks),
                "passed": sum(1 for c in checks if c["status"] == "passed"),
                "failed": sum(1 for c in checks if c["status"] == "failed"),
                "skipped": sum(1 for c in checks if c["status"] == "skipped"),
            }
            report = validate_report(
                {
                    "version": 1,
                    "at": _fmt(self._now()),
                    "candidate": self._policy["candidate"],
                    "simulated": self._policy["simulated"],
                    "policy": copy.deepcopy(self._policy),
                    "checks": checks,
                    "totals": totals,
                    "verdict": (
                        "conformant" if totals["failed"] == 0
                        else "non_conformant"
                    ),
                }
            )
            self._event_seq += 1
            self._events.append(
                make_event(
                    type=(
                        "fabric_conformance.passed"
                        if report["verdict"] == "conformant"
                        else "fabric_conformance.failed"
                    ),
                    node_id="conformance-kit",
                    component="fabric",
                    seq=self._event_seq,
                    at=self._now(),
                    payload={
                        "candidate": report["candidate"],
                        "checks": totals["checks"],
                        "passed": totals["passed"],
                        "failed": totals["failed"],
                        "skipped": totals["skipped"],
                        "simulated": report["simulated"],
                    },
                )
            )
            return report


def run_fabric_conformance(factory, **kwargs) -> dict:
    """One candidate's conformance report, in one call —
    the kit built, run, and answered. ``kwargs`` are
    :class:`FabricConformanceKit`'s."""
    return FabricConformanceKit(factory, **kwargs).run()


def require_fabric_conformant(factory, **kwargs) -> dict:
    """The swapping gate in one call: the conformance
    report when the candidate passes,
    :class:`FabricConformanceError` naming every failed
    check when it does not — a candidate that fails
    the kit does not carry the mesh (ADR-002)."""
    report = run_fabric_conformance(factory, **kwargs)
    if report["verdict"] != "conformant":
        failed = [
            check["name"] for check in report["checks"]
            if check["status"] == "failed"
        ]
        raise FabricConformanceError(
            f"transport {report['candidate']!r} is non-conformant; "
            f"failed checks: {', '.join(failed)}"
        )
    return report


__all__ = [
    "FabricConformanceError",
    "FabricConformanceKit",
    "default_policy",
    "require_fabric_conformant",
    "run_fabric_conformance",
    "validate_check",
    "validate_policy",
    "validate_report",
]
