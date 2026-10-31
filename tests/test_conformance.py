"""Node conformance kit tests — Y-046's acceptance.

The kit must prove, driving the REAL Y-023 – Y-025 adapters
over fake transports speaking their own wire shapes: (a) all
four engine names (ollama, aesir, llamacpp,
openai-compatible) pass the whole kit when the caller names
their generate/embed models and hands in the same adapter
built unreachable — verdict conformant, the report validated,
skips stated only where a door genuinely does not apply
(Aesir runs no embed check: no embed model is named for it);
(b) the unreachable adapters pass the honesty checks as
returned findings; (c) an adapter broken one named way at a
time — a lying census (stream without generate, claims on a
dead engine), registry entries invented for unlisted models,
a fake stream re-chunked from a finished answer, stream text
that disagrees with its result, an unknown model that
"completes", an unservable kind served anyway, a missing
contract method — fails exactly the check that names the
break, the report counts it, and require_conformant refuses
it by name; (d) a minimal honest stub adapter passes, so the
kit is a bar for NEW adapters, not a portrait of the three
that exist; (e) the kit's own misuse is refused, a check gone
wrong never kills the kit, and the report's simulated flag is
the caller's declaration, carried verbatim; and (f) the
founding README is untouched.

No live server is spoken to: every transport here is a test
double, every kit run is declared simulated: true.
"""

import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.aesir_adapter import AesirAdapter, AesirHttpError, AesirUnreachable
from yggdrasil.conformance import (
    ConformanceError,
    ConformanceKit,
    require_conformant,
    run_conformance,
)
from yggdrasil.ollama_adapter import OllamaAdapter, OllamaHttpError, OllamaUnreachable
from yggdrasil.openai_compat_adapter import (
    LlamaCppAdapter,
    OpenAICompatHttpError,
    OpenAICompatibleAdapter,
    OpenAICompatUnreachable,
)

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
NODE_ID = "conformance-node-1"
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"


class FakeTimer:
    def __init__(self):
        self.now = 100.0

    def __call__(self) -> float:
        value = self.now
        self.now += 0.25
        return value


def checks_by_name(report: dict) -> dict:
    return {check["name"]: check for check in report["checks"]}


# -- Ollama fixture (Y-023's wire shapes) --------------------------------------

OLLAMA_TAGS = {
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
OLLAMA_SHOW = {
    "fixture-llm:latest": {"capabilities": ["completion"]},
    "fixture-embed:latest": {"capabilities": ["embedding"]},
}


class FakeOllamaTransport:
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
            return {"model": body["model"], "response": "the whole answer", "done": True}
        if path == "/api/embed":
            if body["model"] not in OLLAMA_SHOW:
                raise OllamaHttpError(404, "model not found (fake)")
            return {"embeddings": [[0.1, 0.2] for _ in body["input"]]}
        raise OllamaHttpError(404, f"unknown route {path} (fake)")

    def post_lines(self, path: str, body: dict):
        self._check()
        if path != "/api/generate":
            raise OllamaHttpError(404, f"unknown route {path} (fake)")
        if body["model"] not in OLLAMA_SHOW:
            raise OllamaHttpError(404, "model not found (fake)")
        yield json.dumps({"model": body["model"], "response": "Hello", "done": False})
        yield json.dumps({"model": body["model"], "response": ", mesh", "done": False})
        yield json.dumps({"model": body["model"], "response": "", "done": True})


def ollama(**kwargs) -> OllamaAdapter:
    return OllamaAdapter(FakeOllamaTransport(**kwargs), timer=FakeTimer())


# -- Aesir fixture (Y-024's wire shapes) ----------------------------------------

AESIR_RESIDENT = "gemma4-e2b:latest"


class FakeAesirTransport:
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
                "model": AESIR_RESIDENT,
                "digest": "sha256:fixturedigest",
                "backend": "cuda",
                "profile": "gemma4-e2b",
                "context": 4096,
            }
        if path == "/v1/models":
            return {
                "object": "list",
                "data": [
                    {"id": AESIR_RESIDENT, "object": "model", "created": 1, "owned_by": "aesir"},
                    {"id": "qwen3-0.6b:latest", "object": "model", "created": 1, "owned_by": "aesir"},
                ],
            }
        raise AesirHttpError(404, f"unknown route {path} (fake)")

    def post_json(self, path: str, body: dict) -> dict:
        self._check()
        if path != "/v1/generate":
            raise AesirHttpError(404, f"unknown route {path} (fake)")
        return {"text": "the whole answer", "finish_reason": "stop", "model": AESIR_RESIDENT}

    def post_lines(self, path: str, body: dict):
        self._check()
        if path != "/v1/generate":
            raise AesirHttpError(404, f"unknown route {path} (fake)")
        yield json.dumps({"text": "Hello", "done": False})
        yield json.dumps({"text": ", mesh", "done": False})
        yield json.dumps({"text": "", "done": True, "finish_reason": "stop", "model": AESIR_RESIDENT})


