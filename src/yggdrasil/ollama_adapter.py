"""Engine adapter I — Ollama — Y-023.

The first real engine wrapped behind the node contract
(ADR-004: engines are wrapped, never re-implemented; Ollama is
the common local shelf, second in the order of regard after
Aesir). The adapter speaks Ollama's own HTTP API shapes —
``GET /api/version``, ``GET /api/tags``, ``POST /api/show``,
``POST /api/generate`` (NDJSON when streaming), ``POST
/api/embed`` — and maps what the engine's own surface reports
into the contract shapes the mesh already holds:

- **discovery** — ``discover()`` answers a validated Y-008
  ``EngineCapability``: reachable or not, the server's version
  when it publishes one, and capabilities mapped honestly (see
  below). An unreachable server is a *finding*, not an
  exception: discovery returns ``reachable: False`` with every
  capability False, because the census (Y-022) needs exactly
  that truth to carry.
- **model list** — ``list_models()`` returns the names
  ``/api/tags`` reports, and ``model_entries()`` maps each tag
  into a validated Y-008 registry ``ModelEntry`` using only
  fields Ollama itself publishes (family, parameter size,
  quantisation level and format, byte size). A field Ollama
  does not publish is not invented: the entry is built from
  what arrived, and a tag with no family cannot become a
  registry entry at all — it is skipped by name in the
  adapter's ``skipped_models`` ledger rather than dressed up.
- **generate (streaming)** — ``generate()`` runs one validated
  Y-007 task through ``/api/generate`` with ``stream: true``
  and yields validated StreamEvents (``started``, one
  ``token`` per NDJSON chunk, ``completed``), then a terminal
  validated ResultEnvelope with provenance (node, engine,
  model, and the quant the tag reported). The stream is the
  engine's own — ADR-004's rule holds: the fabric never fakes
  a stream by chunking a finished answer, so a caller that
  asks for ``stream=False`` gets exactly one ``completed``
  event and no token events at all. ``embed()`` runs embedding
  tasks through ``/api/embed``. ``run_task()`` is the
  collecting front door: task in, (result, stream) out, with
  every failure named in the Y-007 taxonomy.

Honest capability mapping (ADR-004's per-engine rules):

- ``generate`` is claimed only when the server is reachable
  AND at least one model is present — a reachable server with
  an empty shelf cannot generate anything on this node yet.
- ``stream`` follows ``generate``: streaming generate is the
  path this adapter actually drives, never a decoration.
- ``vision`` / ``embed`` are claimed only when a present
  model's own ``/api/show`` capabilities list names them
  (``vision`` / ``embedding`` in Ollama's vocabulary). A model
  *name* that sounds visual proves nothing; the name is never
  the evidence.
- ``provenance_grade`` stays ``unproven`` whatever discovery
  finds. Ollama keeps no proof ledger of its own to mirror,
  and ADR-004 forbids laundering a grade upward: reachability
  is measured here, proven inference is not claimed.

The transport seam: this module carries no network client.
Ollama is spoken through an injected transport — three methods
(``get_json``, ``post_json``, ``post_lines``) that return
parsed JSON or raw response lines and raise this module's
``OllamaUnreachable`` / ``OllamaHttpError`` — the same
injectable-boundary pattern the census probes (Y-022) and the
simulator handlers (Y-014) use, and the reason the Part I
gate's zero-network-imports check still passes over Part II's
first adapter. The NDJSON parsing, request shaping, and error
mapping are all here and all tested against a faithful fake
transport; the socket-carrying transport itself is deployment
wiring at the seam, and no test in this slice claims a live
Ollama server was spoken to — that proof belongs to the
citizen slices (Y-033 – Y-035) on their own hardware.

Errors: a transport failure before any answer is
``engine_unreachable``; an HTTP 404 on generate/embed is
``model_not_present``; any other HTTP failure, a malformed
NDJSON line, or a response missing the fields its endpoint
owes is ``engine_error``. A failure mid-stream keeps the text
already streamed and reports the result ``partial: True`` —
what the caller already holds is never taken back or hidden.
"""

import json
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

ENGINE_NAME = "ollama"

# Ollama's /api/show capability vocabulary, mapped into the
# contract's. Only these words, arriving from the engine
# itself, may raise the matching capability — nothing else.
_SHOW_CAPABILITY_MAP = {"completion": "generate", "vision": "vision", "embedding": "embed"}


def _load_schema(name: str) -> dict:
    return json.loads((_SCHEMAS / name).read_text())


