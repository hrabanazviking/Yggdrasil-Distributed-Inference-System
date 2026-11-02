"""Registry service tests — Y-053's acceptance.

The slice must prove: (a) a node registers its census +
models at any replica, the census must name the
registering node itself, and refusals (malformed,
census mismatch, duplicate model, stale sequence) are
named, counted, and evented; (b) the registry is
replicated — the policy's constants cannot be opened,
every replica holds the whole truth, sync converges
both ways whichever order it happens in, and killing
the replica that first accepted a registration loses
nothing (the registrar's death drill); (c) sequences
are freshness — a newer registration replaces the old
whole, the same sequence with different facts is a
counted conflict settled deterministically on every
replica; (d) withdrawal is a tombstone — it removes the
entry, travels on sync, blocks resurrection by an old
registration, and a stale withdrawal cannot kill a
fresher entry; (e) queries answer from a replica's own
copy (by node, by model, by capability); (f) the table
is bounded by name; (g) reports and events validate;
(h) the founding README is untouched.

Replication here is the in-process sync model: no
socket is opened and no LAN is crossed, and nothing
here claims one was (real carriage is Y-062's).
"""

import copy
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from yggdrasil.observability import validate_event
from yggdrasil.registry_service import (
    RegistryError,
    RegistryService,
    build_registration,
    default_policy,
    validate_entry,
    validate_policy,
    validate_registration,
    validate_report,
)
from yggdrasil.simulator import make_fake_census

ROOT = Path(__file__).resolve().parents[1]
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
EPOCH = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.moment = EPOCH

    def __call__(self):
        return self.moment

    def advance(self, seconds):
        self.moment += timedelta(seconds=seconds)


def model(name="fixture-7b", capabilities=("generate", "stream")):
    return {
        "name": name,
        "family": "fixture",
        "quants": [{"name": "Q4_K_M", "format": "gguf", "size_gb": 4.0}],
        "capabilities": list(capabilities),
        "runnable_on": [],
    }


def registration(node_id, seq=1, *, models=None, clock=None, capabilities=("generate",)):
    clock = clock or Clock()
    chosen = [model()] if models is None else models
    census = make_fake_census(
        node_id,
        models=tuple(m["name"] for m in chosen),
        capabilities=capabilities,
    )
    return build_registration(
        node_id, seq=seq, census=census, models=chosen, at=clock()
    )


def service(node_id="alfheim", clock=None, policy=None):
    return RegistryService(
        registry_node_id=node_id, policy=policy, now=clock or Clock()
    )


# -- the policy's constants are the contract ---------------------


def test_policy_constants_cannot_be_opened():
    policy = default_policy()
    assert policy["replicated"] is True
    assert policy["single_registrar_allowed"] is False
    with pytest.raises(RegistryError):
        validate_policy({**policy, "replicated": False})
    with pytest.raises(RegistryError):
        validate_policy({**policy, "single_registrar_allowed": True})
    with pytest.raises(RegistryError):
        validate_policy({**policy, "max_entries": 0})


# -- registration ---------------------------------------------------


def test_registration_is_derived_from_the_census_owner():
    reg = registration("alfheim")
    validate_registration(reg)
    assert reg["census"]["node_id"] == "alfheim"
    other = make_fake_census("jotunheim")
    with pytest.raises(RegistryError):
        build_registration("alfheim", seq=1, census=other, models=[], at=EPOCH)


def test_register_stores_census_and_models():
    svc = service()
    entry = svc.register(registration("alfheim"))
    validate_entry(entry)
    assert entry["node_id"] == "alfheim"
    assert entry["origin_registry"] == "alfheim"
    assert [m["name"] for m in entry["models"]] == ["fixture-7b"]
    assert svc.entry("alfheim") == entry
    assert svc.entry("ghost") is None
    report = svc.report()
    validate_report(report)
    assert report["entry_count"] == 1
    assert report["model_count"] == 1
    assert report["registrations_accepted"] == 1


