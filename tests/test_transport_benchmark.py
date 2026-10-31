"""Transport benchmark tests — Y-049's acceptance.

The benchmark must prove: (a) ADR-002's three candidates,
in their mesh configurations, each pass criterion 1 in a
run 60 s partition — zero terminal results lost, registry
converged after the heal; (b) the elimination gate bites —
the same candidates stripped of persistence lose terminal
results across the split and are eliminated by name,
regardless of their latency; (c) the latency model ranks
the topologies as their hop arithmetic says — one direct
gossip hop under two brokered hops — and the chatter model
counts each candidate's own sync shape, flat for a broker,
fanout-capped for gossip; (d) the decision follows the
ADR's order and chooses minimal-gossip, with the criteria
carried in the ADR's own order and the basis in words;
(e) the run is deterministic — a fresh benchmark replays
the identical report but for its timestamp — and every
record validates against its v1 schema, with misuse
(unshaped candidate or policy, duplicate ids, no
candidates) refused by name before any race runs;
(f) every run records one validated Y-013 fabric event
with names and counts only; (g) the founding README is
untouched.

No broker was installed and no socket opened: the
candidates race as in-process models, declared SIMULATED.
"""

import copy
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.observability import validate_event
from yggdrasil.transport_benchmark import (
    CRITERIA_ORDER,
    TransportBenchmark,
    TransportBenchmarkError,
    choose,
    default_candidates,
    default_policy,
    require_transport,
    run_benchmark,
    validate_candidate,
    validate_policy,
    validate_report,
    validate_result,
)

ROOT = Path(__file__).resolve().parents[1]
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def run():
    return run_benchmark(now=lambda: NOW)


def by_id(report):
    return {r["candidate_id"]: r for r in report["results"]}


def test_default_candidates_are_the_adrs_three_validated():
    candidates = default_candidates()
    assert [c["id"] for c in candidates] == ["mqtt", "nats", "minimal-gossip"]
    for candidate in candidates:
        assert validate_candidate(candidate) is candidate or True
        assert candidate["tls_first_class"] is True
        assert candidate["failure_modes"]
    # The mesh configurations the ADR's race is run on:
    # persistence is what keeps a broker honest under
    # partition, and gossip's is its outbox.
    assert {c["id"]: c["persistence"] for c in candidates} == {
        "mqtt": "persistent-session",
        "nats": "durable-stream",
        "minimal-gossip": "store-and-forward",
    }
    assert {c["id"]: c["topology"] for c in candidates} == {
        "mqtt": "broker",
        "nats": "broker",
        "minimal-gossip": "peer-mesh",
    }


def test_default_policy_is_the_adrs_race():
    policy = validate_policy(default_policy())
    assert policy["partition_seconds"] == 60.0  # the ADR's split
    assert policy["node_counts"] == [3, 10, 30]  # the ADR's chatter sizes
    assert policy["latency_tasks"] >= 1


def test_every_default_candidate_passes_criterion_1():
    report = run()
    assert validate_report(report) is report or True
    for result in report["results"]:
        assert validate_result(result) is result or True
        c1 = result["criterion1_partition"]
        assert c1["passed"] is True
        assert c1["lost_terminal_results"] == 0
        assert c1["terminal_results_sent"] == 12  # 3 nodes × 4 per node
        assert c1["registrations_converged"] is True
        assert result["eliminated"] is False
        assert result["elimination_reason"] is None
        assert result["simulated"] is True


def test_convergence_waits_for_the_candidates_sync_cycle():
    results = by_id(run())
    assert results["minimal-gossip"]["criterion1_partition"][
        "convergence_seconds_after_heal"
    ] == 5.0
    assert results["mqtt"]["criterion1_partition"][
        "convergence_seconds_after_heal"
    ] == 30.0


@pytest.mark.parametrize("index", [0, 1, 2])
def test_a_candidate_without_persistence_is_eliminated(index):
    candidates = default_candidates()
    stripped = copy.deepcopy(candidates[index])
    stripped["persistence"] = "none"
    # Make it the fastest thing on the field: criterion 1
    # must still eliminate it regardless of speed.
    stripped["hop_latency_ms"] = 0.1
    report = run_benchmark([stripped], now=lambda: NOW)
    (result,) = report["results"]
    assert result["criterion1_partition"]["passed"] is False
    assert result["criterion1_partition"]["lost_terminal_results"] > 0
    assert result["criterion1_partition"]["registrations_converged"] is False
    assert result["eliminated"] is True
    assert "criterion 1" in result["elimination_reason"]
    assert report["chosen"] is None
    with pytest.raises(TransportBenchmarkError, match="chose no transport"):
        require_transport(report)


