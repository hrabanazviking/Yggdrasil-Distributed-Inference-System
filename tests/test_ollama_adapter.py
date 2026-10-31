"""Engine adapter I (Ollama) tests — Y-023's acceptance.

The adapter must prove, against a faithful fake transport that
speaks Ollama's own wire shapes: (a) discovery — a reachable
server maps into a validated EngineCapability with honest
capabilities (generate/stream from a non-empty shelf,
vision/embed ONLY where a model's own /api/show claims them,
provenance_grade unproven whatever is found), an unreachable
server is a returned reachable: False finding, and an empty
shelf claims no generate; (b) the model list and registry
entries are built only from fields Ollama publishes — a tag
with no family is skipped by name, never dressed up; (c)
generate streams: NDJSON chunks become token StreamEvents in
order, the terminal ResultEnvelope assembles the full text
with provenance (node, engine, model, quant) and measured
timings, and a non-streamed answer is never re-chunked into a
fake stream; (d) failures are named in the Y-007 taxonomy —
404 is model_not_present, a dead server engine_unreachable, a
malformed NDJSON line engine_error, and a stream that dies
mid-answer reports the partial text the caller already holds;
(e) the node agent's census carries the adapter's discovery
and model names live, through Y-022's collector seams; and
(f) the founding README is untouched.

No live Ollama server is spoken to in this suite: the fake
transport is the seam's test double, and the citizen slices
(Y-033 – Y-035) own the on-hardware proof.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.census import ProbeSet
from yggdrasil.identity import LocalKeyStore, NodeKeypair, create_mesh_root, issue_node_certificate
from yggdrasil.node_agent import NodeAgent, NodeConfig
from yggdrasil.ollama_adapter import (
    OllamaAdapter,
    OllamaAdapterError,
    OllamaHttpError,
    OllamaUnreachable,
)

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
NODE_ID = "ollama-node-1"
TASK_ID = "3f6f1b2c-9d4e-4c5a-8b7d-000000000023"

TAGS = {
    "models": [
        {
            "name": "fixture-llm:latest",
            "size": 4_700_000_000,
            "details": {
                "family": "llama",
                "parameter_size": "7B",
                "quantization_level": "Q4_K_M",
                "format": "gguf",
            },
        },
        {
            "name": "fixture-vision:latest",
            "size": 4_900_000_000,
            "details": {
                "family": "llava",
                "parameter_size": "7B",
                "quantization_level": "Q4_K_M",
                "format": "gguf",
            },
        },
        {
            "name": "fixture-embed:latest",
            "size": 274_000_000,
            "details": {
                "family": "nomic-bert",
                "parameter_size": "137M",
                "quantization_level": "F16",
                "format": "gguf",
            },
        },
    ]
}
SHOW = {
    "fixture-llm:latest": {"capabilities": ["completion"]},
    "fixture-vision:latest": {"capabilities": ["completion", "vision"]},
    "fixture-embed:latest": {"capabilities": ["embedding"]},
}


class FakeTimer:
    """Each call advances a quarter second — every measured
    interval in a single call pair is exactly 250 ms."""

    def __init__(self):
        self.now = 100.0

    def __call__(self) -> float:
        value = self.now
        self.now += 0.25
        return value


class FakeOllamaTransport:
    """Ollama's wire shapes, canned: the adapter's transport
    seam filled with the server's own answers."""

    def __init__(
        self,
        *,
        unreachable: bool = False,
        version_status: int | None = None,
        show_fails: bool = False,
        tags: dict | None = None,
        stream_mode: str = "ok",
    ):
        self.unreachable = unreachable
        self.version_status = version_status
        self.show_fails = show_fails
        self.tags = TAGS if tags is None else tags
        self.stream_mode = stream_mode
        self.requests: list[tuple[str, str, dict | None]] = []

    def _check(self):
        if self.unreachable:
            raise OllamaUnreachable("connection refused (fake transport)")

    def get_json(self, path: str) -> dict:
        self._check()
        self.requests.append(("GET", path, None))
        if path == "/api/version":
            if self.version_status is not None:
                raise OllamaHttpError(self.version_status, "no version route (fake)")
            return {"version": "0.5.1"}
        if path == "/api/tags":
            return self.tags
        raise OllamaHttpError(404, f"unknown route {path} (fake)")

    def post_json(self, path: str, body: dict) -> dict:
        self._check()
        self.requests.append(("POST", path, body))
        if path == "/api/show":
            if self.show_fails:
                raise OllamaHttpError(500, "show broken (fake)")
            return SHOW.get(body["name"], {})
        if path == "/api/generate":
            if body["model"] not in SHOW:
                raise OllamaHttpError(404, "model not found (fake)")
            return {
                "model": body["model"],
                "response": "the whole answer",
                "done": True,
                "done_reason": "stop",
                "eval_count": 5,
            }
        if path == "/api/embed":
            if body["model"] not in SHOW:
                raise OllamaHttpError(404, "model not found (fake)")
            return {"embeddings": [[0.1, 0.2, 0.3] for _ in body["input"]]}
        raise OllamaHttpError(404, f"unknown route {path} (fake)")

    def post_lines(self, path: str, body: dict):
        self._check()
        self.requests.append(("POST", path, body))
        if path != "/api/generate":
            raise OllamaHttpError(404, f"unknown route {path} (fake)")
        if body["model"] not in SHOW:
            raise OllamaHttpError(404, "model not found (fake)")
        if self.stream_mode == "malformed":
            yield '{"response": "Hel'
            return
        yield json.dumps({"model": body["model"], "response": "Hello", "done": False})
        yield json.dumps({"model": body["model"], "response": ", mesh", "done": False})
        if self.stream_mode == "dies_mid_stream":
            raise OllamaUnreachable("server went away mid-stream (fake)")
        yield json.dumps(
            {
                "model": body["model"],
                "response": "",
                "done": True,
                "done_reason": "stop",
                "eval_count": 3,
            }
        )


