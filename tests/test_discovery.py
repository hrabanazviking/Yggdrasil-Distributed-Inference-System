"""Discovery tests — Y-052's acceptance.

The slice must prove: (a) an announcement is derived from
a node's real Y-011 certificate, and admission is gated
on that certificate — foreign-mesh, expired, and
identity-mismatched announcements are refused by name,
counted, and never enter the peer table; (b) nodes find
each other over the simulated LAN's mDNS road, by
broadcast and by query, and over the seed road, with a
dead seed counted not hidden; (c) freshness is the
hearing node's own clock — entries refresh on repeat or
newer announcements, older sequences are refused stale,
and a quiet peer expires as an event; (d) the peer table
is bounded — expired entries evicted first, a fresh peer
refused by name at a full table; (e) the policy's two
constants cannot be opened — the schema refuses a cloud
rendezvous and refuses dropping the certificate
requirement; (f) reports and events validate, carrying
names and counts only; (g) the founding README is
untouched.

The LAN is the in-process SimulatedLan: no multicast
packet is sent and no LAN is crossed, and nothing here
claims one was (that proof is Y-062's).
"""

import copy
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from yggdrasil.discovery import (
    DiscoveryError,
    DiscoveryService,
    SimulatedLan,
    build_announcement,
    default_policy,
    validate_announcement,
    validate_peer,
    validate_policy,
    validate_report,
)
from yggdrasil.identity import (
    NodeKeypair,
    create_mesh_root,
    issue_node_certificate,
)
from yggdrasil.observability import validate_event

ROOT = Path(__file__).resolve().parents[1]
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
EPOCH = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)


class Clock:
    """One shared virtual clock for a whole mesh of
    services, so a run replays identically."""

    def __init__(self):
        self.moment = EPOCH

    def __call__(self):
        return self.moment

    def advance(self, seconds):
        self.moment += timedelta(seconds=seconds)


def make_mesh(clock, node_specs, *, policy=None):
    """node_specs: (node_id, device_class, host, port).
    Returns (services dict, root_cert)."""
    root_keypair, root_cert = create_mesh_root(now=clock())
    services = {}
    for node_id, device_class, host, port in node_specs:
        keypair = NodeKeypair.generate()
        cert = issue_node_certificate(
            root_keypair,
            root_cert,
            node_id=node_id,
            node_keypair=keypair,
            device_class=device_class,
            agent_version="0.0.1",
            now=clock(),
        )
        services[node_id] = DiscoveryService(
            cert=cert,
            root_cert=root_cert,
            host=host,
            port=port,
            policy=policy,
            now=clock,
        )
    return services, root_cert


SPECS3 = [
    ("alfheim", "pi", "10.0.0.11", 7400),
    ("jotunheim", "laptop", "10.0.0.12", 7400),
    ("midgard", "vm", "10.0.0.13", 7400),
]


# -- the policy's constants are the contract ---------------------


def test_policy_constants_cannot_be_opened():
    policy = default_policy()
    assert policy["require_certificate"] is True
    assert policy["cloud_rendezvous"] is False
    with pytest.raises(DiscoveryError):
        validate_policy({**policy, "cloud_rendezvous": True})
    with pytest.raises(DiscoveryError):
        validate_policy({**policy, "require_certificate": False})
    with pytest.raises(DiscoveryError):
        validate_policy({**policy, "announcement_ttl_seconds": 0})
    with pytest.raises(DiscoveryError):
        validate_policy({**policy, "max_peers": 0})


# -- announcements are derived, not restated ---------------------


def test_announcement_derived_from_certificate():
    clock = Clock()
    services, _ = make_mesh(clock, SPECS3[:1])
    service = services["alfheim"]
    announcement = service.announce()
    validate_announcement(announcement)
    assert announcement["node_id"] == "alfheim"
    assert announcement["device_class"] == "pi"
    assert announcement["transport"] == "tls"
    assert announcement["seq"] == 1
    assert service.announce()["seq"] == 2


