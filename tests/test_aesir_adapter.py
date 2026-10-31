"""Engine adapter II (Aesir) tests — Y-024's acceptance.

The adapter must prove, against a faithful fake transport that
speaks Aesir's native wire shapes (per its own API support
matrix v1): (a) the ledger parser reads Aesir's own markdown
shape and normalises honestly — compound/historical statuses
become "other", never a claim; (b) discovery requires BOTH
words — the ledger's (AES-SRV-010 verified -> generate/stream
claimed, grade proven; partial -> claimed at grade partial;
missing/simulated/absent -> nothing claimed, grade unproven)
and the live surface's (unreachable is a returned finding;
live-but-not-ready claims no generate; something that is not
Aesir's liveness shape is not this engine); (c) the native
API's absences stay absent — embed and vision tasks are
refused by name, catalog models are listed but never dressed
into registry entries from ids alone; (d) generate honours the
resident-only rule before any generation attempt, streams
NDJSON text records into token events with the terminal
record required — a stream ending without it fails, with the
partial text kept — and a non-streamed answer is never
re-chunked; service timings (prefill+decode) are preferred
over the wall clock when Aesir reports them; (e) failures are
named in the Y-007 taxonomy — 401 scope_denied, 404
model_not_present, 504 deadline_unmeetable, a dead server
engine_unreachable; (f) the node agent's census carries the
adapter's discovery (grade and all) through Y-022's seams;
and (g) the founding README is untouched.

No live Aesir service is spoken to in this suite: the fake
transport is the seam's test double, and the citizen slices
(Y-033 – Y-035) own the on-hardware proof — the laptop citizen
(Y-034) is where Aesir's GPU line runs for real.
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.aesir_adapter import (
    AesirAdapter,
    AesirAdapterError,
    AesirHttpError,
    AesirUnreachable,
    parse_ledger,
)
from yggdrasil.census import ProbeSet
from yggdrasil.identity import LocalKeyStore, NodeKeypair, create_mesh_root, issue_node_certificate
from yggdrasil.node_agent import NodeAgent, NodeConfig

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
NODE_ID = "aesir-node-1"
TASK_ID = "3f6f1b2c-9d4e-4c5a-8b7d-000000000024"
RESIDENT = "gemma4-e2b:latest"

# A ledger in Aesir's own markdown shape, carrying the real
# current statuses of the entries this adapter reasons about
# (AES-SRV-010 verified, AES-SRV-008 partial, AES-RAG-003
# partial, AES-SRV-007 missing) plus shapes the parser must
# refuse to over-read: a compound status and a historical one.
LEDGER_TEXT = """# Project A.E.S.I.R. Canonical Capability Ledger

### AES-SRV-007 — llama.cpp HTTP compatibility routes

- **Status:** `missing`
- **Owner:** server protocol domain

### AES-SRV-008 — Ollama HTTP API compatibility

- **Status:** `partial`
- **Owner:** server, CLI, store, and inference domains

### AES-SRV-010 — Authenticated native loopback CUDA generation

- **Status:** `verified`
- **Owner:** CLI orchestration, server transport/protocol, facade session contract

### AES-RAG-003 — Query embedding generation

- **Status:** `partial`
- **Owner:** facade and embedding domains

### AES-OLD-001 — A compound gate annotation

- **Status:** `verified, Phase-C gate PASS (READY_FW)`

### AES-OLD-002 — A withdrawn road

- **Status:** `historical / CPU inference supervision withdrawn (2026-10-12)`