def adapter(**transport_kwargs) -> OllamaAdapter:
    return OllamaAdapter(FakeOllamaTransport(**transport_kwargs), timer=FakeTimer())


def generate_task(**overrides) -> dict:
    task = {
        "id": TASK_ID,
        "kind": "chat.generate",
        "payload": {"prompt": "say hello"},
        "caller": "y-023-tests",
        "requirements": {"model": "fixture-llm:latest", "capabilities": ["generate"]},
    }
    task.update(overrides)
    return task


# -- discovery ---------------------------------------------------------------


def test_discovery_maps_the_engines_own_surface_honestly():
    record = adapter().discover()
    assert record == {
        "engine": "ollama",
        "reachable": True,
        "version": "0.5.1",
        "capabilities": {
            "generate": True,
            "stream": True,
            "vision": True,  # the vision model's own show surface claims it
            "embed": True,  # the embed model's own show surface claims it
            "provenance_grade": "unproven",  # Ollama has no ledger to mirror
        },
    }


def test_an_unreachable_server_is_a_finding_not_an_exception():
    record = adapter(unreachable=True).discover()
    assert record["reachable"] is False
    assert "version" not in record
    assert record["capabilities"] == {
        "generate": False,
        "embed": False,
        "vision": False,
        "stream": False,
        "provenance_grade": "unproven",
    }


def test_an_empty_shelf_claims_no_generate():
    record = adapter(tags={"models": []}).discover()
    assert record["reachable"] is True
    assert record["capabilities"]["generate"] is False
    assert record["capabilities"]["stream"] is False


def test_a_server_that_cannot_name_its_version_is_still_discovered():
    record = adapter(version_status=404).discover()
    assert record["reachable"] is True
    assert "version" not in record


def test_vision_is_never_claimed_from_a_name_or_a_failed_show():
    # The vision model is named like one and present, but its
    # show surface fails: no capability rises on no evidence.
    record = adapter(show_fails=True).discover()
    assert record["capabilities"]["vision"] is False
    assert record["capabilities"]["embed"] is False
    assert record["capabilities"]["generate"] is True  # the shelf is still non-empty


# -- models ---------------------------------------------------------------------


def test_the_model_list_is_the_servers_own_names():
    assert adapter().list_models() == [
        "fixture-llm:latest",
        "fixture-vision:latest",
        "fixture-embed:latest",
    ]


def test_model_entries_are_built_only_from_published_fields():
    entries = adapter().model_entries()
    by_name = {entry["name"]: entry for entry in entries}
    llm = by_name["fixture-llm:latest"]
    assert llm["family"] == "llama"
    assert llm["parameter_count"] == "7B"
    assert llm["quants"] == [
        {"name": "Q4_K_M", "format": "gguf", "size_gb": pytest.approx(4.3772, abs=1e-3)}
    ]
    assert llm["capabilities"] == ["generate", "stream"]
    assert by_name["fixture-vision:latest"]["capabilities"] == ["generate", "stream", "vision"]
    # An embedding model is never dressed as a generator.
    assert by_name["fixture-embed:latest"]["capabilities"] == ["embed"]
    assert all(entry["runnable_on"] == [] for entry in entries)  # runnable is Y-038's word


def test_a_tag_with_no_family_is_skipped_by_name_never_dressed_up():
    tags = {"models": TAGS["models"] + [{"name": "mystery:latest", "size": 10, "details": {}}]}
    instance = adapter(tags=tags)
    names = [entry["name"] for entry in instance.model_entries()]
    assert "mystery:latest" not in names
    assert instance.skipped_models == ["mystery:latest"]
    # ...but the model list still reports it present: presence
    # and registry-entry-worthiness are different claims.
    assert "mystery:latest" in instance.list_models()


# -- generate ---------------------------------------------------------------------


