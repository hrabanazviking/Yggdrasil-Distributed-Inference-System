"""WAN-shaped simulation tests — Y-063's acceptance.

The slice must prove: (a) profiles are data — the
five named shapes validate, weaker/out-of-bounds
shapes fail by name, simulated is a constant;
(b) a lossless shape delivers whole and exact, in
order, once, with latency inside the shaped band;
(c) loss is real and counted — a total-loss shape
drops every message lost_after_retries after exactly
max_attempts attempts, delivered nowhere, evented;
(d) retries rescue — under a partial-loss shape some
messages need more than one attempt and the report
counts retries; (e) runs replay — same seed, profile
and traffic give identical records, a different seed
gives different weather; (f) malformed traffic is
dropped malformed before any draw, including a
direct message carried to the wrong recipient;
(g) latency shapes order honestly — an
intercontinental shape's mean delivered latency
exceeds a LAN shape's over the same traffic;
(h) loss shapes land near their declared rate over
many messages (a seeded, deterministic count);
(i) reports, records, and events validate, drops
are counted apart by reason, and the founding
README is untouched.

Everything here is SIMULATED — no socket is opened
and no WAN is crossed, and nothing claims one was.
"""

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.fabric import Fabric
from yggdrasil.observability import validate_event
from yggdrasil.wan_simulation import (
    NAMED_PROFILES,
    WanError,
    WanSimulation,
    default_profile,
    named_profile,
    validate_profile,
    validate_record,
    validate_report,
)

ROOT = Path(__file__).resolve().parents[1]
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
EPOCH = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)


def fixed_now():
    return EPOCH


def custom_profile(**overrides):
    profile = {
        "version": 1,
        "name": "test_shape",
        "base_latency_ms": 10.0,
        "jitter_ms": 5.0,
        "loss_rate": 0.0,
        "max_attempts": 3,
        "retry_backoff_ms": 20.0,
        "simulated": True,
    }
    profile.update(overrides)
    return profile


def fabric_messages(count, *, from_node="alfheim", topic="registry.updates"):
    fabric = Fabric(now=fixed_now)
    fabric.attach(from_node)
    return [fabric.publish(from_node, topic, {"n": i}) for i in range(count)]


# -- profiles as data --------------------------------------


def test_named_profiles_are_data_and_validate():
    assert sorted(NAMED_PROFILES) == [
        "congested",
        "home_wan",
        "intercontinental",
        "lan",
        "lossy_mobile",
    ]
    for name, profile in NAMED_PROFILES.items():
        assert validate_profile(profile) == profile
        assert profile["simulated"] is True
        assert profile["name"] == name
    assert default_profile()["name"] == "home_wan"
    assert named_profile("lan")["loss_rate"] == 0.0
    assert named_profile("lossy_mobile")["loss_rate"] > named_profile("congested")["loss_rate"]
    with pytest.raises(WanError):
        named_profile("carrier_pigeon")
    with pytest.raises(WanError):
        validate_profile(custom_profile(loss_rate=1.5))
    with pytest.raises(WanError):
        validate_profile(custom_profile(loss_rate=-0.1))
    with pytest.raises(WanError):
        validate_profile(custom_profile(base_latency_ms=-1.0))
    with pytest.raises(WanError):
        validate_profile(custom_profile(max_attempts=0))
    with pytest.raises(WanError):
        validate_profile(custom_profile(simulated=False))
    with pytest.raises(WanError):
        WanSimulation(custom_profile(), seed="not-an-int")


# -- lossless delivery --------------------------------------


def test_lossless_shape_delivers_whole_exact_in_order_once():
    sim = WanSimulation(named_profile("lan"), seed=7, now=fixed_now)
    messages = fabric_messages(5)
    for message in messages:
        record = sim.send(message, "midgard")
        validate_record(record)
        assert record["verdict"] == "delivered"
        assert record["attempts"] == 1
        assert 1.0 <= record["latency_ms"] <= 2.0
    inbox = sim.inbox("midgard")
    assert inbox == messages  # whole and exact, in order
    assert sim.inbox("midgard") == []  # delivery is once
    report = sim.report()
    validate_report(report)
    assert report["sent"] == 5 and report["delivered"] == 5
    assert report["dropped"] == 0 and report["attempts"] == 5
    link = report["links"][0]
    assert link["from_node"] == "alfheim" and link["to_node"] == "midgard"
    assert link["retries"] == 0
    assert 1.0 <= link["mean_latency_ms"] <= link["max_latency_ms"] <= 2.0


def test_direct_messages_cross_to_their_named_recipient():
    fabric = Fabric(now=fixed_now)
    fabric.attach("alfheim")
    message = fabric.send("alfheim", "midgard", {"hello": "wan"})
    sim = WanSimulation(named_profile("lan"), seed=3, now=fixed_now)
    record = sim.send(message, "midgard")
    assert record["verdict"] == "delivered"
    assert sim.inbox("midgard") == [message]


# -- loss, counted -------------------------------------------


def test_total_loss_drops_every_message_after_max_attempts():
    profile = custom_profile(loss_rate=1.0, max_attempts=3)
    sim = WanSimulation(profile, seed=11, now=fixed_now)
    for message in fabric_messages(4):
        record = sim.send(message, "midgard")
        assert record["verdict"] == "dropped"
        assert record["reason"] == "lost_after_retries"
        assert record["attempts"] == 3
        # 3 shaped latencies (10–15 ms each) + 2 backoffs of 20 ms
        assert 70.0 <= record["latency_ms"] <= 85.0
    assert sim.inbox("midgard") == []  # delivered nowhere
    report = sim.report()
    assert report["dropped"] == 4 and report["delivered"] == 0
    assert report["attempts"] == 12
    assert report["dropped_reasons"] == {"lost_after_retries": 4}
    link = report["links"][0]
    assert link["retries"] == 8
    assert link["mean_latency_ms"] is None and link["max_latency_ms"] is None
    types = [e["type"] for e in sim.events]
    assert types.count("wan.message_dropped") == 4
    assert types.count("wan.attempt_lost") == 12