def test_choose_skips_the_eliminated_however_fast():
    candidates = default_candidates()
    stripped = copy.deepcopy(candidates[1])
    stripped["id"] = "nats-core-nostream"
    stripped["persistence"] = "none"
    stripped["hop_latency_ms"] = 0.1
    report = run_benchmark(candidates + [stripped], now=lambda: NOW)
    assert by_id(report)["nats-core-nostream"]["eliminated"] is True
    assert report["chosen"] == "minimal-gossip"


def test_latency_follows_the_hop_arithmetic():
    results = by_id(run())
    gossip = results["minimal-gossip"]["latency_ms"]
    nats = results["nats"]["latency_ms"]
    mqtt = results["mqtt"]["latency_ms"]
    # One direct hop under two brokered hops; NATS's cheaper
    # hop under MQTT's.
    assert gossip["offer_claim_p95"] < nats["offer_claim_p95"] < mqtt["offer_claim_p95"]
    assert gossip["offer_claim_median"] < gossip["offer_claim_p95"]
    for result in results.values():
        lat = result["latency_ms"]
        assert lat["result_median"] == lat["offer_claim_median"]
        assert lat["result_p95"] == lat["offer_claim_p95"]


def test_chatter_counts_each_candidates_sync_shape():
    results = by_id(run())
    # MQTT: 2 keepalive rounds/min × 1 peer × 16 B × 2 ways,
    # flat at every mesh size — the broker is the only peer.
    assert results["mqtt"]["chatter_bytes_per_node_minute"] == {
        "3": 64,
        "10": 64,
        "30": 64,
    }
    gossip = results["minimal-gossip"]["chatter_bytes_per_node_minute"]
    # Gossip: 12 rounds/min × digest 48 B × 2 ways × fanout —
    # 2 peers exist at 3 nodes, the fanout cap of 3 binds
    # from 10 on, so chatter stops growing with the mesh.
    assert gossip == {"3": 2304, "10": 3456, "30": 3456}
    nats = results["nats"]["chatter_bytes_per_node_minute"]
    assert nats == {"3": 288, "10": 288, "30": 288}


def test_the_record_decides_minimal_gossip():
    report = run()
    assert report["chosen"] == "minimal-gossip"
    assert require_transport(report) == "minimal-gossip"
    assert report["criteria_order"] == list(CRITERIA_ORDER)
    assert report["criteria_order"][0] == "correctness_under_partition"
    assert "minimal-gossip" in report["decision_basis"]
    assert report["simulated"] is True
    # Weight rides through as declared, never as measured.
    gossip = by_id(report)["minimal-gossip"]
    assert gossip["weight"] == {
        "install_size_mb_declared": 0.0,
        "resident_memory_mb_pi_declared": 6.0,
    }


def test_choose_is_pure_adr_order_over_results():
    report = run()
    chosen, basis = choose(report["results"])
    assert chosen == "minimal-gossip"
    assert "criterion 1" in basis


def test_the_run_is_deterministic_but_for_its_timestamp():
    first = run()
    later = run_benchmark(now=lambda: datetime(2026, 10, 31, 13, 0, tzinfo=UTC))
    assert first["results"] == later["results"]
    assert first["chosen"] == later["chosen"]
    assert first["at"] != later["at"]


def test_one_fabric_event_per_run_names_and_counts_only():
    bench = TransportBenchmark(now=lambda: NOW)
    bench.run()
    (event,) = bench.events
    assert validate_event(event) is event or True
    assert event["type"] == "transport.benchmark_completed"
    assert event["source"]["component"] == "fabric"
    assert event["payload"] == {
        "candidates": 3,
        "eliminated": 0,
        "chosen": "minimal-gossip",
        "simulated": True,
    }


def test_misuse_is_refused_before_any_race():
    with pytest.raises(TransportBenchmarkError):
        TransportBenchmark([])
    dup = default_candidates()
    with pytest.raises(TransportBenchmarkError, match="unique"):
        TransportBenchmark(dup + [dup[0]])
    bad = default_candidates()
    bad[0]["topology"] = "carrier-pigeon"
    with pytest.raises(TransportBenchmarkError):
        TransportBenchmark(bad)
    with pytest.raises(TransportBenchmarkError):
        TransportBenchmark(policy={"version": 1, "latency_tasks": 0})
    with pytest.raises(TransportBenchmarkError):
        validate_policy({"version": 2})


def test_the_founding_readme_is_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
