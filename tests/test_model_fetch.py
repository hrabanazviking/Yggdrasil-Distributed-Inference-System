"""Model fetch duty tests — Y-037's acceptance.

The fetcher must prove: (a) the pin travels with the ask — a
request is shaped by its schema (no pin, no fetch), the
policy is validated data, and every record/report validates;
(b) the happy path — a peer's chunk stream is staged, hashed
as it lands, and stocked through the store's own add() only
when size and hash measure exactly the pin, after which the
store verifies it present and the agent's census lists it;
(c) presence short-circuits — an already-present model costs
the transport nothing, and a name registered with different
bytes refuses the fetch by name (fetching never re-points a
name); (d) policy is the gate, checked before any byte moves
— peer fetches off, a named source the policy does not name,
a model over the policy's size cap, and a fetch that would
eat the disk reserve all refuse by name with ``scope_denied``
and the transport uncalled; (e) bad bytes never enter the
store — a hash mismatch, a short stream, and an over-long
stream (cut off at the crossing chunk) all fail as
``model_hash_mismatch`` with the staging file removed and
the store exactly as stocked as before; (f) source failures
are named — unreachable (before and mid-stream) is
``engine_unreachable``, a transport's own accidents and
non-byte chunks are ``engine_error``, and no transport at
all fails honestly instead of pretending; (g) a corrupt
registered file is restored by a fetch carrying its pin;
(h) the node agent integration — fetch only from
started/joined, records in status, an unwired agent refuses
by name — and the phone citizen's small-model ceiling caps
its fetch policy; (i) the founding README is untouched.

All transports here are in-process fakes speaking chunk
streams; every fetcher built over one is constructed with
``simulated=True`` and says so in its records. No real peer
or source has carried a model for this tree.
"""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.census import ProbeSet
from yggdrasil.identity import (
    LocalKeyStore,
    NodeKeypair,
    create_mesh_root,
    issue_node_certificate,
)
from yggdrasil.model_fetch import (
    FetchSourceError,
    ModelFetcher,
    ModelFetchError,
    default_policy,
    validate_fetch_policy,
    validate_fetch_record,
    validate_fetch_request,
)
from yggdrasil.model_store import ModelStore
from yggdrasil.node_agent import NodeAgent, NodeAgentError, NodeConfig
from yggdrasil.phone_citizen import build_phone_citizen_agent

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
NODE_ID = "fetch-node-1"

WEIGHTS = b"SIMULATED weights for fetch duty - bytes with a hash, chunked"
WEIGHTS_V2 = b"SIMULATED weights for fetch duty - other bytes entirely...."
WRONG = b"X" * len(WEIGHTS)  # same length, different bytes, different hash


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_store(tmp_path) -> ModelStore:
    return ModelStore(tmp_path / "store", now=lambda: FIXED)


def request_for(data=WEIGHTS, **overrides) -> dict:
    request = {
        "version": 1,
        "name": "fixture-7b",
        "family": "fixture",
        "quant": "Q4_K_M",
        "format": "gguf",
        "capabilities": ["generate", "stream"],
        "expected_sha256": sha(data),
        "expected_size_bytes": len(data),
        "source_kind": "peer",
        "source_id": "peer-node-9",
        "locator": "models/fixture-7b-Q4_K_M.gguf",
    }
    request.update(overrides)
    return request


class ChunkTransport:
    """A fake source: serves fixed data in fixed-size chunks,
    counting calls and chunks, remembering the request."""

    def __init__(self, data=WEIGHTS, chunk_size=7):
        self.data = data
        self.chunk_size = chunk_size
        self.calls = 0
        self.chunks_yielded = 0
        self.requests = []

    def __call__(self, request):
        self.calls += 1
        self.requests.append(dict(request))

        def stream():
            for i in range(0, len(self.data), self.chunk_size):
                self.chunks_yielded += 1
                yield self.data[i : i + self.chunk_size]

        return stream()


def make_fetcher(tmp_path, transport, **kwargs) -> ModelFetcher:
    kwargs.setdefault("simulated", True)
    kwargs.setdefault("now", lambda: FIXED)
    return ModelFetcher(NODE_ID, make_store(tmp_path), transport=transport, **kwargs)


# -- shapes -------------------------------------------------------------------