def _validator(name: str):
    """A validator for one schema, with every contract schema
    registered so relative $refs resolve — the registry pattern
    the earlier slices build."""
    schema = _load_schema(name)
    resources = []
    for path in sorted(_SCHEMAS.glob("*.json")):
        other = json.loads(path.read_text())
        resource = Resource.from_contents(other)
        resources.append((path.name, resource))
        resources.append((other["$id"], resource))
        resources.append((other["$id"].rsplit("/", 1)[0] + "/" + path.name, resource))
    registry = Registry().with_resources(resources)
    return validators.validator_for(schema)(schema, registry=registry)


_ENGINE_VALIDATOR = _validator("engine_capability.schema.json")
_MODEL_VALIDATOR = _validator("model_entry.schema.json")
_TASK_VALIDATOR = _validator("task_envelope.schema.json")
_RESULT_VALIDATOR = _validator("result_envelope.schema.json")
_STREAM_VALIDATOR = _validator("stream_event.schema.json")


class OllamaAdapterError(Exception):
    """Any adapter failure. ``error_code`` names it in the
    Y-007 taxonomy when the failure is a task failure."""

    def __init__(self, message: str, *, error_code: str | None = None):
        super().__init__(message)
        self.error_code = error_code


class OllamaTransportError(OllamaAdapterError):
    """The transport seam's failure base — a real transport
    raises these; the adapter maps them, never sees sockets."""


class OllamaUnreachable(OllamaTransportError):
    """No answer at all: connection refused, timed out, gone."""


class OllamaHttpError(OllamaTransportError):
    """The server answered with a non-success HTTP status."""

    def __init__(self, status: int, detail: str = ""):
        super().__init__(f"ollama answered HTTP {status}" + (f": {detail}" if detail else ""))
        self.status = status
        self.detail = detail


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise OllamaAdapterError(f"{kind} fails its schema: {errors[0].message}")
    return record


def _task_model(task: dict) -> str | None:
    requirements = task.get("requirements") or {}
    if requirements.get("model"):
        return requirements["model"]
    payload = task.get("payload") or {}
    return payload.get("model") if isinstance(payload.get("model"), str) else None