def aesir(**kwargs) -> AesirAdapter:
    return AesirAdapter(
        FakeAesirTransport(**kwargs), ledger={"AES-SRV-010": "verified"}, timer=FakeTimer()
    )


# -- OpenAI-compatible fixture (Y-025's wire shapes) -----------------------------

COMPAT_MODELS = {
    "object": "list",
    "data": [
        {
            "id": "fixture-llm",
            "object": "model",
            "family": "llama",
            "parameter_count": "7B",
            "quant": {"name": "Q4_K_M", "format": "gguf", "size_gb": 4.4},
        },
        {
            "id": "fixture-embed",
            "object": "model",
            "family": "nomic-bert",
            "quant": {"name": "F16", "format": "gguf", "size_gb": 0.26},
        },
    ],
}
COMPAT_KNOWN = {"fixture-llm", "fixture-embed"}


class FakeCompatTransport:
    def __init__(self, *, unreachable: bool = False):
        self.unreachable = unreachable

    def _check(self):
        if self.unreachable:
            raise OpenAICompatUnreachable("connection refused (fake transport)")

    def get_json(self, path: str) -> dict:
        self._check()
        if path == "/health":
            return {"status": "ok"}
        if path == "/v1/models":
            return COMPAT_MODELS
        raise OpenAICompatHttpError(404, f"unknown route {path} (fake)")

    def post_json(self, path: str, body: dict) -> dict:
        self._check()
        if path == "/v1/chat/completions":
            if body["model"] not in COMPAT_KNOWN:
                raise OpenAICompatHttpError(404, "model not found (fake)")
            return {"choices": [{"message": {"content": "the whole answer"}, "finish_reason": "stop"}]}
        if path == "/v1/embeddings":
            if body["model"] not in COMPAT_KNOWN:
                raise OpenAICompatHttpError(404, "model not found (fake)")
            return {"data": [{"index": i, "embedding": [0.1, 0.2]} for i in range(len(body["input"]))]}
        raise OpenAICompatHttpError(404, f"unknown route {path} (fake)")

    def post_lines(self, path: str, body: dict):
        self._check()
        if path != "/v1/chat/completions":
            raise OpenAICompatHttpError(404, f"unknown route {path} (fake)")
        if body["model"] not in COMPAT_KNOWN:
            raise OpenAICompatHttpError(404, "model not found (fake)")
        yield 'data: {"choices": [{"delta": {"content": "Hello"}}]}'
        yield 'data: {"choices": [{"delta": {"content": ", mesh"}}]}'
        yield 'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}'
        yield "data: [DONE]"


def llamacpp(**kwargs) -> LlamaCppAdapter:
    return LlamaCppAdapter(
        FakeCompatTransport(**kwargs),
        timer=FakeTimer(),
        embedding_models=["fixture-embed"],
    )