def test_register_local_issues_rising_sequences():
    svc = service("midgard")
    census = make_fake_census("midgard", models=("fixture-7b",))
    first = svc.register_local(census, [model()])
    second = svc.register_local(census, [model()])
    assert (first["seq"], second["seq"]) == (1, 2)
    assert svc.entry("midgard")["seq"] == 2


def test_census_mismatch_refused_by_name():
    svc = service()
    reg = registration("alfheim")
    reg["census"] = make_fake_census("jotunheim")
    assert svc.register(reg) is None
    assert svc.report()["refused_reasons"] == {"census_node_mismatch": 1}
    assert svc.entries() == []


def test_malformed_and_duplicate_refused_by_name():
    svc = service()
    assert svc.register({"node_id": "ghost"}) is None
    dup = registration("alfheim", models=[model(), model()])
    assert svc.register(dup) is None
    assert svc.report()["refused_reasons"] == {
        "duplicate_model": 1,
        "malformed_registration": 1,
    }


def test_newer_registration_replaces_whole_stale_refused():
    svc = service()
    svc.register(registration("alfheim", seq=1))
    newer = registration(
        "alfheim", seq=2, models=[model(), model("embed-1", ("embed",))]
    )
    entry = svc.register(newer)
    assert entry["seq"] == 2
    assert entry["first_registered_at"] == entry["last_registered_at"]
    assert len(entry["models"]) == 2
    assert svc.register(registration("alfheim", seq=1)) is None
    assert svc.report()["refused_reasons"] == {"stale_sequence": 1}


def test_same_sequence_same_facts_is_idempotent():
    svc = service()
    reg = registration("alfheim", seq=1)
    svc.register(reg)
    again = svc.register(copy.deepcopy(reg))
    assert again is not None and again["seq"] == 1
    assert svc.report()["registrations_accepted"] == 2
    assert svc.report()["entry_count"] == 1


def test_same_sequence_different_facts_is_a_conflict():
    svc = service()
    svc.register(registration("alfheim", seq=1))
    clash = registration("alfheim", seq=1, models=[model("other-1")])
    assert svc.register(clash) is None
    report = svc.report()
    assert report["refused_reasons"] == {"conflicting_sequence": 1}
    assert report["conflicts"] == 1
    # the first registration's facts stand at its own replica
    assert [m["name"] for m in svc.entry("alfheim")["models"]] == ["fixture-7b"]


def test_registry_full_refuses_by_name():
    policy = {**default_policy(), "max_entries": 1}
    svc = service(policy=policy)
    svc.register(registration("alfheim"))
    assert svc.register(registration("jotunheim")) is None
    assert svc.report()["refused_reasons"] == {"registry_full": 1}
    # an update to a held entry is not a new entry
    assert svc.register(registration("alfheim", seq=2)) is not None


# -- queries ----------------------------------------------------------


def test_queries_by_model_and_capability():
    svc = service()
    svc.register(registration("alfheim", capabilities=("generate", "embed")))
    svc.register(
        registration(
            "jotunheim",
            models=[model("vision-1", ("vision",))],
            capabilities=("vision",),
        )
    )
    assert svc.nodes_with_model("fixture-7b") == ["alfheim"]
    assert svc.nodes_with_model("vision-1") == ["jotunheim"]
    assert svc.nodes_with_model("absent") == []
    assert svc.nodes_with_capability("embed") == ["alfheim"]
    assert svc.nodes_with_capability("vision") == ["jotunheim"]


# -- replication --------------------------------------------------------


def test_sync_converges_both_ways():
    a, b = service("alfheim"), service("jotunheim")
    a.register(registration("alfheim"))
    b.register(registration("jotunheim"))
    a.sync_with(b)
    assert [e["node_id"] for e in a.entries()] == ["alfheim", "jotunheim"]
    assert a.entries() == b.entries()
    assert a.report()["entries_learned_via_replication"] == 1
    assert b.report()["syncs_performed"] == 1