def test_service_refuses_a_certificate_that_does_not_verify():
    clock = Clock()
    _, root_cert = make_mesh(clock, SPECS3[:1])
    other_root_keypair, other_root_cert = create_mesh_root(now=clock())
    keypair = NodeKeypair.generate()
    foreign = issue_node_certificate(
        other_root_keypair,
        other_root_cert,
        node_id="vanaheim",
        node_keypair=keypair,
        device_class="vm",
        agent_version="0.0.1",
        now=clock(),
    )
    with pytest.raises(DiscoveryError):
        DiscoveryService(
            cert=foreign, root_cert=root_cert, host="10.0.0.99", port=7400, now=clock
        )


# -- the certificate door ----------------------------------------


def test_mdns_broadcast_finds_peers():
    clock = Clock()
    services, _ = make_mesh(clock, SPECS3)
    lan = SimulatedLan()
    for service in services.values():
        lan.attach(service)
    lan.broadcast("alfheim")
    assert services["jotunheim"].peer("alfheim") is not None
    assert services["midgard"].peer("alfheim") is not None
    entry = services["jotunheim"].peer("alfheim")
    validate_peer(entry)
    assert entry["sources"] == ["mdns"]
    assert entry["host"] == "10.0.0.11"
    # a node never discovers itself
    assert services["alfheim"].peer("alfheim") is None


def test_own_announcement_refused_by_name():
    clock = Clock()
    services, _ = make_mesh(clock, SPECS3[:1])
    own = services["alfheim"].announce()
    assert services["alfheim"].hear(own) is None
    assert services["alfheim"].report()["refused_reasons"] == {
        "own_announcement": 1
    }


def test_mdns_query_finds_the_whole_lan():
    clock = Clock()
    services, _ = make_mesh(clock, SPECS3)
    lan = SimulatedLan()
    for service in services.values():
        lan.attach(service)
    heard = services["midgard"].discover_on_lan(lan)
    assert {e["node_id"] for e in heard} == {"alfheim", "jotunheim"}
    report = services["midgard"].report()
    validate_report(report)
    assert report["mdns_queries"] == 1
    assert report["announcements_accepted"] == 2
    assert [p["node_id"] for p in report["known_peers"]] == ["alfheim", "jotunheim"]


def test_foreign_mesh_announcement_refused_by_name():
    clock = Clock()
    services, _root_cert = make_mesh(clock, SPECS3[:1])
    other_root_keypair, other_root_cert = create_mesh_root(now=clock())
    keypair = NodeKeypair.generate()
    foreign_cert = issue_node_certificate(
        other_root_keypair,
        other_root_cert,
        node_id="vanaheim",
        node_keypair=keypair,
        device_class="vm",
        agent_version="0.0.1",
        now=clock(),
    )
    announcement = build_announcement(
        foreign_cert, host="10.0.0.77", port=7400, seq=1, at=clock()
    )
    assert services["alfheim"].hear(announcement) is None
    report = services["alfheim"].report()
    assert report["known_peers"] == []
    assert report["refused_reasons"] == {"certificate_invalid": 1}


def test_identity_mismatch_refused_by_name():
    clock = Clock()
    services, _ = make_mesh(clock, SPECS3)
    announcement = services["jotunheim"].announce()
    tampered = copy.deepcopy(announcement)
    tampered["node_id"] = "midgard"  # the certificate says jotunheim
    assert services["alfheim"].hear(tampered) is None
    assert services["alfheim"].report()["refused_reasons"] == {
        "identity_mismatch": 1
    }
    tampered_fp = copy.deepcopy(announcement)
    tampered_fp["fingerprint"] = "0" * 64
    assert services["alfheim"].hear(tampered_fp) is None
    assert services["alfheim"].report()["refused_reasons"] == {
        "identity_mismatch": 2
    }


