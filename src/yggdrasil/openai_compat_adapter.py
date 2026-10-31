"""Engine adapter III — llama.cpp server + generic
OpenAI-compatible local endpoints — Y-025.

The last two citizens of ADR-004's order of regard, wrapped
behind the node contract (engines are wrapped, never
re-implemented):

3. **llama.cpp server** (``llama-server``) — the bare-metal
   fallback, engine name ``llamacpp``, default base
   ``http://127.0.0.1:8080``. It speaks the OpenAI-compatible
   shape below plus its own ``GET /health``.
4. **Generic OpenAI-compatible local endpoints** — anything
   on the LAN that speaks the shape (a vLLM, an LM Studio, a
   neighbour's server), engine name ``openai-compatible``,
   labelled as what it is and never upgraded to a named
   engine it is not.

Both flavours are one adapter over the shape they share:

- ``GET /v1/models`` — ``data[]`` entries carrying at least
  an ``id``. This list is discovery's evidence and the model
  list's whole source.
- ``POST /v1/chat/completions`` — one answer as
  ``choices[0].message.content`` (or a completions-shaped
  ``choices[0].text``), or, with ``stream: true``, an SSE
  stream: ``data:`` lines carrying delta chunks, closed by
  ``data: [DONE]``.
- ``POST /v1/embeddings`` — ``data[]`` of
  ``{index, embedding}``, one vector per input.

Honest capability mapping (ADR-004's per-engine rules):

- ``generate`` is claimed only when the server is reachable
  AND at least one model is listed — a reachable server with
  an empty list cannot generate anything on this node yet.
- ``stream`` follows ``generate``: SSE streaming is the
  shape's own streaming path and the one ``generate()``
  actually drives. A stream that ends without its
  ``[DONE]`` terminator is a failure, never a success —
  and a non-streamed answer is never re-chunked into a
  fake stream (with ``stream=False`` the only event is
  ``completed``).
- ``embed`` / ``vision`` are **never inferred**. The
  ``/v1/models`` shape publishes no embedding or vision
  evidence — llama.cpp serves embeddings only under its
  ``--embedding`` flag and vision only with a multimodal
  projector loaded, and a generic endpoint's flags are its
  own — and a model *name* that sounds visual or
  embed-shaped proves nothing (Y-023's rule). These
  capabilities are claimed only for models the **operator
  declares** at the seam (``embedding_models=`` /
  ``vision_models=`` constructor arguments — deployment
  wiring, like the transport itself: the operator who
  started the server knows its flags), and only while a
  declared model is actually listed. The default declares
  nothing and claims nothing. An embedding model declared
  so is never dressed as a generator in its registry entry.
- ``provenance_grade`` stays ``unproven`` whatever discovery
  finds. Neither flavour keeps a proof ledger to mirror,
  and ADR-004 forbids laundering a grade upward.
- **Version** stays absent: the shape publishes no server
  version, and none is guessed. (llama.cpp's ``/health``
  answer is carried as ``health_status()`` / ``last_health``
  — advisory liveness colour, never a capability claim.)

Registry entries (Y-008) are built only from fields a server
actually publishes — the same rule as Y-023 and Y-024: an
entry needs a family and a quant (name, format, byte size)
in the answer, and an id that can be a registry name at all.
The plain shape publishes ids alone, so in practice most
listed models land in ``skipped_models`` by name while
``list_models()`` still reports them present: presence and
registry-worthiness are different claims.

The transport seam: this module carries no network client.
The endpoint is spoken through an injected transport —
``get_json`` / ``post_json`` / ``post_lines`` returning
parsed JSON or raw response lines (the SSE lines, verbatim)
and raising this module's ``OpenAICompatUnreachable`` /
``OpenAICompatHttpError`` — the same injectable boundary as
Y-023/Y-024, and the reason the Part I gate's
zero-network-imports check still passes. An API key, where a
generic endpoint demands one, lives in the transport, never
in the adapter. No test in this slice claims a live server
was spoken to — the suite's fake transports speak the wire
shapes, and on-hardware proof belongs to the citizen slices
(Y-033 – Y-035) on their own devices.

Errors, in the Y-007 taxonomy: no answer at all is
``engine_unreachable``; a refused key (HTTP 401/403) is
``scope_denied``; HTTP 404 while asking for one model is
``model_not_present``; HTTP 429 is ``queue_full`` (the
endpoint's own backpressure, in the mesh's word); any other
HTTP failure, a malformed SSE line, an answer missing the
fields its route owes, or a stream ending without
``[DONE]`` is ``engine_error``. A stream that dies after
chunks were delivered reports ``partial: True`` with the
text the caller already holds.
"""

