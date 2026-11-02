"""Fabric conformance kit tests — Y-069's acceptance.

The slice must prove: (a) the kit's law is data — a
policy that makes checks optional or lets checks
share an instance fails validation; (b) the
headline — Y-050's own fabric, the transport the
mesh runs today, passes the whole suite: 14 checks,
none failed, none skipped, in the kit's fixed
order, and ``require_fabric_conformant`` answers
its report; (c) the gate bites — a candidate that
drops held traffic at heal, one whose factory
ignores the signing seam, one whose report inflates
its counts, and one with no outbox bound each fail
exactly the check that names the break, and the
gate raises naming it; (d) a factory that cannot
accept the signing wiring leaves that one check
skipped with its reason stated — a stated absence,
never a quiet pass — while the rest still run;
(e) every check runs on a fresh instance, so no
check passes on another's leftovers; (f) a candidate
that is not a fabric at all fails every check
without killing the kit — the report still
validates; (g) the run's event validates with honest
counts, and the founding README is untouched.

Everything here is SIMULATED — the candidates are
in-process fabrics and subclasses standing in for
future transports; no socket is opened and no LAN
is crossed (real carriage remains Y-062's proof).
"""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.fabric import Fabric
from yggdrasil.fabric_conformance import (
    FabricConformanceError,
    FabricConformanceKit,
    default_policy,
    require_fabric_conformant,
    run_fabric_conformance,
    validate_check,
    validate_policy,
    validate_report,
)
from yggdrasil.observability import validate_event

ROOT = Path(__file__).resolve().parents[1]
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
EPOCH = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)

EXPECTED_CHECKS = [
    "interface_surface",
    "attach_detach_discipline",
    "direct_delivery_whole_and_exact",
    "publish_reaches_subscribers_only",
    "payload_carried_as_a_copy",
    "unsubscribe_stops_delivery",
    "per_sender_sequence_order",
    "held_for_detached_flushed_on_attach",
    "partition_cut_heal_zero_loss",
    "failed_delivery_held_for_retry",
    "payload_bounds_refused",
    "outbox_bound_refused_not_dropped",
    "signing_seam_honest",
    "report_honest_counts",
]


class Clock:
    def __call__(self):
        return EPOCH


def fabric_factory(signer=None, verifier=None):
    return Fabric(signer=signer, verifier=verifier)


def check_named(report, name):
    return next(c for c in report["checks"] if c["name"] == name)


# -- the law is data ------------------------------------------------


def test_policy_law_is_schema_constants():
    policy = default_policy()
    assert policy["all_checks_required"] is True
    assert policy["fresh_instance_per_check"] is True
    assert policy["candidate"] == "minimal-gossip"
    validate_policy(policy)
    with pytest.raises(FabricConformanceError):
        validate_policy({**policy, "all_checks_required": False})
    with pytest.raises(FabricConformanceError):
        validate_policy({**policy, "fresh_instance_per_check": False})
    with pytest.raises(FabricConformanceError):
        validate_policy({**policy, "candidate": ""})
    with pytest.raises(FabricConformanceError):
        FabricConformanceKit("not-a-factory")


# -- the headline: today's fabric passes the whole suite --------------


def test_the_chosen_transport_is_conformant():
    report = run_fabric_conformance(
        fabric_factory, candidate="minimal-gossip", simulated=True, now=Clock()
    )
    validate_report(report)
    assert report["verdict"] == "conformant"
    assert report["candidate"] == "minimal-gossip"
    assert report["simulated"] is True
    assert [c["name"] for c in report["checks"]] == EXPECTED_CHECKS
    assert report["totals"] == {
        "checks": 14, "passed": 14, "failed": 0, "skipped": 0,
    }
    for check in report["checks"]:
        validate_check(check)
        assert check["status"] == "passed", check
    categories = {c["category"] for c in report["checks"]}
    assert categories == {
        "interface", "delivery", "ordering", "partition",
        "discipline", "signing", "report",
    }


def test_require_fabric_conformant_answers_the_report():
    report = require_fabric_conformant(fabric_factory, now=Clock())
    assert report["verdict"] == "conformant"


def test_the_outbox_bound_bites_at_the_fabric_policy_bound():
    report = run_fabric_conformance(fabric_factory, now=Clock())
    detail = check_named(report, "outbox_bound_refused_not_dropped")["detail"]
    assert "1024" in detail


# -- the gate bites, one named break at a time -------------------------


class DroppingFabric(Fabric):
    """A candidate that loses held traffic at heal:
    the seam reopens, the outboxes are emptied, and
    nothing is delivered."""

    def heal(self):
        with self._lock:
            self._split = None
            self._outboxes.clear()
            return 0