def test_policy_and_request_and_record_shapes_validate(tmp_path):
    assert validate_fetch_policy(default_policy()) == default_policy()
    assert default_policy()["named_sources"] == []  # no source exists unnamed
    assert validate_fetch_request(request_for())["name"] == "fixture-7b"
    with pytest.raises(ModelFetchError, match="fails its schema"):
        validate_fetch_policy({"version": 1, "allow_peer_fetch": True})
    with pytest.raises(ModelFetchError, match="fails its schema"):
        bad = request_for()
        del bad["expected_sha256"]  # no pin, no fetch — refused by the shape
        validate_fetch_request(bad)
    with pytest.raises(ModelFetchError, match="fails its schema"):
        validate_fetch_request(request_for(expected_sha256="not-a-hash"))
    fetcher = make_fetcher(tmp_path, ChunkTransport())
    record = fetcher.fetch_model(request_for())
    assert validate_fetch_record(record) == record


def test_a_fetcher_needs_a_store():
    with pytest.raises(ModelFetchError, match="model store"):
        ModelFetcher(NODE_ID, None)


# -- the happy path -------------------------------------------------------------


def test_fetch_stocks_the_model_hash_verified(tmp_path):
    store = make_store(tmp_path)
    transport = ChunkTransport()
    fetcher = ModelFetcher(
        NODE_ID, store, transport=transport, simulated=True, now=lambda: FIXED
    )
    record = fetcher.fetch_model(request_for())
    assert record["status"] == "fetched"
    assert record["reason"] is None and record["error_code"] is None
    assert record["bytes_received"] == len(WEIGHTS)
    assert record["actual_sha256"] == sha(WEIGHTS)
    assert record["store_status"] == "verified"
    assert record["simulated"] is True
    assert record["at"] == "2026-06-01T12:00:00Z"
    # The transport was handed the validated request, locator and all.
    assert transport.calls == 1
    assert transport.requests[0]["locator"] == "models/fixture-7b-Q4_K_M.gguf"
    # Stocked through the store: present, verified, registry facts as asked.
    assert store.present_models() == ["fixture-7b"]
    entry = store.entry("fixture-7b")
    assert entry["sha256"] == sha(WEIGHTS)
    assert entry["family"] == "fixture" and entry["quant"] == "Q4_K_M"
    assert (store.root / entry["path"]).read_bytes() == WEIGHTS
    # Nothing staged remains behind.
    assert list((store.root / "incoming").glob("*.part")) == []
    # The event names the outcome, never the content.
    events = fetcher.events
    assert [e["type"] for e in events] == ["model.fetched"]
    assert events[0]["payload"]["model"] == "fixture-7b"
    assert "weights" not in str(events[0]["payload"])


def test_fetch_from_a_named_source_the_policy_names(tmp_path):
    policy = default_policy()
    policy["named_sources"] = ["operator-mirror"]
    fetcher = make_fetcher(
        tmp_path,
        ChunkTransport(),
        policy=policy,
    )
    record = fetcher.fetch_model(
        request_for(source_kind="named_source", source_id="operator-mirror")
    )
    assert record["status"] == "fetched"
    assert record["source_kind"] == "named_source"


def test_object_shaped_transport_works_too(tmp_path):
    class Source:
        def fetch_chunks(self, request):
            yield WEIGHTS[:10]
            yield bytearray(WEIGHTS[10:])  # bytearray chunks are bytes too

    fetcher = make_fetcher(tmp_path, Source())
    assert fetcher.fetch_model(request_for())["status"] == "fetched"


def test_run_ms_is_measured_on_the_injected_timer(tmp_path):
    ticks = iter([10.0, 10.25])
    fetcher = make_fetcher(tmp_path, ChunkTransport(), timer=lambda: next(ticks))
    record = fetcher.fetch_model(request_for())
    assert record["run_ms"] == 250.0


# -- presence short-circuits ------------------------------------------------------


def test_already_present_costs_the_transport_nothing(tmp_path):
    store = make_store(tmp_path)
    source = tmp_path / "seed.gguf"
    source.write_bytes(WEIGHTS)
    store.add(
        source, name="fixture-7b", family="fixture", quant="Q4_K_M", format="gguf"
    )
    transport = ChunkTransport()
    fetcher = ModelFetcher(
        NODE_ID, store, transport=transport, simulated=True, now=lambda: FIXED
    )
    record = fetcher.fetch_model(request_for())
    assert record["status"] == "already_present"
    assert record["store_status"] == "verified"
    assert record["actual_sha256"] == sha(WEIGHTS)
    assert transport.calls == 0