import json
import re
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

# A model id can become a registry entry only if it can be a
# registry name at all (the Y-008 name pattern).
_ENTRY_NAME = re.compile(r"^[a-z0-9][a-z0-9./:_-]*$")

# The chat route's accepted sampling fields, in the shape's
# own vocabulary. A task's options are mapped through this
# whitelist only: forwarding an unlisted key would manufacture
# an engine_error out of the adapter's own carelessness (some
# servers reject unknown fields outright).
_CHAT_OPTION_FIELDS = {
    "temperature",
    "top_p",
    "max_tokens",
    "seed",
    "stop",
    "frequency_penalty",
    "presence_penalty",
}


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


class OpenAICompatAdapterError(Exception):
    """Any adapter failure. ``error_code`` names it in the
    Y-007 taxonomy when the failure is a task failure."""

    def __init__(self, message: str, *, error_code: str | None = None):
        super().__init__(message)
        self.error_code = error_code


class OpenAICompatTransportError(OpenAICompatAdapterError):
    """The transport seam's failure base — a real transport
    raises these; the adapter maps them, never sees sockets."""


class OpenAICompatUnreachable(OpenAICompatTransportError):
    """No answer at all: connection refused, timed out, gone."""


class OpenAICompatHttpError(OpenAICompatTransportError):
    """The server answered with a non-success HTTP status."""

    def __init__(self, status: int, detail: str = ""):
        super().__init__(
            f"openai-compatible endpoint answered HTTP {status}"
            + (f": {detail}" if detail else "")
        )
        self.status = status
        self.detail = detail


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise OpenAICompatAdapterError(f"{kind} fails its schema: {errors[0].message}")
    return record


def _task_model(task: dict) -> str | None:
    requirements = task.get("requirements") or {}
    if requirements.get("model"):
        return requirements["model"]
    payload = task.get("payload") or {}
    return payload.get("model") if isinstance(payload.get("model"), str) else None