def test_registrars_death_drill_loses_nothing():
    a, b = service("alfheim"), service("jotunheim")
    a.register(registration("midgard"))
    a.sync_with(b)
    del a  # the replica that first accepted it is gone
    assert b.entry("midgard") is not None
    fresh = service("nidavellir")
    fresh.sync_with(b)
    entry = fresh.entry("midgard")
    assert entry is not None
    assert entry["origin_registry"] == "alfheim"  # provenance, not authority
    assert [m["name"] for m in entry["models"]] == ["fixture-7b"]


def test_sync_settles_conflicts_deterministically():
    a, b = service("alfheim"), service("jotunheim")
    a.register(registration("midgard", seq=1))
    b.register(registration("midgard", seq=1, models=[model("other-1")]))
    a.sync_with(b)
    assert a.entries() == b.entries()
    assert a.report()["conflicts"] + b.report()["conflicts"] >= 1


def test_sync_convergence_does_not_depend_on_order():
    def run(order):
        replicas = {name: service(name) for name in ("alfheim", "jotunheim", "midgard")}
        replicas["alfheim"].register(registration("alfheim"))
        replicas["jotunheim"].register(registration("jotunheim", seq=1))
        replicas["midgard"].register(registration("midgard"))
        for left, right in order:
            replicas[left].sync_with(replicas[right])
        return replicas["alfheim"].entries()

    forward = run([("alfheim", "jotunheim"), ("jotunheim", "midgard"), ("alfheim", "midgard")])
    reverse = run([("midgard", "jotunheim"), ("jotunheim", "alfheim"), ("midgard", "alfheim")])
    assert forward == reverse
    assert [e["node_id"] for e in forward] == ["alfheim", "jotunheim", "midgard"]


def test_replica_cannot_sync_with_itself():
    svc = service()
    with pytest.raises(RegistryError):
        svc.sync_with(svc)


# -- withdrawal tombstones ------------------------------------------------


def test_withdrawal_removes_and_tombstone_blocks_resurrection():
    svc = service()
    reg = registration("alfheim", seq=1)
    svc.register(reg)
    assert svc.withdraw("alfheim", seq=2) is True
    assert svc.entry("alfheim") is None
    assert svc.register(copy.deepcopy(reg)) is None
    assert svc.report()["refused_reasons"] == {"stale_sequence": 1}
    assert svc.report()["withdrawn_node_ids"] == ["alfheim"]
    # a genuinely newer registration is the node's own return
    assert svc.register(registration("alfheim", seq=3)) is not None


def test_stale_withdrawal_cannot_kill_a_fresher_entry():
    svc = service()
    svc.register(registration("alfheim", seq=5))
    assert svc.withdraw("alfheim", seq=3) is False
    assert svc.entry("alfheim")["seq"] == 5
    assert svc.report()["refused_reasons"] == {"withdrawal_stale": 1}


def test_withdrawal_travels_on_sync():
    a, b = service("alfheim"), service("jotunheim")
    reg = registration("midgard", seq=1)
    a.register(reg)
    a.sync_with(b)
    b.withdraw("midgard", seq=2)
    b.sync_with(a)
    assert a.entry("midgard") is None
    assert a.report()["withdrawn_node_ids"] == ["midgard"]
    assert a.register(copy.deepcopy(reg)) is None


# -- events and determinism -------------------------------------------------


def test_events_are_valid_y013_envelopes():
    svc = service()
    svc.register(registration("alfheim"))
    svc.sync_with(service("jotunheim"))
    types = [e["type"] for e in svc.events]
    assert "registry.registered" in types
    for event in svc.events:
        validate_event(event)
        assert event["source"]["component"] == "registry"


def test_replay_is_deterministic():
    def run():
        clock = Clock()
        svc = service("alfheim", clock=clock)
        svc.register(registration("alfheim", clock=clock))
        svc.register(registration("jotunheim", clock=clock))
        clock.advance(30)
        svc.withdraw("jotunheim", seq=2)
        report = svc.report()
        report["entries"] = [
            {k: v for k, v in e.items()} for e in report["entries"]
        ]
        return report

    assert run() == run()


def test_readme_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