def test_a_name_registered_with_other_bytes_refuses_the_fetch(tmp_path):
    store = make_store(tmp_path)
    source = tmp_path / "seed.gguf"
    source.write_bytes(WEIGHTS_V2)
    store.add(
        source, name="fixture-7b", family="fixture", quant="Q4_K_M", format="gguf"
    )
    transport = ChunkTransport()
    fetcher = ModelFetcher(
        NODE_ID, store, transport=transport, simulated=True, now=lambda: FIXED
    )
    record = fetcher.fetch_model(request_for())  # asks for WEIGHTS' pin
    assert record["status"] == "refused"
    assert record["reason"] == "name_registered_with_different_bytes"
    assert record["error_code"] == "model_hash_mismatch"
    assert transport.calls == 0
    assert store.entry("fixture-7b")["sha256"] == sha(WEIGHTS_V2)  # unmoved


def test_a_corrupt_registered_file_is_restored_by_its_pin(tmp_path):
    store = make_store(tmp_path)
    source = tmp_path / "seed.gguf"
    source.write_bytes(WEIGHTS)
    entry = store.add(
        source, name="fixture-7b", family="fixture", quant="Q4_K_M", format="gguf"
    )
    copied = store.root / entry["path"]
    tampered = bytearray(copied.read_bytes())
    tampered[0] ^= 0xFF
    copied.write_bytes(bytes(tampered))
    assert store.verify("fixture-7b")["status"] == "corrupt"

    fetcher = ModelFetcher(
        NODE_ID,
        store,
        transport=ChunkTransport(),
        simulated=True,
        now=lambda: FIXED,
    )
    record = fetcher.fetch_model(request_for())
    assert record["status"] == "fetched"
    assert record["store_status"] == "verified"
    assert store.present_models() == ["fixture-7b"]
    assert copied.read_bytes() == WEIGHTS


# -- policy is the gate ------------------------------------------------------------


def test_policy_refusals_happen_before_any_byte_moves(tmp_path):
    cases = [
        (
            {"allow_peer_fetch": False},
            {},
            "peer_fetch_not_allowed",
        ),
        (
            {},
            {"source_kind": "named_source", "source_id": "unnamed-mirror"},
            "source_not_named_in_policy",
        ),
        (
            {"max_model_bytes": len(WEIGHTS) - 1},
            {},
            "model_too_large_for_policy",
        ),
    ]
    for i, (policy_over, request_over, reason) in enumerate(cases):
        policy = default_policy()
        policy.update(policy_over)
        transport = ChunkTransport()
        fetcher = make_fetcher(tmp_path / f"case{i}", transport, policy=policy)
        record = fetcher.fetch_model(request_for(**request_over))
        assert record["status"] == "refused", reason
        assert record["reason"] == reason
        assert record["error_code"] == "scope_denied"
        assert record["store_status"] == "unregistered"
        assert transport.calls == 0


def test_the_disk_reserve_refuses_a_fetch_that_would_eat_it(tmp_path):
    policy = default_policy()
    policy["min_free_disk_bytes"] = 1024
    transport = ChunkTransport()
    fetcher = make_fetcher(
        tmp_path, transport, policy=policy, disk_free=lambda: len(WEIGHTS) + 1023
    )
    record = fetcher.fetch_model(request_for())
    assert record["status"] == "refused"
    assert record["reason"] == "insufficient_disk_reserve"
    assert transport.calls == 0
    # Exactly enough room (file + reserve) is allowed.
    fetcher = make_fetcher(
        tmp_path / "roomy",
        ChunkTransport(),
        policy=policy,
        disk_free=lambda: len(WEIGHTS) + 1024,
    )
    assert fetcher.fetch_model(request_for())["status"] == "fetched"


# -- bad bytes never enter the store -------------------------------------------------


def test_hash_mismatch_bytes_are_deleted_never_stocked(tmp_path):
    store = make_store(tmp_path)
    fetcher = ModelFetcher(
        NODE_ID,
        store,
        transport=ChunkTransport(data=WRONG),
        simulated=True,
        now=lambda: FIXED,
    )
    record = fetcher.fetch_model(request_for())
    assert record["status"] == "failed"
    assert record["reason"] == "hash_mismatch"
    assert record["error_code"] == "model_hash_mismatch"
    assert record["actual_sha256"] == sha(WRONG)
    assert record["bytes_received"] == len(WRONG)
    assert record["store_status"] == "unregistered"
    assert store.entries() == []
    assert list((store.root / "incoming").glob("*.part")) == []


def test_a_short_stream_fails_as_size_mismatch(tmp_path):
    fetcher = make_fetcher(tmp_path, ChunkTransport(data=WEIGHTS[:11]))
    record = fetcher.fetch_model(request_for())
    assert record["status"] == "failed"
    assert record["reason"] == "size_mismatch"
    assert record["error_code"] == "model_hash_mismatch"
    assert record["bytes_received"] == 11