class OpenAICompatAdapter:
    """One node's OpenAI-compatible endpoint, wrapped. The two
    concrete flavours below fix the engine name and default
    base URL; everything else — the wire shape, the honesty
    rules, the error names — is shared, because the shape is
    shared. ``transport`` is the seam described in the module
    docstring (it also owns any API key); ``embedding_models``
    / ``vision_models`` are the operator's declarations (see
    the docstring — default: none, and nothing is claimed);
    ``timer`` (seconds, monotonic) is injectable so timings
    are deterministic in tests and honestly measured in life.
    """

    ENGINE = "openai-compatible"
    DEFAULT_BASE_URL = "http://127.0.0.1:8000"
    is_simulated = False

    def __init__(
        self,
        transport,
        *,
        base_url: str | None = None,
        timer=None,
        embedding_models: Iterable[str] = (),
        vision_models: Iterable[str] = (),
    ):
        self._transport = transport
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._timer = timer or time.perf_counter
        self._embedding_models = frozenset(embedding_models)
        self._vision_models = frozenset(vision_models)
        self.engine = self.ENGINE
        self._model_items: list[dict] = []  # the last /v1/models answer, verbatim
        self.skipped_models: list[str] = []

    # -- transport, mapped ------------------------------------------------

    def _get_json(self, path: str) -> dict:
        return self._transport.get_json(path)

    def _post_json(self, path: str, body: dict) -> dict:
        return self._transport.post_json(path, body)

    def _post_lines(self, path: str, body: dict) -> Iterable[str]:
        return self._transport.post_lines(path, body)

    @staticmethod
    def _map_transport_error(exc: OpenAICompatTransportError, *, model: str | None = None) -> str:
        """A transport failure's Y-007 name: unreachable is its
        own code; a refused key is scope_denied; a 404 while
        asking for one model is that model not present; the
        endpoint's 429 backpressure is the mesh's queue_full;
        anything else is the engine's error to own."""
        if isinstance(exc, OpenAICompatUnreachable):
            return "engine_unreachable"
        if isinstance(exc, OpenAICompatHttpError):
            if exc.status in (401, 403):
                return "scope_denied"
            if exc.status == 404 and model is not None:
                return "model_not_present"
            if exc.status == 429:
                return "queue_full"
        return "engine_error"

    # -- discovery ----------------------------------------------------------

    def _fetch_models(self) -> list[dict]:
        answer = self._get_json("/v1/models")
        data = answer.get("data") if isinstance(answer, dict) else None
        if not isinstance(data, list):
            raise OpenAICompatAdapterError(
                "/v1/models answered without a data list", error_code="engine_error"
            )
        self._model_items = [
            entry for entry in data if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        ]
        return self._model_items

    def discover(self) -> dict:
        """This endpoint's honest capability record (Y-008
        shape), by the rules in the module docstring.
        Unreachable is a returned finding, never an exception."""
        record: dict = {
            "engine": self.ENGINE,
            "reachable": False,
            "capabilities": {
                "generate": False,
                "embed": False,
                "vision": False,
                "stream": False,
                # Neither flavour keeps a proof ledger to mirror.
                "provenance_grade": "unproven",
            },
        }
        try:
            items = self._fetch_models()
        except OpenAICompatTransportError:
            return _validate(_ENGINE_VALIDATOR, record, "engine capability")
        record["reachable"] = True
        if items:
            record["capabilities"]["generate"] = True
            # SSE streaming is the path generate() drives.
            record["capabilities"]["stream"] = True
            names = {entry["id"] for entry in items}
            # Declared capabilities rise only while a declared
            # model is actually listed — a declaration about an
            # absent model claims nothing.
            if names & self._embedding_models:
                record["capabilities"]["embed"] = True
            if names & self._vision_models:
                record["capabilities"]["vision"] = True
        # No version: the shape publishes none, and none is guessed.
        return _validate(_ENGINE_VALIDATOR, record, "engine capability")

    def capability_for_census(self) -> dict:
        """``discover()`` under the name the census collector's
        engines callable wants (Y-022's live-truth seam)."""
        return self.discover()

    # -- models -------------------------------------------------------------

    def list_models(self) -> list[str]:
        """The model ids the endpoint reports present, in its
        own order. Presence here is the endpoint's word —
        hash-verified presence is the model store's (Y-026)."""
        return [entry["id"] for entry in self._fetch_models()]

    def model_names_for_census(self) -> list[str]:
        """``list_models()`` for the census collector's
        models_present callable (Y-022's seam)."""
        return self.list_models()

    def _capabilities_for(self, name: str) -> list[str]:
        """One model's registry capabilities, from the
        operator's declarations alone: a declared embedding
        model claims embed and is never dressed as a generator;
        any other listed model claims generate + stream, plus
        vision when it is declared a vision model."""
        if name in self._embedding_models:
            return ["embed"]
        capabilities = ["generate", "stream"]
        if name in self._vision_models:
            capabilities.append("vision")
        return sorted(capabilities)

    def model_entries(self) -> list[dict]:
        """The listed models as validated registry entries
        (Y-008 shape), built only from fields the endpoint
        publishes. An entry needs a family and a quant (name,
        format, size) in the answer and an id that can be a
        registry name; the plain shape publishes ids alone, so
        most models are skipped by name in ``skipped_models``
        instead of invented around — while ``list_models()``
        still reports them present."""
        entries: list[dict] = []
        self.skipped_models = []
        for item in self._fetch_models():
            name = item["id"]
            family = item.get("family")
            quant = item.get("quant")
            if (
                not _ENTRY_NAME.match(name)
                or not isinstance(family, str)
                or not family
                or not isinstance(quant, dict)
                or not isinstance(quant.get("name"), str)
                or not isinstance(quant.get("format"), str)
                or not isinstance(quant.get("size_gb"), int | float)
                or quant["size_gb"] <= 0
            ):
                self.skipped_models.append(name)
                continue
            entry: dict = {
                "name": name,
                "family": family,
                "quants": [
                    {
                        "name": quant["name"],
                        "format": quant["format"],
                        "size_gb": quant["size_gb"],
                    }
                ],
                "capabilities": self._capabilities_for(name),
                "runnable_on": [],
            }
            if isinstance(item.get("parameter_count"), str):
                entry["parameter_count"] = item["parameter_count"]
            context = item.get("context_tokens")
            if not isinstance(context, int):
                meta = item.get("meta")
                context = meta.get("n_ctx_train") if isinstance(meta, dict) else None
            if isinstance(context, int) and context >= 1:
                entry["context_tokens"] = context
            entries.append(_validate(_MODEL_VALIDATOR, entry, "model entry"))
        return entries

    def _known_quant(self, model: str) -> str | None:
        if not self._model_items:
            # Provenance wants the quant the server reports;
            # learn the shelf on first need. A failed fetch
            # leaves the quant unknown (absent), and the task's
            # own attempt surfaces the real error in its name.
            try:
                self._fetch_models()
            except OpenAICompatTransportError:
                pass
        for item in self._model_items:
            if item["id"] == model:
                quant = item.get("quant")
                if isinstance(quant, dict) and isinstance(quant.get("name"), str):
                    return quant["name"]
                if isinstance(item.get("quantization"), str):
                    return item["quantization"]
                return None
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
        provenance: dict = {"node_id": node_id, "engine": self.ENGINE, "model": model}
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

    def _messages_for(self, task: dict, model: str) -> list:
        """The chat route's messages, from the task's own
        shapes: ``payload.messages`` (a list of role/content
        dicts) travels verbatim; otherwise ``payload.prompt``
        becomes the user turn, with ``payload.system`` (or a
        ``system`` option) as the system turn before it, and
        ``payload.images`` — on a declared vision model only —
        as image parts of that user turn."""
        payload = task.get("payload") or {}
        images = payload.get("images")
        messages = payload.get("messages")
        if messages is not None:
            if images:
                raise OpenAICompatAdapterError(
                    "payload.images goes with payload.prompt, not payload.messages",
                    error_code="malformed_envelope",
                )
            if (
                not isinstance(messages, list)
                or not messages
                or any(
                    not isinstance(message, dict)
                    or not isinstance(message.get("role"), str)
                    or "content" not in message
                    for message in messages
                )
            ):
                raise OpenAICompatAdapterError(
                    "payload.messages must be a non-empty list of role/content dicts",
                    error_code="malformed_envelope",
                )
            return messages
        prompt = payload.get("prompt")
        if not isinstance(prompt, str):
            raise OpenAICompatAdapterError(
                "a generate task needs a payload.prompt string (or payload.messages)",
                error_code="malformed_envelope",
            )
        built: list = []
        options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
        system = payload.get("system") if isinstance(payload.get("system"), str) else None
        if system is None and isinstance(options.get("system"), str):
            system = options["system"]
        if system is not None:
            built.append({"role": "system", "content": system})
        if images:
            if model not in self._vision_models:
                raise OpenAICompatAdapterError(
                    f"model {model!r} is not declared a vision model for this "
                    "endpoint; image input is not claimed (see docs/OPENAI_COMPAT_ADAPTER.md)",
                    error_code="no_capable_node",
                )
            if not isinstance(images, list) or not all(
                isinstance(image, str) for image in images
            ):
                raise OpenAICompatAdapterError(
                    "payload.images must be a list of strings (URLs or data URIs)",
                    error_code="malformed_envelope",
                )
            content: list = [{"type": "text", "text": prompt}]
            content += [
                {"type": "image_url", "image_url": {"url": image}} for image in images
            ]
            built.append({"role": "user", "content": content})
        else:
            built.append({"role": "user", "content": prompt})
        return built

    @staticmethod
    def _choice_text(choice: dict) -> str | None:
        """One choice's text, in whichever of the shape's two
        wordings arrived: a chat choice's message content, or
        a completions choice's text."""
        message = choice.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
        if isinstance(choice.get("text"), str):
            return choice["text"]
        return None

    @staticmethod
    def _chat_result(answer: dict, engine: str, choice: dict) -> dict:
        result: dict = {"text": OpenAICompatAdapter._choice_text(choice), "engine": engine}
        if isinstance(choice.get("finish_reason"), str):
            result["finish_reason"] = choice["finish_reason"]
        usage = answer.get("usage")
        if isinstance(usage, dict):
            for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if isinstance(usage.get(field), int):
                    result[field] = usage[field]
        return result

    def generate(
        self, task: dict, node_id: str, *, stream: bool = True
    ) -> Iterator[tuple[dict | None, dict | None]]:
        """Run one generate/chat task through
        ``/v1/chat/completions``; yield (stream_event, None)
        pairs as the endpoint answers, then a final (None,
        result) pair carrying the terminal ResultEnvelope.

        With ``stream=True`` the endpoint's SSE lines are the
        stream — ``data:`` chunks become token events in wire
        order, and the stream must close with ``data: [DONE]``:
        a stream ending without its terminator raises
        ``engine_error`` (incomplete is not success). With
        ``stream=False`` the only event yielded is
        ``completed`` — a finished answer is never re-chunked
        into a fake stream (ADR-004). Failures raise
        OpenAICompatAdapterError with a Y-007 ``error_code``;
        ``run_task()`` is the door that turns them into result
        envelopes instead.
        """
        _validate(_TASK_VALIDATOR, task, "task envelope")
        model = _task_model(task)
        if model is None:
            raise OpenAICompatAdapterError(
                "a generate task names no model (requirements.model or payload.model)",
                error_code="model_not_present",
            )
        messages = self._messages_for(task, model)
        payload = task.get("payload") or {}
        body: dict = {"model": model, "messages": messages, "stream": stream}
        options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
        for field in _CHAT_OPTION_FIELDS:
            if field in options:
                body[field] = options[field]
        started = self._timer()
        if not stream:
            try:
                answer = self._post_json("/v1/chat/completions", body)
            except OpenAICompatTransportError as exc:
                raise OpenAICompatAdapterError(
                    str(exc), error_code=self._map_transport_error(exc, model=model)
                ) from exc
            choices = answer.get("choices") if isinstance(answer, dict) else None
            if (
                not isinstance(choices, list)
                or not choices
                or not isinstance(choices[0], dict)
                or self._choice_text(choices[0]) is None
            ):
                raise OpenAICompatAdapterError(
                    "/v1/chat/completions answered without a choice carrying text",
                    error_code="engine_error",
                )
            run_ms = (self._timer() - started) * 1000.0
            result = self._result(
                task,
                node_id,
                status="completed",
                run_ms=run_ms,
                model=model,
                result=self._chat_result(answer, self.ENGINE, choices[0]),
            )
            yield self._stream_event(task["id"], 0, "completed"), None
            yield None, result
            return

        yield self._stream_event(task["id"], 0, "started"), None
        seq = 1
        chunks: list[str] = []
        final_choice: dict = {}
        final_answer: dict = {}
        saw_done = False
        try:
            lines = self._post_lines("/v1/chat/completions", body)
            for line in lines:
                if not line or not line.strip():
                    continue
                text_line = line.strip()
                if text_line.startswith(":"):
                    continue  # an SSE comment / keep-alive line, not data
                if text_line.startswith("data:"):
                    text_line = text_line[len("data:") :].strip()
                if text_line == "[DONE]":
                    saw_done = True
                    break
                try:
                    piece = json.loads(text_line)
                except json.JSONDecodeError as exc:
                    raise OpenAICompatAdapterError(
                        f"/v1/chat/completions streamed a malformed SSE line: {line[:80]!r}",
                        error_code="engine_error",
                    ) from exc
                if not isinstance(piece, dict):
                    raise OpenAICompatAdapterError(
                        "/v1/chat/completions streamed a non-object chunk",
                        error_code="engine_error",
                    )
                if piece.get("error"):
                    raise OpenAICompatAdapterError(
                        f"/v1/chat/completions streamed an error chunk: {piece['error']!r}",
                        error_code="engine_error",
                    )
                choices = piece.get("choices")
                if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                    # e.g. a usage-only closing chunk: keep its
                    # usage for the result, but it carries no text.
                    if isinstance(piece.get("usage"), dict):
                        final_answer = {**final_answer, "usage": piece["usage"]}
                    continue
                choice = choices[0]
                final_answer = piece
                final_choice = choice
                delta = choice.get("delta")
                piece_text: str | None = None
                if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                    piece_text = delta["content"]
                elif isinstance(choice.get("text"), str):
                    piece_text = choice["text"]
                if piece_text:
                    chunks.append(piece_text)
                    yield self._stream_event(
                        task["id"], seq, "token", {"text": piece_text}
                    ), None
                    seq += 1
        except OpenAICompatTransportError as exc:
            raise OpenAICompatAdapterError(
                str(exc), error_code=self._map_transport_error(exc, model=model)
            ) from exc
        if not saw_done:
            # The shape's own terminator never arrived: the
            # answer is incomplete, however much text came.
            raise OpenAICompatAdapterError(
                "/v1/chat/completions stream ended without its [DONE] terminator — incomplete",
                error_code="engine_error",
            )
        run_ms = (self._timer() - started) * 1000.0
        answer = dict(final_answer)
        choice = dict(final_choice)
        if isinstance(choice.get("delta"), dict):
            choice["message"] = {"content": "".join(chunks)}
        else:
            choice["text"] = "".join(chunks)
        result = self._result(
            task,
            node_id,
            status="completed",
            run_ms=run_ms,
            model=model,
            result=self._chat_result(answer, self.ENGINE, choice),
        )
        yield self._stream_event(task["id"], seq, "completed"), None
        yield None, result

    def embed(self, task: dict, node_id: str) -> dict:
        """Run one embedding task through ``/v1/embeddings``
        and return its terminal ResultEnvelope. The model must
        be one the operator declared an embedding model for
        this endpoint (the module docstring's rule): an
        undeclared model is refused by name before any request
        is made, because the shape itself offers no evidence
        the route serves this model. Vectors are ordered by
        the answer's own ``index`` fields, never by arrival
        order."""
        _validate(_TASK_VALIDATOR, task, "task envelope")
        model = _task_model(task)
        if model is None:
            raise OpenAICompatAdapterError(
                "an embed task names no model (requirements.model or payload.model)",
                error_code="model_not_present",
            )
        if model not in self._embedding_models:
            raise OpenAICompatAdapterError(
                f"model {model!r} is not declared an embedding model for this "
                "endpoint; the embeddings route is not claimed for it",
                error_code="no_capable_node",
            )
        payload = task.get("payload") or {}
        texts = payload.get("texts")
        if not isinstance(texts, list) or not texts:
            single = payload.get("text")
            texts = [single] if isinstance(single, str) else []
        if not texts:
            raise OpenAICompatAdapterError(
                "an embed task needs payload.texts (or payload.text)",
                error_code="malformed_envelope",
            )
        started = self._timer()
        try:
            answer = self._post_json("/v1/embeddings", {"model": model, "input": list(texts)})
        except OpenAICompatTransportError as exc:
            raise OpenAICompatAdapterError(
                str(exc), error_code=self._map_transport_error(exc, model=model)
            ) from exc
        data = answer.get("data") if isinstance(answer, dict) else None
        if not isinstance(data, list) or len(data) != len(texts):
            raise OpenAICompatAdapterError(
                "/v1/embeddings answered without one embedding per input",
                error_code="engine_error",
            )
        ordered = sorted(
            data,
            key=lambda item: item.get("index", 0) if isinstance(item, dict) else 0,
        )
        vectors = [
            item.get("embedding") if isinstance(item, dict) else None for item in ordered
        ]
        if any(not isinstance(vector, list) for vector in vectors):
            raise OpenAICompatAdapterError(
                "/v1/embeddings answered with a non-list embedding",
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
                "dim": len(vectors[0]) if vectors else 0,
                "engine": self.ENGINE,
            },
        )

    def run_task(self, task: dict, node_id: str) -> tuple[dict, list[dict]]:
        """The collecting front door: one task in, (ResultEnvelope,
        StreamEvents) out — the simulator's return shape, from a
        real endpoint's answers. Embed tasks run only for
        declared embedding models and vision tasks only for
        declared vision models; both are refused by name when
        undeclared, because an unclaimed capability served
        anyway would be a lie the census never told. Adapter
        failures become failed envelopes with Y-007 codes; a
        stream that dies after chunks were delivered reports
        ``partial: True`` with the text the caller holds."""
        _validate(_TASK_VALIDATOR, task, "task envelope")
        kind = task.get("kind", "")
        # Kinds are dotted names (``chat.generate``, ``embed.text``,
        # ``vision.describe``): the capability a kind asks for is
        # one of its segments, at either end (Y-024's reading).
        segments = kind.split(".")
        model = _task_model(task) or "unknown"
        if "embed" in segments:
            events = [self._stream_event(task["id"], 0, "accepted")]
            try:
                result = self.embed(task, node_id)
            except OpenAICompatAdapterError as exc:
                if exc.error_code == "no_capable_node":
                    result = self._refused(task, node_id, model, str(exc))
                    return result, [self._stream_event(task["id"], 0, "failed")]
                result = self._failure_result(task, node_id, exc, model=model, run_ms=0.0)
                events.append(self._stream_event(task["id"], 1, "failed"))
                return result, events
            events.append(self._stream_event(task["id"], 1, "completed"))
            return result, events
        if "vision" in segments and model not in self._vision_models:
            return self._refused(
                task,
                node_id,
                model,
                f"model {model!r} is not declared a vision model for this "
                "endpoint; vision is not claimed (a name is never the evidence)",
            ), [self._stream_event(task["id"], 0, "failed")]
        if not set(segments) & {"generate", "chat", "vision"}:
            return self._refused(
                task,
                node_id,
                model,
                f"the {self.ENGINE} adapter serves generate/embed tasks, not {kind!r}",
            ), [self._stream_event(task["id"], 0, "failed")]

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
        except OpenAICompatAdapterError as exc:
            run_ms = (self._timer() - started) * 1000.0
            partial = bool(streamed_text)
            if exc.error_code == "no_capable_node":
                result = self._refused(task, node_id, model, str(exc))
            else:
                result = self._failure_result(
                    task,
                    node_id,
                    exc,
                    model=model,
                    run_ms=run_ms,
                    partial=partial,
                    result=(
                        {"text": "".join(streamed_text), "engine": self.ENGINE}
                        if partial
                        else None
                    ),
                )
            events.append(self._stream_event(task["id"], len(events), "failed"))
            return result, events
        raise OpenAICompatAdapterError(  # pragma: no cover - generate always ends with a result
            "generate ended without a result", error_code="internal"
        )

    def _refused(self, task: dict, node_id: str, model: str, detail: str) -> dict:
        return self._result(
            task,
            node_id,
            status="refused",
            run_ms=0.0,
            model=model,
            error={"code": "no_capable_node", "detail": detail},
            result={"engine": self.ENGINE},
        )

    def _failure_result(
        self,
        task: dict,
        node_id: str,
        exc: OpenAICompatAdapterError,
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
            model=model if isinstance(model, str) and model else "unknown",
            error={"code": exc.error_code or "engine_error", "detail": str(exc)},
            partial=partial,
            result=result,
        )