def generic(**kwargs) -> OpenAICompatibleAdapter:
    return OpenAICompatibleAdapter(
        FakeCompatTransport(**kwargs),
        timer=FakeTimer(),
        embedding_models=["fixture-embed"],
    )


# -- the real adapters pass --------------------------------------------------------

def test_the_ollama_adapter_is_conformant():
    report = run_conformance(
        ollama(),
        node_id=NODE_ID,
        generate_model="fixture-llm:latest",
        embed_model="fixture-embed:latest",
        unreachable_adapter=ollama(unreachable=True),
        simulated=True,
        now=lambda: FIXED,
    )
    assert report["verdict"] == "conformant", checks_by_name(report)
    assert report["engine"] == "ollama"
    assert report["simulated"] is True
    assert report["totals"] == {"checks": 16, "passed": 16, "failed": 0, "skipped": 0}


def test_the_aesir_adapter_is_conformant_with_its_absences_skipped_honestly():
    report = run_conformance(
        aesir(),
        node_id=NODE_ID,
        generate_model=AESIR_RESIDENT,
        unreachable_adapter=aesir(unreachable=True),
        simulated=True,
        now=lambda: FIXED,
    )
    assert report["verdict"] == "conformant", checks_by_name(report)
    assert report["engine"] == "aesir"
    by_name = checks_by_name(report)
    # No embed model is named for Aesir — its native surface
    # serves none — so the embed check is a stated skip.
    assert by_name["embed_completes"]["status"] == "skipped"
    assert report["totals"]["passed"] + report["totals"]["skipped"] == 16


def test_both_openai_compatible_flavours_are_conformant():
    for make, engine in ((llamacpp, "llamacpp"), (generic, "openai-compatible")):
        report = run_conformance(
            make(),
            node_id=NODE_ID,
            generate_model="fixture-llm",
            embed_model="fixture-embed",
            unreachable_adapter=make(unreachable=True),
            simulated=True,
            now=lambda: FIXED,
        )
        assert report["verdict"] == "conformant", (engine, checks_by_name(report))
        assert report["engine"] == engine
        assert report["totals"]["failed"] == 0


def test_require_conformant_returns_the_report_for_a_passing_adapter():
    report = require_conformant(
        ollama(),
        node_id=NODE_ID,
        generate_model="fixture-llm:latest",
        embed_model="fixture-embed:latest",
        unreachable_adapter=ollama(unreachable=True),
        simulated=True,
    )
    assert report["verdict"] == "conformant"


def test_a_run_without_optional_naming_skips_instead_of_passing_quietly():
    report = run_conformance(ollama(), node_id=NODE_ID, simulated=True, now=lambda: FIXED)
    by_name = checks_by_name(report)
    assert by_name["generate_completes"]["status"] == "skipped"
    assert by_name["embed_completes"]["status"] == "skipped"
    assert by_name["unreachable_claims_nothing"]["status"] == "skipped"
    assert by_name["unreachable_task_named"]["status"] == "skipped"
    # The error doors still ran: an unnamed adapter must still
    # refuse what it cannot serve, by name.
    assert by_name["unknown_model_named"]["status"] == "passed"
    assert by_name["unservable_kind_refused"]["status"] == "passed"
    assert report["verdict"] == "conformant"


def test_an_unreachable_adapter_alone_is_examined_honestly():
    report = run_conformance(
        ollama(unreachable=True), node_id=NODE_ID, simulated=True, now=lambda: FIXED
    )
    by_name = checks_by_name(report)
    assert by_name["unreachable_claims_nothing"]["status"] == "passed"
    assert by_name["discovery_shape"]["status"] == "passed"
    assert by_name["model_list_shape"]["status"] == "skipped"
    assert by_name["unknown_model_named"]["status"] == "passed"
    assert report["verdict"] == "conformant"