def test_malformed_announcement_counted_not_raised():
    clock = Clock()
    services, _ = make_mesh(clock, SPECS3[:1])
    assert services["alfheim"].hear({"node_id": "ghost"}) is None
    assert services["alfheim"].report()["refused_reasons"] == {
        "malformed_announcement": 1
    }


def test_expired_certificate_refused():
    clock = Clock()
    root_keypair, root_cert = create_mesh_root(now=clock())

    def cert_for(node_id, **kwargs):
        return issue_node_certificate(
            root_keypair,
            root_cert,
            node_id=node_id,
            node_keypair=NodeKeypair.generate(),
            device_class="vm",
            agent_version="0.0.1",
            now=clock(),
            **kwargs,
        )

    hearer = DiscoveryService(
        cert=cert_for("hearer"),
        root_cert=root_cert,
        host="10.0.0.13",
        port=7400,
        now=clock,
    )
    announcement = build_announcement(
        cert_for("shortlived", lifetime_days=1),
        host="10.0.0.55",
        port=7400,
        seq=1,
        at=clock(),
    )
    # inside its window the same announcement is admitted
    assert hearer.hear(announcement) is not None
    clock.advance(2 * 24 * 3600)
    # a re-hearing after the certificate's window is refused:
    # admission rides on the certificate, not on familiarity
    assert hearer.hear(announcement) is None
    assert hearer.report()["refused_reasons"] == {"certificate_invalid": 1}


# -- the seed road -------------------------------------------------


def test_seed_road_finds_a_node_the_lan_missed():
    clock = Clock()
    services, _ = make_mesh(clock, SPECS3[:2])
    lan = SimulatedLan()
    lan.attach(services["jotunheim"])  # alfheim is not broadcasting
    seeds = [{"host": "10.0.0.12", "port": 7400}]
    heard = services["alfheim"].discover_via_seeds(seeds, lan.announcement_at)
    assert [e["node_id"] for e in heard] == ["jotunheim"]
    entry = services["alfheim"].peer("jotunheim")
    assert entry["sources"] == ["seed"]
    report = services["alfheim"].report()
    assert report["seed_queries"] == 1
    assert report["seed_unreachable"] == 0


def test_dead_seed_counted_and_evented():
    clock = Clock()
    services, _ = make_mesh(clock, SPECS3[:1])
    lan = SimulatedLan()
    seeds = [{"host": "10.0.0.250", "port": 7400}]
    assert services["alfheim"].discover_via_seeds(seeds, lan.announcement_at) == []
    report = services["alfheim"].report()
    assert report["seed_unreachable"] == 1
    assert report["known_peers"] == []
    assert any(
        e["type"] == "discovery.seed_unreachable" for e in services["alfheim"].events
    )


def test_seed_entry_must_be_an_endpoint():
    clock = Clock()
    services, _ = make_mesh(clock, SPECS3[:1])
    with pytest.raises(DiscoveryError):
        services["alfheim"].discover_via_seeds(
            [{"node_id": "jotunheim"}], lambda h, p: None
        )


def test_peer_found_by_both_roads_records_both():
    clock = Clock()
    services, _ = make_mesh(clock, SPECS3[:2])
    lan = SimulatedLan()
    for service in services.values():
        lan.attach(service)
    services["alfheim"].discover_on_lan(lan)
    services["alfheim"].discover_via_seeds(
        [{"host": "10.0.0.12", "port": 7400}], lan.announcement_at
    )
    assert services["alfheim"].peer("jotunheim")["sources"] == ["mdns", "seed"]


# -- freshness on the hearer's clock -------------------------------


def test_quiet_peer_expires_as_an_event():
    clock = Clock()
    services, _ = make_mesh(clock, SPECS3[:2])
    lan = SimulatedLan()
    for service in services.values():
        lan.attach(service)
    services["alfheim"].discover_on_lan(lan)
    assert services["alfheim"].peer("jotunheim") is not None
    clock.advance(91)  # past the default 90 s TTL
    assert services["alfheim"].peers() == []
    report = services["alfheim"].report()
    assert report["peers_expired"] == 1
    assert any(
        e["type"] == "discovery.peer_expired" for e in services["alfheim"].events
    )