class LlamaCppAdapter(OpenAICompatAdapter):
    """llama.cpp's ``llama-server``: the OpenAI-compatible
    shape plus its own ``GET /health``. The health answer is
    advisory colour (``health_status()`` / ``last_health``) —
    discovery's evidence stays the ``/v1/models`` surface, so
    a server still loading its model (health answers 503
    then, in its own shape) is reported by health_status as
    exactly that, never dressed as a capability."""

    ENGINE = "llamacpp"
    DEFAULT_BASE_URL = "http://127.0.0.1:8080"

    def __init__(self, transport, **kwargs):
        super().__init__(transport, **kwargs)
        self.last_health: str | None = None

    def health_status(self) -> str | None:
        """The ``/health`` answer's status word (``"ok"`` on a
        loaded server), or None when health could not be read
        — including the 503 a loading server answers with, in
        which case the HTTP status is kept in
        ``last_health_status``. Absence is reported as absence."""
        self.last_health_status: int | None = None
        try:
            answer = self._get_json("/health")
        except OpenAICompatHttpError as exc:
            self.last_health_status = exc.status
            self.last_health = None
            return None
        except OpenAICompatTransportError:
            self.last_health = None
            return None
        status = answer.get("status") if isinstance(answer, dict) else None
        self.last_health = status if isinstance(status, str) else None
        return self.last_health

    def discover(self) -> dict:
        """The shared discovery, with the health word read
        alongside: health colours ``last_health`` but never
        gates the capability record — the models surface is
        the evidence, as on every adapter in this family."""
        self.health_status()
        return super().discover()


class OpenAICompatibleAdapter(OpenAICompatAdapter):
    """A generic OpenAI-compatible local endpoint: the shared
    shape and nothing else assumed — no native health route
    is called, no vendor is inferred from an answer, and the
    engine is recorded as ``openai-compatible``, the label
    ADR-004 gives anything on the LAN that speaks the shape."""

    ENGINE = "openai-compatible"
    DEFAULT_BASE_URL = "http://127.0.0.1:8000"
