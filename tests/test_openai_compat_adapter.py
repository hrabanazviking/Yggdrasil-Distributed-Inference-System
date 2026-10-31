"""Engine adapter III (llama.cpp + generic OpenAI-compatible)
tests — Y-025's acceptance.

The adapters must prove, against faithful fake transports that
speak the OpenAI-compatible wire shapes: (a) discovery — a
reachable endpoint maps into a validated EngineCapability with
honest capabilities (generate/stream from a non-empty model
list; embed/vision ONLY for operator-declared models actually
listed, never inferred from a name; provenance_grade unproven;
no version, because the shape publishes none), an unreachable
endpoint is a returned reachable: False finding, and an empty
list claims no generate — for BOTH flavours, with the engine
recorded as llamacpp resp. openai-compatible; (b) llama.cpp's
own /health is read as advisory colour and never gates
discovery; (c) the model list is the endpoint's own ids, and
registry entries are built only from published fields — an id
with no family/quant, or one that cannot be a registry name,
is skipped by name, never dressed up, and a declared embedding
model is never dressed as a generator; (d) generate streams:
SSE data lines become token StreamEvents in order, the stream
must close with [DONE] (ending without it fails, partial text
kept), the terminal ResultEnvelope assembles the full text
with provenance (node, engine, model, quant), a non-streamed
answer is never re-chunked, payload.messages travels verbatim,
payload.system becomes the system turn, and sampling options
are forwarded through the whitelist only; (e) vision runs only
for a declared vision model, as image_url content parts, and
is refused by name otherwise; embed runs only for a declared
embedding model, vectors ordered by the answer's own index
fields; (f) failures are named in the Y-007 taxonomy — 404 is
model_not_present, 401 scope_denied, 429 queue_full, a dead
server engine_unreachable, a malformed SSE line engine_error;
(g) the node agent's census carries each flavour's discovery
and model names live, through Y-022's collector seams; and
(h) the founding README is untouched.

No live server is spoken to in this suite: the fake transports
are the seam's test doubles, and the citizen slices
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
from yggdrasil.openai_compat_adapter import (
    LlamaCppAdapter,
    OpenAICompatAdapterError,
    OpenAICompatHttpError,
    OpenAICompatibleAdapter,
    OpenAICompatUnreachable,
)

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
NODE_ID = "compat-node-1"
TASK_ID = "3f6f1b2c-9d4e-4c5a-8b7d-000000000025"

MODELS = {
    "object": "list",
    "data": [
        {
            "id": "fixture-llm",
            "object": "model",
            "owned_by": "fixture",
            "family": "llama",
            "parameter_count": "7B",
            "quant": {"name": "Q4_K_M", "format": "gguf", "size_gb": 4.4},
            "meta": {"n_ctx_train": 8192},
        },
        {
            "id": "fixture-vision",
            "object": "model",
            "owned_by": "fixture",
            "family": "llava",
            "quant": {"name": "Q4_K_M", "format": "gguf", "size_gb": 4.9},
        },
        {
            "id": "fixture-embed",
            "object": "model",
            "owned_by": "fixture",
            "family": "nomic-bert",
            "quant": {"name": "F16", "format": "gguf", "size_gb": 0.26},
        },
        # Id-only: present, never a registry entry.
        {"id": "bare-model", "object": "model", "owned_by": "fixture"},
        # llama.cpp's real meta shape: sizes, but no family/quant names.
        {"id": "meta-only-model", "object": "model", "meta": {"size": 123, "n_params": 7}},
        # An id that cannot be a registry name at all.
        {"id": "/models/Upper Case.gguf", "object": "model", "owned_by": "fixture"},
    ],
}
KNOWN_MODELS = {entry["id"] for entry in MODELS["data"]}


class FakeTimer:
    """Each call advances a quarter second — every measured
    interval in a single call pair is exactly 250 ms."""

    def __init__(self):
        self.now = 100.0

    def __call__(self) -> float:
        value = self.now
        self.now += 0.25
        return value


class FakeCompatTransport:
    """The OpenAI-compatible wire shapes, canned: the adapter's
    transport seam filled with the server's own answers. With
    ``has_health`` the fake also speaks llama.cpp's /health."""

    def __init__(
        self,
        *,
        unreachable: bool = False,
        has_health: bool = True,
        health_status: int | None = None,
        models: dict | None = None,
        stream_mode: str = "ok",
        chat_status: int | None = None,
    ):
        self.unreachable = unreachable
        self.has_health = has_health
        self.health_status = health_status
        self.models = MODELS if models is None else models
        self.stream_mode = stream_mode
        self.chat_status = chat_status
        self.requests: list[tuple[str, str, dict | None]] = []

    def _check(self):
        if self.unreachable:
            raise OpenAICompatUnreachable("connection refused (fake transport)")

    def get_json(self, path: str) -> dict:
        self._check()
        self.requests.append(("GET", path, None))
        if path == "/health" and self.has_health:
            if self.health_status is not None:
                raise OpenAICompatHttpError(self.health_status, "loading model (fake)")
            return {"status": "ok"}
        if path == "/v1/models":
            return self.models
        raise OpenAICompatHttpError(404, f"unknown route {path} (fake)")

    def post_json(self, path: str, body: dict) -> dict:
        self._check()
        self.requests.append(("POST", path, body))
        if path == "/v1/chat/completions":
            if self.chat_status is not None:
                raise OpenAICompatHttpError(self.chat_status, "chat refused (fake)")
            if body["model"] not in KNOWN_MODELS:
                raise OpenAICompatHttpError(404, "model not found (fake)")
            return {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "the whole answer"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
            }
        if path == "/v1/embeddings":
            if body["model"] not in KNOWN_MODELS:
                raise OpenAICompatHttpError(404, "model not found (fake)")
            # Arrival order is deliberately reversed: the
            # adapter must order by the answer's index fields.
            return {
                "data": [
                    {"index": i, "embedding": [float(i), 0.5]}
                    for i in reversed(range(len(body["input"])))
                ]
            }
        raise OpenAICompatHttpError(404, f"unknown route {path} (fake)")

    def post_lines(self, path: str, body: dict):
        self._check()
        self.requests.append(("POST", path, body))
        if path != "/v1/chat/completions":
            raise OpenAICompatHttpError(404, f"unknown route {path} (fake)")
        if self.chat_status is not None:
            raise OpenAICompatHttpError(self.chat_status, "chat refused (fake)")
        if body["model"] not in KNOWN_MODELS:
            raise OpenAICompatHttpError(404, "model not found (fake)")
        if self.stream_mode == "malformed":
            yield 'data: {"choices": [{"delta": {"content": "Hel'
            return
        yield 'data: {"choices": [{"delta": {"role": "assistant", "content": ""}}]}'
        yield 'data: {"choices": [{"delta": {"content": "Hello"}}]}'
        yield ""  # SSE blank line: framing, not data
        yield 'data: {"choices": [{"delta": {"content": ", mesh"}}]}'
        if self.stream_mode == "dies_mid_stream":
            raise OpenAICompatUnreachable("server went away mid-stream (fake)")
        yield 'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}'
        if self.stream_mode == "no_done":
            return
        # A usage-only closing chunk, as servers that report
        # stream usage send it: no choices, usage only.
        yield 'data: {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}}'
        yield "data: [DONE]"