### AES-OLD-003 — An entry whose status line never arrived
"""

LEDGER = parse_ledger(LEDGER_TEXT)

LIVE = {"schema_version": 1, "status": "ok", "scope": "process"}
READY = {
    "schema_version": 1,
    "status": "ready",
    "ready": True,
    "model": RESIDENT,
    "digest": "sha256:fixturedigest",
    "backend": "cuda",
    "profile": "gemma4-e2b",
    "context": 4096,
}
MODELS = {
    "object": "list",
    "data": [
        {"id": RESIDENT, "object": "model", "created": 1, "owned_by": "aesir"},
        {"id": "qwen3-0.6b:latest", "object": "model", "created": 1, "owned_by": "aesir"},
    ],
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


class FakeAesirTransport:
    """Aesir's native wire shapes, canned: the adapter's
    transport seam filled with the server's own answers
    (the bearer key lives here, as it does in deployment —
    the adapter never sees it)."""

    def __init__(
        self,
        *,
        unreachable: bool = False,
        ready: bool = True,
        live_shape: dict | None = None,
        ready_status: int | None = None,
        auth_status: int | None = None,
        stream_mode: str = "ok",
        service_timings: bool = True,
    ):
        self.unreachable = unreachable
        self.ready = ready
        self.live_shape = LIVE if live_shape is None else live_shape
        self.ready_status = ready_status
        self.auth_status = auth_status
        self.stream_mode = stream_mode
        self.service_timings = service_timings
        self.requests: list[tuple[str, str, dict | None]] = []

    def _check(self):
        if self.unreachable:
            raise AesirUnreachable("connection refused (fake transport)")
        if self.auth_status is not None:
            raise AesirHttpError(self.auth_status, "unauthorized (fake)")

    def get_json(self, path: str) -> dict:
        self._check()
        self.requests.append(("GET", path, None))
        if path == "/health/live":
            return self.live_shape
        if path == "/health/ready":
            if self.ready_status is not None:
                raise AesirHttpError(self.ready_status, "not ready (fake)")
            if not self.ready:
                raise AesirHttpError(503, "not_ready (fake)")
            return READY
        if path == "/v1/models":
            return MODELS
        raise AesirHttpError(404, f"unknown route {path} (fake)")

    def _timings(self) -> dict:
        return {"timings_ms": {"prefill": 12.0, "decode": 30.0}} if self.service_timings else {}

    def post_json(self, path: str, body: dict) -> dict:
        self._check()
        self.requests.append(("POST", path, body))
        if path != "/v1/generate":
            raise AesirHttpError(404, f"unknown route {path} (fake)")
        if self.stream_mode == "deadline":
            raise AesirHttpError(504, "generation deadline (fake)")
        answer = {
            "text": "the whole answer",
            "finish_reason": "stop",
            "prompt_tokens": 4,
            "generated_tokens": 3,
            "context_used": 7,
            "backend": "cuda",
            "cpu_offload": 0,
            "model": RESIDENT,
        }
        answer.update(self._timings())
        return answer

    def post_lines(self, path: str, body: dict):
        self._check()
        self.requests.append(("POST", path, body))
        if path != "/v1/generate":
            raise AesirHttpError(404, f"unknown route {path} (fake)")
        if self.stream_mode == "malformed":
            yield '{"text": "Hel'
            return
        yield json.dumps({"text": "Hello", "done": False})
        yield json.dumps({"text": ", mesh", "done": False})
        if self.stream_mode == "dies_mid_stream":
            raise AesirUnreachable("server went away mid-stream (fake)")
        if self.stream_mode == "no_terminal":
            return
        terminal = {
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
        terminal.update(self._timings())
        yield json.dumps(terminal)


def adapter(ledger=LEDGER, **transport_kwargs) -> AesirAdapter:
    return AesirAdapter(FakeAesirTransport(**transport_kwargs), ledger=ledger, timer=FakeTimer())


def generate_task(**overrides) -> dict:
    task = {
        "id": TASK_ID,
        "kind": "chat.generate",
        "payload": {"prompt": "say hello"},
        "caller": "y-024-tests",
        "requirements": {"model": RESIDENT, "capabilities": ["generate"]},
    }
    task.update(overrides)
    return task


# -- the ledger parser -------------------------------------------------------


def test_parse_ledger_reads_statuses_and_refuses_to_overread():
    assert LEDGER["AES-SRV-010"] == "verified"
    assert LEDGER["AES-SRV-008"] == "partial"
    assert LEDGER["AES-RAG-003"] == "partial"
    assert LEDGER["AES-SRV-007"] == "missing"
    # A compound annotation's leading word is all that is read.
    assert LEDGER["AES-OLD-001"] == "verified"
    # A historical/withdrawn status is not a claimable word.
    assert LEDGER["AES-OLD-002"] == "other"
    # No status line -> no entry at all.
    assert "AES-OLD-003" not in LEDGER


def test_ledger_summary_counts_the_snapshot():
    counts = adapter().ledger_summary()
    assert counts == {"verified": 2, "partial": 2, "missing": 1, "other": 1}


# -- discovery: both words required -------------------------------------------


def test_discovery_with_verified_ledger_and_ready_resident():
    record = adapter().discover()
    assert record["engine"] == "aesir"
    assert record["reachable"] is True
    assert record["capabilities"]["generate"] is True
    assert record["capabilities"]["stream"] is True
    # The native API serves neither, whatever the ledger's
    # engine-internal entries say.
    assert record["capabilities"]["embed"] is False
    assert record["capabilities"]["vision"] is False
    assert record["capabilities"]["provenance_grade"] == "proven"


def test_discovery_partial_ledger_grades_partial_never_proven():
    partial = dict(LEDGER, **{"AES-SRV-010": "partial"})
    record = adapter(ledger=partial).discover()
    assert record["capabilities"]["generate"] is True
    assert record["capabilities"]["provenance_grade"] == "partial"


@pytest.mark.parametrize("status", ["missing", "scaffold", "simulated", "blocked", "other"])
def test_discovery_unclaimable_ledger_statuses_claim_nothing(status):
    downgraded = dict(LEDGER, **{"AES-SRV-010": status})
    record = adapter(ledger=downgraded).discover()
    assert record["reachable"] is True  # the server is up...
    assert record["capabilities"]["generate"] is False  # ...but the mesh promises nothing
    assert record["capabilities"]["stream"] is False
    assert record["capabilities"]["provenance_grade"] == "unproven"


def test_discovery_without_the_ledger_entry_claims_nothing():
    record = adapter(ledger={}).discover()
    assert record["capabilities"]["generate"] is False
    assert record["capabilities"]["provenance_grade"] == "unproven"


def test_discovery_unreachable_is_a_finding_not_an_exception():
    record = adapter(unreachable=True).discover()
    assert record["reachable"] is False
    assert record["capabilities"]["generate"] is False
    # The ledger's grade still rides along: proven work on a
    # server that is down is a down server, graded honestly.
    assert record["capabilities"]["provenance_grade"] == "proven"


def test_discovery_live_but_not_ready_claims_no_generate():
    record = adapter(ready=False).discover()
    assert record["reachable"] is True
    assert record["capabilities"]["generate"] is False


def test_discovery_something_else_on_the_port_is_not_aesir():
    record = adapter(live_shape={"status": "ok-ish", "hello": "world"}).discover()
    assert record["reachable"] is False
    assert record["capabilities"]["generate"] is False


# -- models: visibility is not runnability --------------------------------------


def test_list_models_and_resident_model_are_distinct_truths():
    instance = adapter()
    assert instance.list_models() == [RESIDENT, "qwen3-0.6b:latest"]
    # Two models are listed; exactly one may generate.
    assert instance.resident_model() == RESIDENT


def test_model_entries_are_never_dressed_up_from_ids_alone():
    instance = adapter()
    assert instance.model_entries() == []
    assert instance.skipped_models == [RESIDENT, "qwen3-0.6b:latest"]


# -- generate -------------------------------------------------------------------


def test_generate_streams_tokens_and_requires_the_terminal_record():
    result, events = adapter().run_task(generate_task(), NODE_ID)
    assert result["status"] == "completed"
    assert result["result"]["text"] == "Hello, mesh"
    assert result["result"]["finish_reason"] == "stop"
    assert result["result"]["generated_tokens"] == 3
    assert result["result"]["backend"] == "cuda"
    assert [event["event"] for event in events] == ["started", "token", "token", "completed"]
    # Aesir's own service timings (12 + 30) beat the wall clock.
    assert result["timings_ms"]["run"] == 42.0
    # The provenance carries the ledger citation for its grade.
    assert result["provenance"]["verification"] == "aesir-ledger:AES-SRV-010=verified"
    assert result["provenance"]["model"] == RESIDENT


def test_generate_without_service_timings_uses_the_timer():
    result, _ = adapter(service_timings=False).run_task(generate_task(), NODE_ID)
    assert result["status"] == "completed"
    assert result["timings_ms"]["run"] == 250.0  # one FakeTimer interval pair


def test_generate_non_streamed_is_never_rechunked():
    pairs = list(adapter().generate(generate_task(), NODE_ID, stream=False))
    events = [event for event, _ in pairs if event is not None]
    result = next(res for _, res in pairs if res is not None)
    assert [event["event"] for event in events] == ["completed"]
    assert result["result"]["text"] == "the whole answer"


def test_generate_maps_only_whitelisted_options():
    transport = FakeAesirTransport()
    instance = AesirAdapter(transport, ledger=LEDGER, timer=FakeTimer())
    task = generate_task(
        payload={
            "prompt": "say hello",
            "options": {"temperature": 0.5, "max_tokens": 16, "evil": "rm -rf"},
        }
    )
    result, _ = instance.run_task(task, NODE_ID)
    assert result["status"] == "completed"
    body = next(body for _, path, body in transport.requests if path == "/v1/generate")
    assert body["temperature"] == 0.5
    assert body["max_tokens"] == 16
    assert "evil" not in body


def test_generate_for_a_non_resident_model_is_refused_before_any_attempt():
    transport = FakeAesirTransport()
    instance = AesirAdapter(transport, ledger=LEDGER, timer=FakeTimer())
    task = generate_task(requirements={"model": "qwen3-0.6b:latest"})
    result, _ = instance.run_task(task, NODE_ID)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "model_not_present"
    # Listed in the catalog, visible — and never attempted:
    # only the resident model may generate.
    assert not any(path == "/v1/generate" for _, path, _ in transport.requests)


def test_generate_with_no_ready_resident_fails_unreachable():
    result, _ = adapter(ready=False).run_task(generate_task(), NODE_ID)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "engine_unreachable"


def test_stream_without_terminal_record_is_failure_with_partial_text():
    result, events = adapter(stream_mode="no_terminal").run_task(generate_task(), NODE_ID)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "engine_error"
    assert "terminal record" in result["error"]["detail"]
    assert result["partial"] is True
    assert result["result"]["text"] == "Hello, mesh"
    assert events[-1]["event"] == "failed"


def test_stream_dying_mid_answer_keeps_the_partial_text():
    result, _ = adapter(stream_mode="dies_mid_stream").run_task(generate_task(), NODE_ID)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "engine_unreachable"
    assert result["partial"] is True
    assert result["result"]["text"] == "Hello, mesh"


def test_malformed_stream_line_is_engine_error():
    result, _ = adapter(stream_mode="malformed").run_task(generate_task(), NODE_ID)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "engine_error"


def test_deadline_maps_to_deadline_unmeetable():
    pairs_error = None
    try:
        list(adapter(stream_mode="deadline").generate(generate_task(), NODE_ID, stream=False))
    except AesirAdapterError as exc:
        pairs_error = exc
    assert pairs_error is not None
    assert pairs_error.error_code == "deadline_unmeetable"


def test_refused_key_maps_to_scope_denied():
    result, _ = adapter(auth_status=401).run_task(generate_task(), NODE_ID)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "scope_denied"


def test_generate_without_a_prompt_is_malformed():
    task = generate_task(payload={})
    result, _ = adapter().run_task(task, NODE_ID)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "malformed_envelope"


# -- the native API's absences ---------------------------------------------------


def test_embed_tasks_are_refused_by_name():
    task = generate_task(kind="embed.text", payload={"texts": ["hello"]})
    result, _ = adapter().run_task(task, NODE_ID)
    assert result["status"] == "refused"
    assert result["error"]["code"] == "no_capable_node"
    assert "embeddings as absent" in result["error"]["detail"]


def test_vision_tasks_are_refused_by_name():
    task = generate_task(kind="vision.describe", payload={"prompt": "what is this?"})
    result, _ = adapter().run_task(task, NODE_ID)
    assert result["status"] == "refused"
    assert result["error"]["code"] == "no_capable_node"


def test_unknown_kinds_are_refused():
    task = generate_task(kind="audio.transcribe")
    result, _ = adapter().run_task(task, NODE_ID)
    assert result["status"] == "refused"
    assert result["error"]["code"] == "no_capable_node"


# -- census carriage ---------------------------------------------------------------


def static_probes() -> ProbeSet:
    return ProbeSet(
        cpu=lambda: {"arch": "x86_64", "cores": 8},
        ram_gb=lambda: 16.0,
        accelerators=lambda: [{"kind": "cuda", "name": "fixture GPU", "proven": False}],
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
        device_class="laptop",
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
            "device_class": "laptop",
            "key_store_dir": str(store_dir),
        }
    )
    return NodeAgent(config, now=lambda: FIXED, **agent_kwargs)


def test_the_agent_census_carries_the_ledger_graded_discovery(tmp_path):
    instance = adapter()
    agent = _provisioned_agent(
        tmp_path,
        census_probes=static_probes(),
        census_engines=lambda: [instance.capability_for_census()],
        census_models=instance.model_names_for_census,
    )
    census = agent.census()
    engine = census["engines"][0]
    assert engine["engine"] == "aesir"
    assert engine["reachable"] is True
    assert engine["capabilities"]["generate"] is True
    # The census carries Aesir's own grade, straight from its
    # ledger — proven here, and never laundered upward.
    assert engine["capabilities"]["provenance_grade"] == "proven"
    assert census["models_present"] == [RESIDENT, "qwen3-0.6b:latest"]


def test_the_founding_readme_is_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == "89a37a4e18cab9a998e1751c32e7bfd5"