class OllamaAdapter:
    """One node's Ollama, wrapped. ``transport`` is the seam
    described in the module docstring; ``timer`` (seconds,
    monotonic) is injectable so run timings are deterministic
    in tests and honestly measured in life."""

    engine = ENGINE_NAME
    is_simulated = False

    def __init__(self, transport, *, base_url: str = "http://127.0.0.1:11434", timer=None):
        self._transport = transport
        self.base_url = base_url.rstrip("/")
        self._timer = timer or time.perf_counter
        self._tags: list[dict] = []  # the last /api/tags answer, verbatim
        self._show: dict[str, dict] = {}  # model name -> last /api/show answer
        self.skipped_models: list[str] = []

    # -- transport, mapped ------------------------------------------------

    def _get_json(self, path: str) -> dict:
        return self._transport.get_json(path)

    def _post_json(self, path: str, body: dict) -> dict:
        return self._transport.post_json(path, body)

    def _post_lines(self, path: str, body: dict) -> Iterable[str]:
        return self._transport.post_lines(path, body)

    @staticmethod
    def _map_transport_error(exc: OllamaTransportError, *, model: str | None = None) -> str:
        """A transport failure's Y-007 name: unreachable is its
        own code; a 404 while asking for one model is that
        model not being present; anything else is the engine's
        error to own."""
        if isinstance(exc, OllamaUnreachable):
            return "engine_unreachable"
        if isinstance(exc, OllamaHttpError) and exc.status == 404 and model is not None:
            return "model_not_present"
        return "engine_error"

    # -- discovery ----------------------------------------------------------

    def _fetch_tags(self) -> list[dict]:
        answer = self._get_json("/api/tags")
        models = answer.get("models") if isinstance(answer, dict) else None
        if not isinstance(models, list):
            raise OllamaAdapterError(
                "/api/tags answered without a models list", error_code="engine_error"
            )
        self._tags = [entry for entry in models if isinstance(entry, dict) and entry.get("name")]
        return self._tags

    def _fetch_show(self, name: str) -> dict:
        """One model's own capability surface, or {} when the
        server cannot show it — absence of evidence, kept as
        absence (no capability is raised on a failed show)."""
        try:
            answer = self._post_json("/api/show", {"name": name})
        except OllamaTransportError:
            return {}
        show = answer if isinstance(answer, dict) else {}
        self._show[name] = show
        return show

    def _model_capabilities(self, name: str) -> set[str]:
        show = self._show.get(name)
        if show is None:
            show = self._fetch_show(name)
        claimed = set()
        for word in show.get("capabilities") or []:
            mapped = _SHOW_CAPABILITY_MAP.get(word)
            if mapped:
                claimed.add(mapped)
        return claimed

    def discover(self) -> dict:
        """This engine's honest capability record (Y-008
        shape): what the server's own surface reports, mapped
        by the rules in the module docstring. Unreachable is a
        returned finding, never an exception."""
        record: dict = {
            "engine": ENGINE_NAME,
            "reachable": False,
            "capabilities": {
                "generate": False,
                "embed": False,
                "vision": False,
                "stream": False,
                # Ollama keeps no proof ledger to mirror.
                "provenance_grade": "unproven",
            },
        }
        try:
            tags = self._fetch_tags()
        except OllamaTransportError:
            return _validate(_ENGINE_VALIDATOR, record, "engine capability")
        record["reachable"] = True
        try:
            version = self._get_json("/api/version")
            if isinstance(version, dict) and isinstance(version.get("version"), str):
                record["version"] = version["version"]
        except OllamaTransportError:
            pass  # a server too old to name its version is still a server
        if tags:
            record["capabilities"]["generate"] = True
            # Streaming generate is the path generate() drives.
            record["capabilities"]["stream"] = True
            for entry in tags:
                claimed = self._model_capabilities(entry["name"])
                if "vision" in claimed:
                    record["capabilities"]["vision"] = True
                if "embed" in claimed:
                    record["capabilities"]["embed"] = True
        return _validate(_ENGINE_VALIDATOR, record, "engine capability")

    def capability_for_census(self) -> dict:
        """``discover()`` under the name the census collector's
        engines callable wants (Y-022's live-truth seam)."""
        return self.discover()

    # -- models -------------------------------------------------------------

    def list_models(self) -> list[str]:
        """The model names the server reports present, in the
        server's own order. Presence here is Ollama's word —
        hash-verified presence is the model store's (Y-026)."""
        return [entry["name"] for entry in self._fetch_tags()]

    def model_names_for_census(self) -> list[str]:
        """``list_models()`` for the census collector's
        models_present callable (Y-022's seam)."""
        return self.list_models()

    def model_entries(self) -> list[dict]:
        """The present models as validated registry entries
        (Y-008 shape), built only from fields Ollama publishes.
        A tag with no family cannot become an entry; its name
        is recorded in ``skipped_models`` instead of invented
        around. Per-model capabilities come from that model's
        own show surface alone (plus ``stream`` where
        ``generate`` is claimed, streaming being generate's
        mode here): an embedding model is never dressed as a
        generator, and a model whose show surface is silent
        claims nothing."""
        entries: list[dict] = []
        self.skipped_models = []
        for tag in self._fetch_tags():
            details = tag.get("details") or {}
            family = details.get("family")
            if not family:
                self.skipped_models.append(tag["name"])
                continue
            quant: dict = {"name": details.get("quantization_level") or "unknown"}
            if details.get("format"):
                quant["format"] = details["format"]
            size = tag.get("size")
            quant["size_gb"] = round(size / (1024**3), 4) if isinstance(size, int | float) else 0
            if quant["size_gb"] <= 0:
                self.skipped_models.append(tag["name"])
                continue
            claimed = self._model_capabilities(tag["name"])
            capabilities = set(claimed)
            if "generate" in claimed:
                capabilities.add("stream")
            entry: dict = {
                "name": tag["name"],
                "family": family,
                "quants": [quant],
                "capabilities": sorted(capabilities),
                "runnable_on": [],
            }
            if details.get("parameter_size"):
                entry["parameter_count"] = details["parameter_size"]
            entries.append(_validate(_MODEL_VALIDATOR, entry, "model entry"))
        return entries

    def _known_quant(self, model: str) -> str | None:
        if not self._tags:
            # Provenance wants the quant the tag reports; learn
            # the shelf on first need. A failed fetch leaves the
            # quant unknown (absent), and the task's own attempt
            # surfaces the real error in its own name.
            try:
                self._fetch_tags()
            except OllamaTransportError:
                pass
        for tag in self._tags:
            if tag["name"] == model:
                return (tag.get("details") or {}).get("quantization_level")
        return None

    # -- task running ---------------------------------------------------------

    def _stream_event(self, task_id: str, seq: int, event: str, data=None) -> dict:
        record: dict = {"task_id": task_id, "seq": seq, "event": event}
        if data is not None:
            record["data"] = data
        return _validate(_STREAM_VALIDATOR, record, "stream event")

    def _result(
        self,
        task: dict,
        node_id: str,
        *,
        status: str,
        run_ms: float,
        model: str,
        result: dict | None = None,
        error: dict | None = None,
        partial: bool = False,
    ) -> dict:
        provenance: dict = {"node_id": node_id, "engine": ENGINE_NAME, "model": model}
        quant = self._known_quant(model)
        if quant:
            provenance["quant"] = quant
        record: dict = {
            "task_id": task["id"],
            "status": status,
            "provenance": provenance,
            "timings_ms": {"queue": 0.0, "run": round(run_ms, 3), "total": round(run_ms, 3)},
        }
        if partial:
            record["partial"] = True
        if error is not None:
            record["error"] = error
        if result is not None:
            record["result"] = result
        return _validate(_RESULT_VALIDATOR, record, "result envelope")

    def generate(
        self, task: dict, node_id: str, *, stream: bool = True
    ) -> Iterator[tuple[dict | None, dict | None]]:
        """Run one generate task; yield (stream_event, None)
        pairs as the engine answers, then a final (None,
        result) pair carrying the terminal ResultEnvelope.

        With ``stream=False`` the engine is asked for the whole
        answer at once and the only event yielded is
        ``completed`` — the answer is never re-chunked into a
        fake stream (ADR-004). Failures raise
        OllamaAdapterError with a Y-007 ``error_code``;
        ``run_task()`` is the door that turns them into failed
        result envelopes instead.
        """
        _validate(_TASK_VALIDATOR, task, "task envelope")
        model = _task_model(task)
        if model is None:
            raise OllamaAdapterError(
                "a generate task names no model (requirements.model or payload.model)",
                error_code="model_not_present",
            )
        payload = task.get("payload") or {}
        prompt = payload.get("prompt")
        if not isinstance(prompt, str):
            raise OllamaAdapterError(
                "a generate task needs a payload.prompt string",
                error_code="malformed_envelope",
            )
        body: dict = {"model": model, "prompt": prompt, "stream": stream}
        if isinstance(payload.get("options"), dict):
            body["options"] = payload["options"]
        started = self._timer()
        if not stream:
            try:
                answer = self._post_json("/api/generate", body)
            except OllamaTransportError as exc:
                raise OllamaAdapterError(
                    str(exc), error_code=self._map_transport_error(exc, model=model)
                ) from exc
            if not isinstance(answer, dict) or not isinstance(answer.get("response"), str):
                raise OllamaAdapterError(
                    "/api/generate answered without a response string",
                    error_code="engine_error",
                )
            run_ms = (self._timer() - started) * 1000.0
            result = self._result(
                task,
                node_id,
                status="completed",
                run_ms=run_ms,
                model=model,
                result=self._generate_result(answer),
            )
            yield self._stream_event(task["id"], 0, "completed"), None
            yield None, result
            return

        yield self._stream_event(task["id"], 0, "started"), None
        seq = 1
        chunks: list[str] = []
        final: dict = {}
        try:
            lines = self._post_lines("/api/generate", body)
            for line in lines:
                if not line or not line.strip():
                    continue
                try:
                    piece = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise OllamaAdapterError(
                        f"/api/generate streamed a malformed NDJSON line: {line[:80]!r}",
                        error_code="engine_error",
                    ) from exc
                if not isinstance(piece, dict) or not isinstance(piece.get("response"), str):
                    raise OllamaAdapterError(
                        "/api/generate streamed a chunk without a response string",
                        error_code="engine_error",
                    )
                chunks.append(piece["response"])
                if piece["response"]:
                    yield self._stream_event(
                        task["id"], seq, "token", {"text": piece["response"]}
                    ), None
                    seq += 1
                if piece.get("done"):
                    final = piece
                    break
        except OllamaTransportError as exc:
            raise OllamaAdapterError(
                str(exc), error_code=self._map_transport_error(exc, model=model)
            ) from exc
        run_ms = (self._timer() - started) * 1000.0
        answer = dict(final)
        answer["response"] = "".join(chunks)
        result = self._result(
            task,
            node_id,
            status="completed",
            run_ms=run_ms,
            model=model,
            result=self._generate_result(answer),
        )
        yield self._stream_event(task["id"], seq, "completed"), None
        yield None, result

    @staticmethod
    def _generate_result(answer: dict) -> dict:
        result: dict = {"text": answer["response"], "engine": ENGINE_NAME}
        if isinstance(answer.get("done_reason"), str):
            result["done_reason"] = answer["done_reason"]
        if isinstance(answer.get("eval_count"), int):
            result["eval_count"] = answer["eval_count"]
        return result

    def embed(self, task: dict, node_id: str) -> dict:
        """Run one embedding task through ``/api/embed`` and
        return its terminal ResultEnvelope (embeddings carry
        no token stream; the stream shape for an embed task is
        the caller's accepted/completed pair, as in the
        simulator)."""
        _validate(_TASK_VALIDATOR, task, "task envelope")
        model = _task_model(task)
        if model is None:
            raise OllamaAdapterError(
                "an embed task names no model (requirements.model or payload.model)",
                error_code="model_not_present",
            )
        payload = task.get("payload") or {}
        texts = payload.get("texts")
        if not isinstance(texts, list) or not texts:
            single = payload.get("text")
            texts = [single] if isinstance(single, str) else []
        if not texts:
            raise OllamaAdapterError(
                "an embed task needs payload.texts (or payload.text)",
                error_code="malformed_envelope",
            )
        started = self._timer()
        try:
            answer = self._post_json("/api/embed", {"model": model, "input": list(texts)})
        except OllamaTransportError as exc:
            raise OllamaAdapterError(
                str(exc), error_code=self._map_transport_error(exc, model=model)
            ) from exc
        vectors = answer.get("embeddings") if isinstance(answer, dict) else None
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise OllamaAdapterError(
                "/api/embed answered without one embedding per input",
                error_code="engine_error",
            )
        run_ms = (self._timer() - started) * 1000.0
        return self._result(
            task,
            node_id,
            status="completed",
            run_ms=run_ms,
            model=model,
            result={
                "embeddings": vectors,
                "dim": len(vectors[0]) if vectors and isinstance(vectors[0], list) else 0,
                "engine": ENGINE_NAME,
            },
        )

    def run_task(self, task: dict, node_id: str) -> tuple[dict, list[dict]]:
        """The collecting front door: one task in, (ResultEnvelope,
        StreamEvents) out — the simulator's return shape, from a
        real engine's answers. Adapter failures become failed
        (or, for a kind this engine cannot serve, refused)
        envelopes with Y-007 codes; a stream that dies after
        chunks were delivered reports ``partial: True`` with the
        text the caller already holds."""
        _validate(_TASK_VALIDATOR, task, "task envelope")
        kind = task.get("kind", "")
        model = _task_model(task) or "unknown"
        if kind.startswith("embed"):
            events = [self._stream_event(task["id"], 0, "accepted")]
            try:
                result = self.embed(task, node_id)
            except OllamaAdapterError as exc:
                result = self._failure_result(task, node_id, exc, model=model, run_ms=0.0)
                events.append(self._stream_event(task["id"], 1, "failed"))
                return result, events
            events.append(self._stream_event(task["id"], 1, "completed"))
            return result, events
        if not kind.startswith(("generate", "chat")):
            result = self._result(
                task,
                node_id,
                status="refused",
                run_ms=0.0,
                model=model,
                error={
                    "code": "no_capable_node",
                    "detail": f"the ollama adapter serves generate/embed tasks, not {kind!r}",
                },
                result={"engine": ENGINE_NAME},
            )
            return result, [self._stream_event(task["id"], 0, "failed")]

        events: list[dict] = []
        streamed_text: list[str] = []
        started = self._timer()
        try:
            for event, result in self.generate(task, node_id):
                if event is not None:
                    events.append(event)
                    if event["event"] == "token":
                        streamed_text.append(event["data"]["text"])
                if result is not None:
                    return result, events
        except OllamaAdapterError as exc:
            run_ms = (self._timer() - started) * 1000.0
            partial = bool(streamed_text)
            result = self._failure_result(
                task,
                node_id,
                exc,
                model=model,
                run_ms=run_ms,
                partial=partial,
                result=(
                    {"text": "".join(streamed_text), "engine": ENGINE_NAME} if partial else None
                ),
            )
            events.append(self._stream_event(task["id"], len(events), "failed"))
            return result, events
        raise OllamaAdapterError(  # pragma: no cover - generate always ends with a result
            "generate ended without a result", error_code="internal"
        )

    def _failure_result(
        self,
        task: dict,
        node_id: str,
        exc: OllamaAdapterError,
        *,
        model: str,
        run_ms: float,
        partial: bool = False,
        result: dict | None = None,
    ) -> dict:
        return self._result(
            task,
            node_id,
            status="failed",
            run_ms=run_ms,
            model=model,
            error={"code": exc.error_code or "engine_error", "detail": str(exc)},
            partial=partial,
            result=result,
        )