DECLARED = {"embedding_models": ["fixture-embed"], "vision_models": ["fixture-vision"]}


def llamacpp(**transport_kwargs) -> LlamaCppAdapter:
    return LlamaCppAdapter(FakeCompatTransport(**transport_kwargs), timer=FakeTimer(), **DECLARED)


def generic(**transport_kwargs) -> OpenAICompatibleAdapter:
    transport_kwargs.setdefault("has_health", False)
    return OpenAICompatibleAdapter(
        FakeCompatTransport(**transport_kwargs), timer=FakeTimer(), **DECLARED
    )


def generate_task(**overrides) -> dict:
    task = {
        "id": TASK_ID,
        "kind": "chat.generate",
        "payload": {"prompt": "say hello"},
        "caller": "y-025-tests",
        "requirements": {"model": "fixture-llm", "capabilities": ["generate"]},
    }
    task.update(overrides)
    return task


# -- discovery ---------------------------------------------------------------


def test_llamacpp_discovery_maps_the_endpoints_own_surface_honestly():
    instance = llamacpp()
    record = instance.discover()
    assert record == {
        "engine": "llamacpp",
        "reachable": True,
        "capabilities": {
            "generate": True,
            "stream": True,
            "vision": True,  # declared, and the declared model is listed
            "embed": True,  # declared, and the declared model is listed
            "provenance_grade": "unproven",  # no ledger to mirror, ever
        },
    }
    assert "version" not in record  # the shape publishes none; none is guessed
    assert instance.last_health == "ok"  # llama.cpp's own word, read alongside