def test_retries_rescue_some_messages_under_partial_loss():
    profile = custom_profile(loss_rate=0.5, max_attempts=5, name="coin_flip")
    sim = WanSimulation(profile, seed=42, now=fixed_now)
    records = [sim.send(m, "midgard") for m in fabric_messages(40)]
    delivered = [r for r in records if r["verdict"] == "delivered"]
    assert delivered, "some messages must cross a coin-flip shape"
    assert any(r["attempts"] > 1 for r in delivered), "some crossings must need a retry"
    report = sim.report()
    assert report["delivered"] == len(delivered)
    assert report["links"][0]["retries"] > 0
    assert len(sim.inbox("midgard")) == len(delivered)


def test_loss_rate_lands_near_its_declared_shape():
    # One attempt only, so delivery is a pure draw on
    # the declared rate — a seeded count, exact for
    # this seed, asserted as a band around 20%.
    profile = custom_profile(loss_rate=0.2, max_attempts=1, name="one_shot")
    sim = WanSimulation(profile, seed=99, now=fixed_now)
    for message in fabric_messages(300):
        sim.send(message, "midgard")
    report = sim.report()
    assert report["sent"] == 300
    rate = report["dropped"] / report["sent"]
    assert 0.10 < rate < 0.30


# -- replay ---------------------------------------------------


def test_same_seed_replays_identically_and_other_seed_differs():
    profile = named_profile("lossy_mobile")
    messages = fabric_messages(30)

    def run(seed):
        sim = WanSimulation(profile, seed=seed, now=fixed_now)
        return [sim.send(m, "midgard") for m in messages], sim.report()

    records_a, report_a = run(5)
    records_b, report_b = run(5)
    assert records_a == records_b
    assert report_a == report_b
    records_c, _ = run(6)
    assert records_c != records_a  # a different seed is different weather


# -- malformed -------------------------------------------------


def test_malformed_messages_drop_before_any_draw():
    sim = WanSimulation(named_profile("lan"), seed=2, now=fixed_now)
    record = sim.send({"not": "a fabric message"}, "midgard")
    assert record["verdict"] == "dropped" and record["reason"] == "malformed"
    assert record["attempts"] == 0 and record["latency_ms"] is None
    assert sim.send("nope", "midgard")["reason"] == "malformed"
    report = sim.report()
    validate_report(report)
    assert report["dropped_reasons"] == {"malformed": 2}
    assert sim.inbox("midgard") == []


def test_a_direct_message_to_the_wrong_recipient_is_malformed():
    fabric = Fabric(now=fixed_now)
    fabric.attach("alfheim")
    message = fabric.send("alfheim", "vanaheim", {"for": "vanaheim"})
    sim = WanSimulation(named_profile("lan"), seed=2, now=fixed_now)
    record = sim.send(message, "midgard")
    assert record["reason"] == "malformed"
    assert sim.inbox("midgard") == []


# -- shapes order honestly --------------------------------------


def test_latency_shapes_order_by_their_declared_numbers():
    messages = fabric_messages(50)

    def mean_latency(profile_name):
        sim = WanSimulation(named_profile(profile_name), seed=8, now=fixed_now)
        for message in messages:
            sim.send(message, "midgard")
        return sim.report()["links"][0]["mean_latency_ms"]

    lan_mean = mean_latency("lan")
    wan_mean = mean_latency("intercontinental")
    assert lan_mean < 2.0
    assert wan_mean > 150.0
    assert wan_mean > lan_mean * 50


def test_links_are_judged_apart_per_direction():
    sim = WanSimulation(named_profile("home_wan"), seed=4, now=fixed_now)
    forward = fabric_messages(3, from_node="alfheim")
    fabric = Fabric(now=fixed_now)
    fabric.attach("midgard")
    back = [fabric.publish("midgard", "registry.updates", {"n": i}) for i in range(2)]
    for message in forward:
        sim.send(message, "midgard")
    for message in back:
        sim.send(message, "alfheim")
    report = sim.report()
    pairs = {(link["from_node"], link["to_node"]) for link in report["links"]}
    assert pairs == {("alfheim", "midgard"), ("midgard", "alfheim")}
    assert report["sent"] == 5


# -- shapes, events, honesty -------------------------------------


def test_report_records_and_events_validate():
    sim = WanSimulation(named_profile("congested"), seed=13, now=fixed_now)
    for message in fabric_messages(10):
        sim.send(message, "midgard")
    sim.send({"bad": True}, "midgard")
    report = sim.report()
    validate_report(report)
    assert report["simulated"] is True
    assert report["seed"] == 13
    assert report["profile"]["name"] == "congested"
    assert report["sent"] == report["delivered"] + report["dropped"]
    for event in sim.events:
        validate_event(event)
    types = [e["type"] for e in sim.events]
    assert "wan.message_delivered" in types
    assert "wan.message_dropped" in types


def test_readme_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5


def test_message_ids_are_real_uuids_in_fixtures():
    # Guard the fixture itself: fabric messages carry
    # uuid ids the records echo back by name.
    message = fabric_messages(1)[0]
    uuid.UUID(message["message_id"])
    sim = WanSimulation(named_profile("lan"), seed=1, now=fixed_now)
    assert sim.send(message, "midgard")["message_id"] == message["message_id"]