def test_repeat_announcement_refreshes_freshness():
    clock = Clock()
    services, _ = make_mesh(clock, SPECS3[:2])
    announcement = services["jotunheim"].announce()
    services["alfheim"].hear(announcement)
    clock.advance(80)
    services["alfheim"].hear(announcement)  # the same seq, heard again
    clock.advance(80)  # 160 s after the first hearing, 80 after the last
    assert services["alfheim"].peer("jotunheim") is not None
    clock.advance(11)
    assert services["alfheim"].peer("jotunheim") is None


def test_stale_sequence_refused():
    clock = Clock()
    services, _ = make_mesh(clock, SPECS3[:2])
    first = services["jotunheim"].announce()
    second = services["jotunheim"].announce()
    services["alfheim"].hear(second)
    assert services["alfheim"].hear(first) is None
    assert services["alfheim"].report()["refused_reasons"] == {"stale_sequence": 1}
    assert services["alfheim"].peer("jotunheim")["seq"] == 2


def test_changed_endpoint_taken_from_newer_announcement():
    clock = Clock()
    services, _ = make_mesh(clock, SPECS3[:2])
    services["alfheim"].hear(services["jotunheim"].announce())
    moved = copy.deepcopy(services["jotunheim"].announce())
    moved["host"] = "10.0.0.112"
    services["alfheim"].hear(moved)
    assert services["alfheim"].peer("jotunheim")["host"] == "10.0.0.112"


# -- the table is bounded ------------------------------------------


def test_full_table_refuses_new_peer_but_evicts_expired():
    clock = Clock()
    policy = {**default_policy(), "max_peers": 1}
    services, _ = make_mesh(clock, SPECS3, policy=policy)
    services["alfheim"].hear(services["jotunheim"].announce())
    assert services["alfheim"].hear(services["midgard"].announce()) is None
    assert services["alfheim"].report()["refused_reasons"] == {"peer_table_full": 1}
    # the fresh peer is never evicted to make room
    assert services["alfheim"].peer("jotunheim") is not None
    clock.advance(91)
    assert services["alfheim"].hear(services["midgard"].announce()) is not None
    assert services["alfheim"].peer("midgard") is not None


# -- events and determinism -----------------------------------------


def test_events_are_valid_y013_envelopes():
    clock = Clock()
    services, _ = make_mesh(clock, SPECS3[:2])
    lan = SimulatedLan()
    for service in services.values():
        lan.attach(service)
    lan.broadcast("jotunheim")
    events = services["alfheim"].events
    assert [e["type"] for e in events] == ["discovery.peer_found"]
    for event in events:
        validate_event(event)
        assert event["source"]["component"] == "fabric"


def test_replay_is_deterministic():
    def run():
        clock = Clock()
        services, _ = make_mesh(clock, SPECS3)
        lan = SimulatedLan()
        for service in services.values():
            lan.attach(service)
        lan.broadcast("alfheim")
        services["midgard"].discover_on_lan(lan)
        clock.advance(30)
        services["midgard"].discover_via_seeds(
            [{"host": "10.0.0.11", "port": 7400}], lan.announcement_at
        )
        return services["midgard"].report()

    def normalised(report):
        # each replay mints fresh Y-011 keypairs, so the
        # fingerprints — sha256 of those keys — differ by
        # construction; everything else must replay exactly
        report = copy.deepcopy(report)
        for peer in report["known_peers"]:
            peer["fingerprint"] = "<key fingerprint>"
        return report

    first, second = run(), run()
    assert normalised(first) == normalised(second)


def test_lan_refuses_two_services_for_one_node():
    clock = Clock()
    services, _ = make_mesh(clock, SPECS3[:1])
    lan = SimulatedLan()
    lan.attach(services["alfheim"])
    with pytest.raises(DiscoveryError):
        lan.attach(services["alfheim"])


def test_readme_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