def test_generic_discovery_uses_the_same_shape_under_its_own_name():
    instance = generic()
    record = instance.discover()
    assert record["engine"] == "openai-compatible"
    assert record["reachable"] is True
    assert record["capabilities"]["generate"] is True
    # A generic endpoint is never asked for a native health route.
    assert all(path != "/health" for _, path, _ in instance._transport.requests)


def test_an_unreachable_endpoint_is_a_finding_not_an_exception():
    for make in (llamacpp, generic):
        record = make(unreachable=True).discover()
        assert record["reachable"] is False
        assert record["capabilities"] == {
            "generate": False,
            "embed": False,
            "vision": False,
            "stream": False,
            "provenance_grade": "unproven",
        }


def test_an_empty_model_list_claims_no_generate():
    record = llamacpp(models={"object": "list", "data": []}).discover()
    assert record["reachable"] is True
    assert record["capabilities"]["generate"] is False
    assert record["capabilities"]["stream"] is False


def test_nothing_is_claimed_without_an_operator_declaration():
    # The vision- and embed-shaped models are listed, but the
    # adapter was built declaring neither: a name is never the
    # evidence, and an undeclared flag is never inferred.
    instance = LlamaCppAdapter(FakeCompatTransport(), timer=FakeTimer())
    record = instance.discover()
    assert record["capabilities"]["generate"] is True
    assert record["capabilities"]["vision"] is False
    assert record["capabilities"]["embed"] is False


def test_a_declaration_about_an_absent_model_claims_nothing():
    instance = LlamaCppAdapter(
        FakeCompatTransport(),
        timer=FakeTimer(),
        embedding_models=["ghost-embed"],
        vision_models=["ghost-vision"],
    )
    record = instance.discover()
    assert record["capabilities"]["embed"] is False
    assert record["capabilities"]["vision"] is False


def test_llama_health_is_advisory_and_never_gates_discovery():
    # llama-server answers /health 503 while its model loads;
    # the models surface is discovery's evidence, so a loading
    # health word is recorded, not obeyed as a capability gate.
    instance = llamacpp(health_status=503)
    record = instance.discover()
    assert record["reachable"] is True
    assert record["capabilities"]["generate"] is True
    assert instance.last_health is None
    assert instance.last_health_status == 503
    assert instance.health_status() is None


# -- models ---------------------------------------------------------------------


def test_the_model_list_is_the_endpoints_own_ids():
    assert llamacpp().list_models() == [entry["id"] for entry in MODELS["data"]]


def test_model_entries_are_built_only_from_published_fields():
    instance = llamacpp()
    entries = instance.model_entries()
    by_name = {entry["name"]: entry for entry in entries}
    llm = by_name["fixture-llm"]
    assert llm["family"] == "llama"
    assert llm["parameter_count"] == "7B"
    assert llm["quants"] == [{"name": "Q4_K_M", "format": "gguf", "size_gb": 4.4}]
    assert llm["context_tokens"] == 8192  # llama.cpp's meta, published in the answer
    assert llm["capabilities"] == ["generate", "stream"]
    assert by_name["fixture-vision"]["capabilities"] == ["generate", "stream", "vision"]
    # A declared embedding model is never dressed as a generator.
    assert by_name["fixture-embed"]["capabilities"] == ["embed"]
    assert all(entry["runnable_on"] == [] for entry in entries)  # runnable is Y-038's word
    # Id-only, meta-only, and unnameable ids are skipped by name…
    assert instance.skipped_models == ["bare-model", "meta-only-model", "/models/Upper Case.gguf"]
    # …but the model list still reports them present: presence
    # and registry-entry-worthiness are different claims.
    assert "bare-model" in instance.list_models()


# -- generate ---------------------------------------------------------------------