def test_an_overlong_stream_is_cut_at_the_crossing_chunk(tmp_path):
    transport = ChunkTransport(data=WEIGHTS + b"extra-extra-extra", chunk_size=8)
    fetcher = make_fetcher(tmp_path, transport)
    record = fetcher.fetch_model(request_for())
    assert record["status"] == "failed"
    assert record["reason"] == "size_mismatch"
    assert record["bytes_received"] > len(WEIGHTS)
    # The stream was abandoned at the crossing, not drained to the end.
    total_chunks = -(-len(transport.data) // transport.chunk_size)
    assert transport.chunks_yielded < total_chunks


# -- source failures are named ---------------------------------------------------------


def test_an_unreachable_source_fails_named(tmp_path):
    def down(request):
        raise FetchSourceError("peer-node-9 is not answering")

    fetcher = make_fetcher(tmp_path, down)
    record = fetcher.fetch_model(request_for())
    assert record["status"] == "failed"
    assert record["reason"] == "source_unreachable"
    assert record["error_code"] == "engine_unreachable"
    assert record["bytes_received"] == 0
    assert record["actual_sha256"] is None


def test_a_source_dying_mid_stream_fails_with_what_arrived(tmp_path):
    def flaky(request):
        yield WEIGHTS[:9]
        raise FetchSourceError("connection dropped")

    store = make_store(tmp_path)
    fetcher = ModelFetcher(
        NODE_ID, store, transport=flaky, simulated=True, now=lambda: FIXED
    )
    record = fetcher.fetch_model(request_for())
    assert record["status"] == "failed"
    assert record["error_code"] == "engine_unreachable"
    assert record["bytes_received"] == 9
    assert store.entries() == []
    assert list((store.root / "incoming").glob("*.part")) == []


def test_transport_accidents_and_non_bytes_are_engine_error(tmp_path):
    def boom(request):
        raise RuntimeError("the fake exploded")

    record = make_fetcher(tmp_path / "a", boom).fetch_model(request_for())
    assert record["status"] == "failed"
    assert record["reason"] == "transport_error"
    assert record["error_code"] == "engine_error"

    def words(request):
        yield "not bytes at all"

    record = make_fetcher(tmp_path / "b", words).fetch_model(request_for())
    assert record["status"] == "failed"
    assert record["reason"] == "transport_sent_non_bytes"
    assert record["error_code"] == "engine_error"


def test_no_transport_wired_fails_honestly(tmp_path):
    fetcher = make_fetcher(tmp_path, None)
    record = fetcher.fetch_model(request_for())
    assert record["status"] == "failed"
    assert record["reason"] == "no_transport_wired"
    assert record["error_code"] == "engine_unreachable"


# -- the ledger --------------------------------------------------------------------------


def test_the_report_counts_every_attempt_and_sums_only_fetched_bytes(tmp_path):
    transport = ChunkTransport()
    fetcher = make_fetcher(tmp_path, transport)
    fetcher.fetch_model(request_for())  # fetched
    fetcher.fetch_model(request_for())  # already present now
    fetcher.fetch_model(request_for(name="other-1b", source_id="peer-node-9",
                                    source_kind="named_source"))  # refused: unnamed
    report = fetcher.report()
    assert report["counts"] == {
        "fetched": 1,
        "already_present": 1,
        "refused": 1,
        "failed": 0,
    }
    assert report["bytes_fetched"] == len(WEIGHTS)
    assert report["simulated"] is True
    assert [r["status"] for r in report["records"]] == [
        "fetched",
        "already_present",
        "refused",
    ]
    assert [e["type"] for e in fetcher.events] == [
        "model.fetched",
        "model.fetch_already_present",
        "model.fetch_refused",
    ]


# -- the node agent ------------------------------------------------------------------------


def static_probes() -> ProbeSet:
    return ProbeSet(
        cpu=lambda: {"arch": "x86_64", "cores": 4, "model": "Fixture CPU"},
        ram_gb=lambda: 16.0,
        accelerators=lambda: [{"kind": "none"}],
        disk_free_gb=lambda: 100.0,
        thermal_c=lambda: None,
        vram_gb=lambda: None,
    )


def make_agent(tmp_path, store, fetcher=None) -> NodeAgent:
    keys = tmp_path / "keys"
    key_store = LocalKeyStore(keys)
    root_keypair, root_cert = create_mesh_root(now=FIXED)
    node_keypair = NodeKeypair.generate()
    cert = issue_node_certificate(
        root_keypair,
        root_cert,
        node_id=NODE_ID,
        node_keypair=node_keypair,
        device_class="vm",
        agent_version="0.0.1",
        now=FIXED,
    )
    key_store.save_certificate(root_cert)
    key_store.save_seed(NODE_ID, node_keypair.seed)
    key_store.save_certificate(cert)
    config = NodeConfig.from_dict(
        {
            "version": 1,
            "node_id": NODE_ID,
            "device_class": "vm",
            "key_store_dir": str(keys),
        },
        base_dir=tmp_path,
    )
    return NodeAgent(
        config,
        now=lambda: FIXED,
        census_probes=static_probes(),
        model_store=store,
        model_fetcher=fetcher,
    )


def test_the_agent_fetches_only_while_up_and_reports_it(tmp_path):
    store = make_store(tmp_path)
    fetcher = ModelFetcher(
        NODE_ID,
        store,
        transport=ChunkTransport(),
        simulated=True,
        now=lambda: FIXED,
    )
    agent = make_agent(tmp_path, store, fetcher)
    with pytest.raises(NodeAgentError, match="fetch_model"):
        agent.fetch_model(request_for())  # still created — not started
    agent.start()
    agent.join()
    record = agent.fetch_model(request_for())
    assert record["status"] == "fetched"
    # The fetched model is present, so the agent's census lists it.
    assert agent.census()["models_present"] == ["fixture-7b"]
    report = agent.status()["model_fetch"]
    assert report["counts"]["fetched"] == 1
    assert agent.fetch_report() == report
    agent.drain()
    with pytest.raises(NodeAgentError, match="fetch_model"):
        agent.fetch_model(request_for(name="other-1b"))  # draining stocks nothing


def test_an_unwired_agent_fetches_nothing_and_says_so(tmp_path):
    agent = make_agent(tmp_path, make_store(tmp_path))
    assert agent.model_fetcher is None
    assert agent.fetch_report() is None
    assert agent.status()["model_fetch"] is None
    agent.start()
    with pytest.raises(NodeAgentError, match="no model fetcher"):
        agent.fetch_model(request_for())


def test_the_phone_citizens_fetch_policy_carries_its_ceiling(tmp_path):
    keys = tmp_path / "keys"
    key_store = LocalKeyStore(keys)
    root_keypair, root_cert = create_mesh_root(now=FIXED)
    node_keypair = NodeKeypair.generate()
    cert = issue_node_certificate(
        root_keypair,
        root_cert,
        node_id="phone-sim-1",
        node_keypair=node_keypair,
        device_class="phone",
        agent_version="0.0.1",
        now=FIXED,
    )
    key_store.save_certificate(root_cert)
    key_store.save_seed("phone-sim-1", node_keypair.seed)
    key_store.save_certificate(cert)
    config = NodeConfig.from_dict(
        {
            "version": 1,
            "node_id": "phone-sim-1",
            "device_class": "phone",
            "key_store_dir": str(keys),
        },
        base_dir=tmp_path,
    )
    probes = ProbeSet(
        cpu=lambda: {
            "arch": "aarch64",
            "cores": 8,
            "model": "Representative phone-class ARM SoC (SIMULATED shape)",
        },
        ram_gb=lambda: 6.0,
        accelerators=lambda: [{"kind": "none"}],
        disk_free_gb=lambda: 32.0,
        thermal_c=lambda: 36.0,
        vram_gb=lambda: None,
    )
    agent, report = build_phone_citizen_agent(
        config,
        store_root=tmp_path / "store",
        probes=probes,
        readings=lambda: {
            "thermal_c": 36.0,
            "memory_used_bytes": 2 * 1024**3,
            "memory_total_bytes": 6 * 1024**3,
            "cpu_share_used": 0.25,
        },
        neighbours={"phone_os": "active", "cellular_radio": "present"},
        battery={"level_percent": 80, "charging": False},
        simulated=True,
        now=lambda: FIXED,
    )
    assert report["ready"] is True
    fetcher = agent.model_fetcher
    assert fetcher is not None
    # Small models only, at the fetch door too: the profile's
    # 1.5 GiB ceiling is the fetch policy's cap.
    assert fetcher.policy["max_model_bytes"] == int(1.5 * 1024**3)
    agent.start()
    agent.join()
    big = request_for()
    big["name"] = "too-big-7b"
    big["expected_size_bytes"] = int(1.5 * 1024**3) + 1
    record = agent.fetch_model(big)
    assert record["status"] == "refused"
    assert record["reason"] == "model_too_large_for_policy"


def test_readme_untouched():
    import hashlib as _hashlib

    digest = _hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