def test_generate_streams_tokens_and_lands_a_provenanced_result():
    result, events = adapter().run_task(generate_task(), NODE_ID)
    assert [event["event"] for event in events] == ["started", "token", "token", "completed"]
    assert [event["data"]["text"] for event in events if event["event"] == "token"] == [
        "Hello",
        ", mesh",
    ]
    assert [event["seq"] for event in events] == [0, 1, 2, 3]
    assert result["status"] == "completed"
    assert result["result"] == {
        "text": "Hello, mesh",
        "engine": "ollama",
        "done_reason": "stop",
        "eval_count": 3,
    }
    assert result["provenance"] == {
        "node_id": NODE_ID,
        "engine": "ollama",
        "model": "fixture-llm:latest",
        "quant": "Q4_K_M",  # from the tag the adapter discovered
    }
    assert result["timings_ms"] == {"queue": 0.0, "run": 250.0, "total": 250.0}


def test_a_non_streamed_answer_is_never_rechunked_into_a_fake_stream():
    pairs = list(adapter().generate(generate_task(), NODE_ID, stream=False))
    events = [event for event, _ in pairs if event is not None]
    results = [result for _, result in pairs if result is not None]
    assert [event["event"] for event in events] == ["completed"]
    assert results[0]["result"]["text"] == "the whole answer"


def test_generate_requires_a_model_and_a_prompt():
    task = generate_task()
    del task["requirements"]
    with pytest.raises(OllamaAdapterError, match="names no model") as caught:
        list(adapter().generate(task, NODE_ID))
    assert caught.value.error_code == "model_not_present"
    with pytest.raises(OllamaAdapterError, match="payload.prompt") as caught:
        list(adapter().generate(generate_task(payload={}), NODE_ID))
    assert caught.value.error_code == "malformed_envelope"


def test_a_missing_model_is_named_model_not_present():
    task = generate_task(requirements={"model": "ghost:latest"})
    result, events = adapter().run_task(task, NODE_ID)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "model_not_present"
    assert events[-1]["event"] == "failed"


def test_a_malformed_stream_line_is_an_engine_error():
    result, _ = adapter(stream_mode="malformed").run_task(generate_task(), NODE_ID)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "engine_error"
    assert "malformed NDJSON" in result["error"]["detail"]


def test_a_stream_that_dies_mid_answer_reports_the_partial_text():
    result, events = adapter(stream_mode="dies_mid_stream").run_task(generate_task(), NODE_ID)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "engine_unreachable"
    assert result["partial"] is True
    # The caller already holds these tokens; the failure
    # envelope says so instead of pretending they never came.
    assert result["result"]["text"] == "Hello, mesh"
    assert events[-1]["event"] == "failed"


def test_an_unreachable_engine_fails_by_its_taxonomy_name():
    result, _ = adapter(unreachable=True).run_task(generate_task(), NODE_ID)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "engine_unreachable"


# -- embed + refusal ----------------------------------------------------------------


def test_embed_runs_one_vector_per_input():
    task = {
        "id": TASK_ID,
        "kind": "embed.text",
        "payload": {"texts": ["one", "two"]},
        "caller": "y-023-tests",
        "requirements": {"model": "fixture-embed:latest", "capabilities": ["embed"]},
    }
    result, events = adapter().run_task(task, NODE_ID)
    assert [event["event"] for event in events] == ["accepted", "completed"]
    assert result["status"] == "completed"
    assert result["result"]["embeddings"] == [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]]
    assert result["result"]["dim"] == 3


def test_a_kind_the_adapter_cannot_serve_is_refused_by_name():
    task = generate_task(kind="vision.describe")
    result, _ = adapter().run_task(task, NODE_ID)
    assert result["status"] == "refused"
    assert result["error"]["code"] == "no_capable_node"
    assert "vision.describe" in result["error"]["detail"]


# -- the agent's census, fed by the adapter ------------------------------------------


def static_probes() -> ProbeSet:
    return ProbeSet(
        cpu=lambda: {"arch": "x86_64", "cores": 8},
        ram_gb=lambda: 16.0,
        accelerators=lambda: [{"kind": "none"}],
    )


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


def test_the_agent_census_carries_the_adapters_live_truth(tmp_path):
    instance = adapter()
    agent = _provisioned_agent(
        tmp_path,
        census_probes=static_probes(),
        census_engines=lambda: [instance.capability_for_census()],
        census_models=instance.model_names_for_census,
    )
    census = agent.census()
    assert census["engines"][0]["engine"] == "ollama"
    assert census["engines"][0]["reachable"] is True
    assert census["models_present"] == [
        "fixture-llm:latest",
        "fixture-vision:latest",
        "fixture-embed:latest",
    ]


def test_the_founding_readme_is_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == "89a37a4e18cab9a998e1751c32e7bfd5"
    assert json.loads((ROOT / "data" / "data_manifest.json").read_text())