def test_generate_streams_tokens_and_lands_a_provenanced_result():
    result, events = llamacpp().run_task(generate_task(), NODE_ID)
    assert [event["event"] for event in events] == ["started", "token", "token", "completed"]
    assert [event["data"]["text"] for event in events if event["event"] == "token"] == [
        "Hello",
        ", mesh",
    ]
    assert [event["seq"] for event in events] == [0, 1, 2, 3]
    assert result["status"] == "completed"
    assert result["result"] == {
        "text": "Hello, mesh",
        "engine": "llamacpp",
        "finish_reason": "stop",
        "prompt_tokens": 3,  # from the usage-only closing chunk
        "completion_tokens": 5,
        "total_tokens": 8,
    }
    assert result["provenance"] == {
        "node_id": NODE_ID,
        "engine": "llamacpp",
        "model": "fixture-llm",
        "quant": "Q4_K_M",  # from the models answer the adapter discovered
    }
    assert result["timings_ms"] == {"queue": 0.0, "run": 250.0, "total": 250.0}


def test_the_generic_flavour_streams_under_its_own_engine_name():
    result, _ = generic().run_task(generate_task(), NODE_ID)
    assert result["status"] == "completed"
    assert result["result"]["engine"] == "openai-compatible"
    assert result["provenance"]["engine"] == "openai-compatible"


def test_a_non_streamed_answer_is_never_rechunked_into_a_fake_stream():
    pairs = list(llamacpp().generate(generate_task(), NODE_ID, stream=False))
    events = [event for event, _ in pairs if event is not None]
    results = [result for _, result in pairs if result is not None]
    assert [event["event"] for event in events] == ["completed"]
    assert results[0]["result"] == {
        "text": "the whole answer",
        "engine": "llamacpp",
        "finish_reason": "stop",
        "prompt_tokens": 3,
        "completion_tokens": 5,
        "total_tokens": 8,
    }


def test_messages_travel_verbatim_and_system_becomes_the_system_turn():
    instance = llamacpp()
    messages = [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
    ]
    task = generate_task(payload={"messages": messages})
    result, _ = instance.run_task(task, NODE_ID)
    assert result["status"] == "completed"
    sent = [body for _, path, body in instance._transport.requests if path == "/v1/chat/completions"]
    assert sent[0]["messages"] == messages

    instance = llamacpp()
    task = generate_task(payload={"prompt": "hi", "system": "be kind"})
    instance.run_task(task, NODE_ID)
    sent = [body for _, path, body in instance._transport.requests if path == "/v1/chat/completions"]
    assert sent[0]["messages"] == [
        {"role": "system", "content": "be kind"},
        {"role": "user", "content": "hi"},
    ]


def test_options_are_forwarded_through_the_whitelist_only():
    instance = llamacpp()
    task = generate_task(
        payload={"prompt": "hi", "options": {"temperature": 0.2, "bogus_option": "drop me"}}
    )
    instance.run_task(task, NODE_ID)
    sent = [body for _, path, body in instance._transport.requests if path == "/v1/chat/completions"]
    assert sent[0]["temperature"] == 0.2
    assert "bogus_option" not in sent[0]


def test_generate_requires_a_model_and_a_prompt():
    task = generate_task()
    del task["requirements"]
    with pytest.raises(OpenAICompatAdapterError, match="names no model") as caught:
        list(llamacpp().generate(task, NODE_ID))
    assert caught.value.error_code == "model_not_present"
    with pytest.raises(OpenAICompatAdapterError, match="payload.prompt") as caught:
        list(llamacpp().generate(generate_task(payload={}), NODE_ID))
    assert caught.value.error_code == "malformed_envelope"


def test_a_missing_model_is_named_model_not_present():
    task = generate_task(requirements={"model": "ghost"})
    result, events = llamacpp().run_task(task, NODE_ID)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "model_not_present"
    assert events[-1]["event"] == "failed"


def test_a_refused_key_is_scope_denied_and_a_full_queue_is_queue_full():
    result, _ = llamacpp(chat_status=401).run_task(generate_task(), NODE_ID)
    assert result["error"]["code"] == "scope_denied"
    result, _ = llamacpp(chat_status=429).run_task(generate_task(), NODE_ID)
    assert result["error"]["code"] == "queue_full"


def test_a_malformed_sse_line_is_an_engine_error():
    result, _ = llamacpp(stream_mode="malformed").run_task(generate_task(), NODE_ID)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "engine_error"
    assert "malformed SSE" in result["error"]["detail"]


def test_a_stream_without_done_is_incomplete_never_a_success():
    result, _ = llamacpp(stream_mode="no_done").run_task(generate_task(), NODE_ID)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "engine_error"
    assert "[DONE]" in result["error"]["detail"]
    assert result["partial"] is True
    assert result["result"]["text"] == "Hello, mesh"