def test_one_event_per_run_names_and_counts_only():
    kit = ConformanceKit(
        ollama(),
        node_id=NODE_ID,
        generate_model="fixture-llm:latest",
        simulated=True,
        now=lambda: FIXED,
    )
    kit.run()
    (event,) = kit.events
    assert event["type"] == "conformance.passed"
    assert event["payload"]["engine"] == "ollama"
    assert event["payload"]["checks"] == 16


# -- a stub adapter, honest and broken one way at a time ---------------------------

class StubAdapter:
    """A minimal adapter that honours the contract — the bar
    a NEW adapter must clear — with one named break at a time."""

    engine = "ollama"
    is_simulated = True

    def __init__(self, *, fault: str | None = None):
        self.fault = fault
        self.skipped_models: list[str] = []

    def discover(self) -> dict:
        caps = {"generate": True, "embed": False, "vision": False, "stream": True,
                "provenance_grade": "unproven"}
        if self.fault == "stream_without_generate":
            caps["generate"] = False
        if self.fault == "dead_claims":
            return {"engine": "ollama", "reachable": False, "capabilities": caps}
        return {"engine": "ollama", "reachable": True, "capabilities": caps}

    def capability_for_census(self) -> dict:
        return self.discover()

    def list_models(self) -> list[str]:
        return ["stub-llm"]

    def model_names_for_census(self) -> list[str]:
        return self.list_models()

    def model_entries(self) -> list[dict]:
        if self.fault == "stream_without_generate":
            return []
        entry = {
            "name": "stub-llm",
            "family": "stub",
            "quants": [{"name": "Q4", "format": "gguf", "size_gb": 1.0}],
            "capabilities": ["generate", "stream"],
            "runnable_on": [],
        }
        if self.fault == "invents_entries":
            entry = dict(entry, name="ghost-llm")
        return [entry]

    def _result(self, task, node_id, *, status="completed", text="Hello, mesh", code=None):
        result = {
            "task_id": task["id"],
            "status": status,
            "provenance": {"node_id": node_id, "engine": "ollama", "model": "stub-llm"},
            "timings_ms": {"queue": 0.0, "run": 1.0, "total": 1.0},
        }
        if status == "completed":
            result["result"] = {"text": text, "engine": "ollama"}
        else:
            result["error"] = {"code": code, "detail": "stub refusal"}
        return result

    def _event(self, task, seq, event, text=None):
        record = {"task_id": task["id"], "seq": seq, "event": event}
        if text is not None:
            record["data"] = {"text": text}
        return record

    def run_task(self, task, node_id):
        model = (task.get("requirements") or {}).get("model")
        kind = task.get("kind", "")
        if self.fault == "serves_the_unknown" and model == "conformance-ghost:latest":
            result = self._result(task, node_id)
            return result, [self._event(task, 0, "completed")]
        if model == "conformance-ghost:latest":
            result = self._result(task, node_id, status="failed", code="model_not_present")
            return result, [self._event(task, 0, "failed")]
        if kind == "conformance.unservable":
            if self.fault == "serves_unservable":
                result = self._result(task, node_id)
                return result, [self._event(task, 0, "completed")]
            result = self._result(task, node_id, status="refused", code="no_capable_node")
            return result, [self._event(task, 0, "failed")]
        text = "Different, mesh" if self.fault == "stream_lies" else "Hello, mesh"
        result = self._result(task, node_id)
        events = [
            self._event(task, 0, "started"),
            self._event(task, 1, "token", "Hello"),
            self._event(task, 2, "token", ", mesh"),
            self._event(task, 3, "completed"),
        ]
        if self.fault == "stream_lies":
            events[1] = self._event(task, 1, "token", text.split(",")[0])
        return result, events

    def generate(self, task, node_id, *, stream=True):
        if not stream:
            if self.fault == "fake_stream":
                yield self._event(task, 0, "token", "the"), None
                yield self._event(task, 1, "token", " whole answer"), None
            yield self._event(task, 2 if self.fault == "fake_stream" else 0, "completed"), None
            yield None, self._result(task, node_id, text="the whole answer")
            return
        result, events = self.run_task(task, node_id)
        for event in events:
            yield event, None
        yield None, result


