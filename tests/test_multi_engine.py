"""Multi-engine node tests — Y-045's acceptance.

The host must prove, driving the real Aesir (Y-024) and Ollama
(Y-023) adapters over fake transports speaking their own wire
shapes: (a) side-by-side discovery — both engines' validated
records reach the census in hosting order, each with its own
provenance grade, and the census model union lists a shared
name once while the report keeps per-engine listings separate;
(b) slots accounted across engines — assignments load through
the one shared lifecycle, a model resident for one engine
cannot be assigned to another, an engine is never assigned a
model its own surface does not list, pinned residents block a
further load across engines, and an eviction under the shared
LRU law drops the evicted model's assignment so per-engine
slot shares always sum to the node's used slots; (c) routing
by the model's own facts — explicit engine, assignment,
single listing, ambiguity refused naming every candidate, no
listing refused model_not_present; (d) runs counted against
the engine that ran them, by the result's own status,
including an adapter's own refusal; (e) an unreachable engine
lists nothing and routes nothing, its discovery saying why;
(f) the node agent seam — assignments delegate when wired,
status carries the report, an unwired agent accounts nothing;
and (g) the founding README is untouched.

No live Aesir or Ollama server is spoken to: the transports
are the adapters' seams filled with test doubles, labelled
SIMULATED wherever a report could be mistaken for hardware.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.aesir_adapter import AesirAdapter, AesirHttpError, AesirUnreachable
from yggdrasil.identity import (
    LocalKeyStore,
    NodeKeypair,
    create_mesh_root,
    issue_node_certificate,
)
from yggdrasil.model_lifecycle import ModelLifecycle
from yggdrasil.model_store import ModelStore
from yggdrasil.multi_engine import MultiEngineError, MultiEngineHost
from yggdrasil.node_agent import NodeAgent, NodeAgentError, NodeConfig
from yggdrasil.ollama_adapter import OllamaAdapter, OllamaHttpError, OllamaUnreachable

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
NODE_ID = "multi-node-1"
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
TASK_ID = "3f6f1b2c-9d4e-4c5a-8b7d-000000000045"
RESIDENT = "gemma4-e2b:latest"  # listed by BOTH engines (the shared name)
AESIR_ONLY = "qwen3-0.6b:latest"
OLLAMA_ONLY = "fixture-llm:latest"
OLLAMA_VISION = "fixture-vision:latest"

LEDGER = {"AES-SRV-010": "verified"}


class FakeTimer:
    def __init__(self):
        self.now = 100.0

    def __call__(self) -> float:
        value = self.now
        self.now += 0.25
        return value


class FakeAesirTransport:
    """Aesir's native wire shapes, canned (as in Y-024's suite)."""

    def __init__(self, *, unreachable: bool = False):
        self.unreachable = unreachable

    def _check(self):
        if self.unreachable:
            raise AesirUnreachable("connection refused (fake transport)")

    def get_json(self, path: str) -> dict:
        self._check()
        if path == "/health/live":
            return {"schema_version": 1, "status": "ok", "scope": "process"}
        if path == "/health/ready":
            return {
                "schema_version": 1,
                "status": "ready",
                "ready": True,
                "model": RESIDENT,
                "digest": "sha256:fixturedigest",
                "backend": "cuda",
                "profile": "gemma4-e2b",
                "context": 4096,
            }
        if path == "/v1/models":
            return {
                "object": "list",
                "data": [
                    {"id": RESIDENT, "object": "model", "created": 1, "owned_by": "aesir"},
                    {"id": AESIR_ONLY, "object": "model", "created": 1, "owned_by": "aesir"},
                ],
            }
        raise AesirHttpError(404, f"unknown route {path} (fake)")

    def post_json(self, path: str, body: dict) -> dict:
        self._check()
        if path != "/v1/generate":
            raise AesirHttpError(404, f"unknown route {path} (fake)")
        return {
            "text": "the whole answer",
            "finish_reason": "stop",
            "prompt_tokens": 4,
            "generated_tokens": 3,
            "context_used": 7,
            "backend": "cuda",
            "cpu_offload": 0,
            "model": RESIDENT,
        }

    def post_lines(self, path: str, body: dict):
        self._check()
        if path != "/v1/generate":
            raise AesirHttpError(404, f"unknown route {path} (fake)")
        yield json.dumps({"text": "Hello", "done": False})
        yield json.dumps({"text": ", mesh", "done": False})
        yield json.dumps(
            {
                "text": "",
                "done": True,
                "finish_reason": "stop",
                "prompt_tokens": 4,
                "generated_tokens": 3,
                "context_used": 7,
                "backend": "cuda",
                "cpu_offload": 0,
                "model": RESIDENT,
                "model_digest": "sha256:fixturedigest",
            }
        )


OLLAMA_TAGS = {
    "models": [
        {
            "name": OLLAMA_ONLY,
            "size": 4_700_000_000,
            "details": {
                "family": "llama",
                "parameter_size": "7B",
                "quantization_level": "Q4_K_M",
                "format": "gguf",
            },
        },
        {
            "name": OLLAMA_VISION,
            "size": 4_900_000_000,
            "details": {
                "family": "llava",
                "parameter_size": "7B",
                "quantization_level": "Q4_K_M",
                "format": "gguf",
            },
        },
        {
            "name": RESIDENT,
            "size": 2_000_000_000,
            "details": {
                "family": "gemma",
                "parameter_size": "2B",
                "quantization_level": "Q4_K_M",
                "format": "gguf",
            },
        },
    ]
}
OLLAMA_SHOW = {
    OLLAMA_ONLY: {"capabilities": ["completion"]},
    OLLAMA_VISION: {"capabilities": ["completion", "vision"]},
    RESIDENT: {"capabilities": ["completion"]},
}


class FakeOllamaTransport:
    """Ollama's wire shapes, canned (as in Y-023's suite)."""

    def __init__(self, *, unreachable: bool = False):
        self.unreachable = unreachable

    def _check(self):
        if self.unreachable:
            raise OllamaUnreachable("connection refused (fake transport)")

    def get_json(self, path: str) -> dict:
        self._check()
        if path == "/api/version":
            return {"version": "0.5.1"}
        if path == "/api/tags":
            return OLLAMA_TAGS
        raise OllamaHttpError(404, f"unknown route {path} (fake)")

    def post_json(self, path: str, body: dict) -> dict:
        self._check()
        if path == "/api/show":
            return OLLAMA_SHOW.get(body["name"], {})
        if path == "/api/generate":
            if body["model"] not in OLLAMA_SHOW:
                raise OllamaHttpError(404, "model not found (fake)")
            return {
                "model": body["model"],
                "response": "the whole answer",
                "done": True,
                "done_reason": "stop",
                "eval_count": 5,
            }
        raise OllamaHttpError(404, f"unknown route {path} (fake)")

    def post_lines(self, path: str, body: dict):
        self._check()
        if path != "/api/generate":
            raise OllamaHttpError(404, f"unknown route {path} (fake)")
        if body["model"] not in OLLAMA_SHOW:
            raise OllamaHttpError(404, "model not found (fake)")
        yield json.dumps({"model": body["model"], "response": "Hello", "done": False})
        yield json.dumps({"model": body["model"], "response": ", mesh", "done": False})
        yield json.dumps(
            {
                "model": body["model"],
                "response": "",
                "done": True,
                "done_reason": "stop",
                "eval_count": 3,
            }
        )


def aesir_adapter(**kwargs) -> AesirAdapter:
    return AesirAdapter(FakeAesirTransport(**kwargs), ledger=LEDGER, timer=FakeTimer())


def ollama_adapter(**kwargs) -> OllamaAdapter:
    return OllamaAdapter(FakeOllamaTransport(**kwargs), timer=FakeTimer())


STORE_MODELS = (RESIDENT, AESIR_ONLY, OLLAMA_ONLY, OLLAMA_VISION)


def make_host(tmp_path, *, max_slots=4, aesir=None, ollama=None):
    store = ModelStore(tmp_path / "store", now=lambda: FIXED)
    for index, name in enumerate(STORE_MODELS):
        source = tmp_path / f"source-{index}.gguf"
        source.write_bytes(bytes([7]) * 100)
        store.add(source, name=name, family="fixture", quant="Q4_K_M", format="gguf")
    lifecycle = ModelLifecycle(
        NODE_ID,
        store,
        vram_bytes=0,
        ram_bytes=10_000,
        max_slots=max_slots,
        now=lambda: FIXED,
    )
    host = MultiEngineHost(
        NODE_ID,
        [aesir or aesir_adapter(), ollama or ollama_adapter()],
        lifecycle,
        now=lambda: FIXED,
    )
    return host, lifecycle


def task_for(model: str, *, kind: str = "chat.generate") -> dict:
    return {
        "id": TASK_ID,
        "kind": kind,
        "payload": {"prompt": "say hello"},
        "caller": "y-045-tests",
        "requirements": {"model": model, "capabilities": ["generate"]},
    }


# -- construction ---------------------------------------------------------------


def test_construction_refuses_duplicate_engines_and_an_empty_host(tmp_path):
    _, lifecycle = make_host(tmp_path)
    with pytest.raises(MultiEngineError, match="hosted twice"):
        MultiEngineHost(NODE_ID, [ollama_adapter(), ollama_adapter()], lifecycle)
    with pytest.raises(MultiEngineError, match="at least one engine"):
        MultiEngineHost(NODE_ID, [], lifecycle)
    with pytest.raises(MultiEngineError, match="belongs to node"):
        MultiEngineHost("other-node-1", [ollama_adapter()], lifecycle)


# -- side-by-side discovery -------------------------------------------------------


def test_census_engines_carry_both_discoveries_with_their_own_grades(tmp_path):
    host, _ = make_host(tmp_path)
    engines = host.census_engines()
    assert [e["engine"] for e in engines] == ["aesir", "ollama"]
    assert engines[0]["capabilities"]["provenance_grade"] == "proven"
    assert engines[1]["capabilities"]["provenance_grade"] == "unproven"
    assert all(e["reachable"] for e in engines)


def test_census_models_union_once_report_listings_per_engine(tmp_path):
    host, _ = make_host(tmp_path)
    union = host.census_models()
    assert sorted(union) == sorted([RESIDENT, AESIR_ONLY, OLLAMA_ONLY, OLLAMA_VISION])
    report = host.report()
    per_engine = {e["engine"]: e["models_listed"] for e in report["engines"]}
    assert per_engine["aesir"] == [RESIDENT, AESIR_ONLY]
    assert per_engine["ollama"] == [OLLAMA_ONLY, OLLAMA_VISION, RESIDENT]
    # The shared name is two listings in the report, one name in the census.
    assert report["totals"]["listings"] == 5
    assert report["totals"]["engines_reachable"] == 2


# -- slots accounted across engines -------------------------------------------------


def test_assign_loads_through_the_shared_lifecycle_and_names_the_engine(tmp_path):
    host, lifecycle = make_host(tmp_path)
    record = host.assign(RESIDENT, "aesir")
    assert record["engine"] == "aesir"
    assert record["model"] == RESIDENT
    assert record["placement"] == "system"  # no VRAM pool in this fixture
    assert lifecycle.loaded_models() == [RESIDENT]
    # Idempotent for its own engine…
    assert host.assign(RESIDENT, "aesir") == record
    # …and refused for the other, though Ollama lists it too.
    with pytest.raises(MultiEngineError, match="one residency, one engine"):
        host.assign(RESIDENT, "ollama")


def test_assign_refuses_what_the_engine_does_not_list_and_spends_no_slot(tmp_path):
    host, lifecycle = make_host(tmp_path)
    with pytest.raises(MultiEngineError) as caught:
        host.assign(OLLAMA_ONLY, "aesir")
    assert caught.value.error_code == "model_not_present"
    with pytest.raises(MultiEngineError, match="not hosted"):
        host.assign(OLLAMA_ONLY, "llamacpp")
    assert lifecycle.loaded_models() == []
    assert host.report()["slots"] == {"max_slots": 4, "used": 0, "free": 4}


def test_slots_are_one_budget_across_engines_and_eviction_drops_assignment(tmp_path):
    host, lifecycle = make_host(tmp_path, max_slots=2)
    host.assign(RESIDENT, "aesir")
    host.assign(OLLAMA_ONLY, "ollama")
    report = host.report()
    assert report["slots"] == {"max_slots": 2, "used": 2, "free": 0}
    shares = {e["engine"]: e["slots_used"] for e in report["engines"]}
    assert shares == {"aesir": 1, "ollama": 1}
    # A third load spends the SAME budget: the shared LRU law
    # evicts the eldest residency (Aesir's), whichever engine asks.
    host.assign(OLLAMA_VISION, "ollama")
    assert sorted(lifecycle.loaded_models()) == sorted([OLLAMA_ONLY, OLLAMA_VISION])
    assert host.assignment(RESIDENT) is None  # evicted: no slot, no assignment
    report = host.report()
    shares = {e["engine"]: e["slots_used"] for e in report["engines"]}
    assert sum(shares.values()) == report["slots"]["used"] == 2
    assert shares == {"aesir": 0, "ollama": 2}


def test_pinned_residents_block_a_cross_engine_load_by_name(tmp_path):
    host, _ = make_host(tmp_path, max_slots=2)
    host.assign(RESIDENT, "aesir", pinned=True)
    host.assign(OLLAMA_ONLY, "ollama", pinned=True)
    with pytest.raises(MultiEngineError, match="does not fit"):
        host.assign(OLLAMA_VISION, "ollama")
    assert host.report()["totals"]["assignments"] == 2


def test_unassign_unloads_and_frees_the_shared_slot(tmp_path):
    host, lifecycle = make_host(tmp_path)
    host.assign(OLLAMA_ONLY, "ollama")
    record = host.unassign(OLLAMA_ONLY)
    assert record["engine"] == "ollama"
    assert lifecycle.loaded_models() == []
    assert host.assignment(OLLAMA_ONLY) is None
    with pytest.raises(MultiEngineError, match="no engine assignment"):
        host.unassign(OLLAMA_ONLY)


# -- routing -------------------------------------------------------------------------


def test_route_by_single_listing_and_run_counts(tmp_path):
    host, _ = make_host(tmp_path)
    assert host.route(task_for(OLLAMA_ONLY)) == "ollama"
    result, _events = host.run_task(task_for(OLLAMA_ONLY))
    assert result["status"] == "completed"
    assert result["provenance"]["engine"] == "ollama"
    report = host.report()
    counts = {e["engine"]: e["tasks_run"] for e in report["engines"]}
    assert counts == {"aesir": 0, "ollama": 1}
    assert report["totals"]["tasks_run"] == 1


def test_route_by_assignment_beats_ambiguity(tmp_path):
    host, _ = make_host(tmp_path)
    host.assign(RESIDENT, "aesir")
    result, _events = host.run_task(task_for(RESIDENT))
    assert result["status"] == "completed"
    assert result["provenance"]["engine"] == "aesir"


def test_route_ambiguous_shared_model_is_refused_naming_both(tmp_path):
    host, _ = make_host(tmp_path)
    with pytest.raises(MultiEngineError) as caught:
        host.run_task(task_for(RESIDENT))
    message = str(caught.value)
    assert "aesir" in message and "ollama" in message
    assert "Part IV" in message  # the refusal says whose judgement it is
    assert [e["type"] for e in host.events] == ["multi_engine.route_refused"]


def test_route_explicit_engine_must_list_the_model(tmp_path):
    host, _ = make_host(tmp_path)
    assert host.route(task_for(OLLAMA_ONLY), engine="ollama") == "ollama"
    with pytest.raises(MultiEngineError) as caught:
        host.route(task_for(OLLAMA_ONLY), engine="aesir")
    assert caught.value.error_code == "model_not_present"
    with pytest.raises(MultiEngineError) as caught:
        host.route(task_for(OLLAMA_ONLY), engine="llamacpp")
    assert caught.value.error_code == "no_capable_node"


def test_route_no_listing_and_no_model_are_refused_by_name(tmp_path):
    host, _ = make_host(tmp_path)
    with pytest.raises(MultiEngineError) as caught:
        host.run_task(task_for("ghost:latest"))
    assert caught.value.error_code == "model_not_present"
    bare = {"id": TASK_ID, "kind": "chat.generate", "payload": {}, "caller": "t"}
    with pytest.raises(MultiEngineError, match="names no model"):
        host.route(bare)
    assert host.route(bare, engine="ollama") == "ollama"


def test_an_adapter_refusal_is_counted_against_its_engine(tmp_path):
    host, _ = make_host(tmp_path)
    embed_task = task_for(RESIDENT, kind="embed.text")
    embed_task["requirements"] = {"model": RESIDENT, "capabilities": ["embed"]}
    result, _events = host.run_task(embed_task, engine="aesir")
    assert result["status"] == "refused"  # Aesir's own refusal, kept as its own
    assert result["error"]["code"] == "no_capable_node"
    counts = {e["engine"]: e for e in host.report()["engines"]}
    assert counts["aesir"]["tasks_run"] == 1
    assert counts["aesir"]["tasks_refused"] == 1
    assert counts["ollama"]["tasks_run"] == 0


# -- unreachable engines -----------------------------------------------------------------


def test_an_unreachable_engine_lists_and_routes_nothing(tmp_path):
    host, _ = make_host(tmp_path, ollama=ollama_adapter(unreachable=True))
    report = host.report()
    per_engine = {e["engine"]: e for e in report["engines"]}
    assert per_engine["ollama"]["discovery"]["reachable"] is False
    assert per_engine["ollama"]["models_listed"] == []
    assert report["totals"]["engines_reachable"] == 1
    assert RESIDENT in host.census_models()  # Aesir still lists the shared name
    assert OLLAMA_ONLY not in host.census_models()
    with pytest.raises(MultiEngineError) as caught:
        host.run_task(task_for(OLLAMA_ONLY))
    assert caught.value.error_code == "model_not_present"


# -- the node agent seam ---------------------------------------------------------------------


def _provisioned_agent(tmp_path: Path, **agent_kwargs) -> NodeAgent:
    store_dir = tmp_path / "keys"
    store = LocalKeyStore(store_dir)
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
    store.save_certificate(root_cert)
    store.save_seed(NODE_ID, node_keypair.seed)
    store.save_certificate(cert)
    config = NodeConfig.from_dict(
        {
            "version": 1,
            "node_id": NODE_ID,
            "device_class": "vm",
            "key_store_dir": str(store_dir),
        }
    )
    return NodeAgent(config, now=lambda: FIXED, **agent_kwargs)


def test_the_agent_assigns_and_reports_when_a_host_is_wired(tmp_path):
    host, lifecycle = make_host(tmp_path)
    agent = _provisioned_agent(
        tmp_path, model_lifecycle=lifecycle, multi_engine=host
    )
    agent.start()
    agent.join()
    record = agent.assign_model_to_engine(OLLAMA_ONLY, "ollama")
    assert record["engine"] == "ollama"
    status = agent.status()
    assert status["multi_engine"]["totals"]["assignments"] == 1
    assert status["models_loaded"] == [OLLAMA_ONLY]
    agent.unassign_model(OLLAMA_ONLY)
    assert agent.status()["multi_engine"]["totals"]["assignments"] == 0


def test_an_unwired_agent_accounts_nothing_across_engines(tmp_path):
    agent = _provisioned_agent(tmp_path)
    assert agent.multi_engine is None
    assert agent.multi_engine_report() is None
    assert agent.status()["multi_engine"] is None
    with pytest.raises(NodeAgentError, match="no multi-engine host"):
        agent.assign_model_to_engine(OLLAMA_ONLY, "ollama")


# -- the founding document ---------------------------------------------------------------------


def test_readme_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