def test_a_stream_that_dies_mid_answer_reports_the_partial_text():
    result, events = llamacpp(stream_mode="dies_mid_stream").run_task(generate_task(), NODE_ID)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "engine_unreachable"
    assert result["partial"] is True
    assert result["result"]["text"] == "Hello, mesh"
    assert events[-1]["event"] == "failed"


def test_an_unreachable_engine_fails_by_its_taxonomy_name():
    result, _ = generic(unreachable=True).run_task(generate_task(), NODE_ID)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "engine_unreachable"


# -- vision ---------------------------------------------------------------------


def test_vision_runs_for_a_declared_model_as_image_parts():
    instance = llamacpp()
    task = generate_task(
        kind="vision.describe",
        payload={"prompt": "what is this?", "images": ["data:image/png;base64,AAAA"]},
        requirements={"model": "fixture-vision", "capabilities": ["vision"]},
    )
    result, _ = instance.run_task(task, NODE_ID)
    assert result["status"] == "completed"
    sent = [body for _, path, body in instance._transport.requests if path == "/v1/chat/completions"]
    content = sent[0]["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "what is this?"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == "data:image/png;base64,AAAA"


def test_vision_is_refused_by_name_for_an_undeclared_model():
    task = generate_task(
        kind="vision.describe",
        payload={"prompt": "what is this?", "images": ["data:image/png;base64,AAAA"]},
        requirements={"model": "fixture-llm"},
    )
    result, _ = llamacpp().run_task(task, NODE_ID)
    assert result["status"] == "refused"
    assert result["error"]["code"] == "no_capable_node"


# -- embed + refusal ----------------------------------------------------------------


def embed_task(**overrides) -> dict:
    task = {
        "id": TASK_ID,
        "kind": "embed.text",
        "payload": {"texts": ["one", "two"]},
        "caller": "y-025-tests",
        "requirements": {"model": "fixture-embed", "capabilities": ["embed"]},
    }
    task.update(overrides)
    return task


def test_embed_runs_one_vector_per_input_ordered_by_index():
    result, events = llamacpp().run_task(embed_task(), NODE_ID)
    assert [event["event"] for event in events] == ["accepted", "completed"]
    assert result["status"] == "completed"
    # The fake answers in reversed arrival order; the answer's
    # own index fields, not arrival, set the order.
    assert result["result"]["embeddings"] == [[0.0, 0.5], [1.0, 0.5]]
    assert result["result"]["dim"] == 2


def test_embed_is_refused_by_name_for_an_undeclared_model():
    task = embed_task(requirements={"model": "fixture-llm", "capabilities": ["embed"]})
    result, _ = llamacpp().run_task(task, NODE_ID)
    assert result["status"] == "refused"
    assert result["error"]["code"] == "no_capable_node"
    assert "not declared an embedding model" in result["error"]["detail"]


def test_a_kind_the_adapter_cannot_serve_is_refused_by_name():
    task = generate_task(kind="audio.transcribe")
    result, _ = generic().run_task(task, NODE_ID)
    assert result["status"] == "refused"
    assert result["error"]["code"] == "no_capable_node"
    assert "audio.transcribe" in result["error"]["detail"]


# -- the agent's census, fed by the adapters ------------------------------------------


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


def test_the_agent_census_carries_each_flavours_live_truth(tmp_path):
    llama = llamacpp()
    agent = _provisioned_agent(
        tmp_path / "llama",
        census_probes=static_probes(),
        census_engines=lambda: [llama.capability_for_census()],
        census_models=llama.model_names_for_census,
    )
    census = agent.census()
    assert census["engines"][0]["engine"] == "llamacpp"
    assert census["engines"][0]["reachable"] is True
    assert census["models_present"] == [entry["id"] for entry in MODELS["data"]]

    compat = generic()
    agent = _provisioned_agent(
        tmp_path / "generic",
        census_probes=static_probes(),
        census_engines=lambda: [compat.capability_for_census()],
        census_models=compat.model_names_for_census,
    )
    census = agent.census()
    assert census["engines"][0]["engine"] == "openai-compatible"
    assert census["engines"][0]["reachable"] is True


def test_the_founding_readme_is_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == "89a37a4e18cab9a998e1751c32e7bfd5"
    assert json.loads((ROOT / "data" / "data_manifest.json").read_text())