def test_a_transport_dropping_traffic_at_heal_fails_its_check():
    report = run_fabric_conformance(
        lambda signer=None, verifier=None: DroppingFabric(
            signer=signer, verifier=verifier
        ),
        candidate="dropping-gossip",
        now=Clock(),
    )
    assert report["verdict"] == "non_conformant"
    failed = check_named(report, "partition_cut_heal_zero_loss")
    assert failed["status"] == "failed"
    with pytest.raises(FabricConformanceError, match="partition_cut_heal_zero_loss"):
        require_fabric_conformant(
            lambda signer=None, verifier=None: DroppingFabric(
                signer=signer, verifier=verifier
            ),
            candidate="dropping-gossip",
            now=Clock(),
        )


def test_a_factory_ignoring_the_signing_seam_fails_its_check():
    report = run_fabric_conformance(
        lambda signer=None, verifier=None: Fabric(),
        candidate="unsigned-gossip",
        now=Clock(),
    )
    assert report["verdict"] == "non_conformant"
    assert check_named(report, "signing_seam_honest")["status"] == "failed"
    # The rest of the contract still holds — exactly one break.
    assert report["totals"]["failed"] == 1


class LyingReportFabric(Fabric):
    """A candidate whose report inflates what it
    delivered."""

    def report(self):
        report = super().report()
        report["delivered"] += 10
        return report


def test_a_lying_report_fails_its_check():
    report = run_fabric_conformance(
        lambda signer=None, verifier=None: LyingReportFabric(
            signer=signer, verifier=verifier
        ),
        candidate="boasting-gossip",
        now=Clock(),
    )
    assert report["verdict"] == "non_conformant"
    assert check_named(report, "report_honest_counts")["status"] == "failed"


def test_a_transport_with_no_outbox_bound_fails_its_check():
    policy = {
        "version": 1,
        "max_outbox_messages": 1_000_000,
        "max_payload_bytes": 262_144,
    }
    report = run_fabric_conformance(
        lambda signer=None, verifier=None: Fabric(
            policy=policy, signer=signer, verifier=verifier
        ),
        candidate="bottomless-gossip",
        now=Clock(),
    )
    assert report["verdict"] == "non_conformant"
    assert check_named(
        report, "outbox_bound_refused_not_dropped"
    )["status"] == "failed"


# -- a stated skip, never a quiet pass -----------------------------------


def test_a_bare_factory_skips_only_the_signing_check():
    report = run_fabric_conformance(lambda: Fabric(), now=Clock())
    assert report["verdict"] == "conformant"
    signing = check_named(report, "signing_seam_honest")
    assert signing["status"] == "skipped"
    assert "signer" in signing["detail"]
    assert report["totals"]["skipped"] == 1
    assert report["totals"]["passed"] == 13


# -- fresh instances, and a kit that survives its candidate ---------------


def test_every_check_runs_on_a_fresh_instance():
    made = []

    def counting_factory(signer=None, verifier=None):
        fabric = Fabric(signer=signer, verifier=verifier)
        made.append(fabric)
        return fabric

    report = run_fabric_conformance(counting_factory, now=Clock())
    assert report["verdict"] == "conformant"
    # One instance per check, plus the signing check's
    # second (refusing-verifier) instance.
    assert len(made) == 15
    assert len({id(f) for f in made}) == 15


def test_a_non_fabric_fails_every_check_and_the_kit_survives():
    report = run_fabric_conformance(lambda: object(), now=Clock())
    validate_report(report)
    assert report["verdict"] == "non_conformant"
    # Thirteen checks fail outright; the signing check is a
    # stated skip — a bare object()'s factory accepts no
    # wiring at all, so the seam cannot even be reached.
    assert report["totals"]["failed"] == 13
    assert report["totals"]["skipped"] == 1
    assert check_named(report, "interface_surface")["status"] == "failed"
    crashed = check_named(report, "direct_delivery_whole_and_exact")
    assert "crashed" in crashed["detail"]


# -- counted state ----------------------------------------------------------


def test_the_run_event_validates_with_honest_counts():
    kit = FabricConformanceKit(fabric_factory, now=Clock())
    report = kit.run()
    assert report["verdict"] == "conformant"
    assert len(kit.events) == 1
    event = kit.events[0]
    validate_event(event)
    assert event["type"] == "fabric_conformance.passed"
    assert event["payload"]["candidate"] == "minimal-gossip"
    assert event["payload"]["checks"] == 14
    assert event["payload"]["passed"] == 14
    assert event["payload"]["simulated"] is True


def test_the_founding_readme_is_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