def stub_report(fault: str | None = None, **kwargs):
    kwargs.setdefault("node_id", NODE_ID)
    kwargs.setdefault("generate_model", "stub-llm")
    kwargs.setdefault("simulated", True)
    kwargs.setdefault("now", lambda: FIXED)
    return run_conformance(StubAdapter(fault=fault), **kwargs)


def test_a_minimal_honest_stub_adapter_is_conformant():
    report = stub_report()
    assert report["verdict"] == "conformant", checks_by_name(report)


@pytest.mark.parametrize(
    ("fault", "check"),
    [
        ("stream_without_generate", "stream_follows_generate"),
        ("invents_entries", "model_entries_honest"),
        ("fake_stream", "no_fake_stream"),
        ("stream_lies", "stream_shape_and_text"),
        ("serves_the_unknown", "unknown_model_named"),
        ("serves_unservable", "unservable_kind_refused"),
    ],
)
def test_each_named_break_fails_exactly_its_check(fault, check):
    report = stub_report(fault)
    by_name = checks_by_name(report)
    assert report["verdict"] == "non_conformant"
    assert by_name[check]["status"] == "failed", by_name[check]
    failed = [name for name, record in by_name.items() if record["status"] == "failed"]
    assert failed == [check], failed
    with pytest.raises(ConformanceError, match=check):
        require_conformant(
            StubAdapter(fault=fault),
            node_id=NODE_ID,
            generate_model="stub-llm",
            simulated=True,
        )


def test_a_dead_adapter_that_still_claims_fails_the_unreachable_check():
    report = run_conformance(
        StubAdapter(),
        node_id=NODE_ID,
        generate_model="stub-llm",
        unreachable_adapter=StubAdapter(fault="dead_claims"),
        simulated=True,
        now=lambda: FIXED,
    )
    by_name = checks_by_name(report)
    assert by_name["unreachable_claims_nothing"]["status"] == "failed"
    assert "generate" in by_name["unreachable_claims_nothing"]["detail"]
    assert report["verdict"] == "non_conformant"


def test_a_missing_contract_method_fails_the_interface_check():
    adapter = StubAdapter()
    adapter.list_models = None  # type: ignore[assignment]
    report = run_conformance(
        adapter, node_id=NODE_ID, generate_model="stub-llm", simulated=True, now=lambda: FIXED
    )
    by_name = checks_by_name(report)
    assert by_name["interface_surface"]["status"] == "failed"
    assert "list_models" in by_name["interface_surface"]["detail"]
    assert report["verdict"] == "non_conformant"


def test_the_kit_refuses_its_own_misuse_and_survives_a_crashing_adapter():
    with pytest.raises(ConformanceError, match="needs an adapter"):
        ConformanceKit(None)
    with pytest.raises(ConformanceError, match="node_id"):
        ConformanceKit(StubAdapter(), node_id="")

    class ExplodingAdapter(StubAdapter):
        def discover(self):
            raise RuntimeError("kaboom (stub)")

    report = run_conformance(
        ExplodingAdapter(), node_id=NODE_ID, simulated=True, now=lambda: FIXED
    )
    by_name = checks_by_name(report)
    assert by_name["discovery_shape"]["status"] == "failed"
    assert "kaboom" in by_name["discovery_shape"]["detail"]
    assert report["totals"]["checks"] == 16  # the kit finished all the same


def test_the_simulated_flag_is_the_callers_declaration_carried_verbatim():
    honest = copy.deepcopy(StubAdapter())
    report = run_conformance(
        honest, node_id=NODE_ID, generate_model="stub-llm", simulated=False, now=lambda: FIXED
    )
    assert report["simulated"] is False


# -- the founding document ---------------------------------------------------------

def test_readme_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
