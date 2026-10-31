"""Engine adapter II — Project Aesir — Y-024.

The house's own engine, first in ADR-004's order of regard,
wrapped behind the node contract. The adapter speaks Aesir's
**native** service API as it stands (Aesir's own
``docs/API_SUPPORT_MATRIX_V1.md``, protocol v1) — not its
Ollama-shaped compatibility mode, which is Y-023's surface
spoken by another server:

- ``GET /health/live`` — schema-v1 process liveness; it makes
  no model claim, and this adapter treats it as exactly that.
- ``GET /health/ready`` — schema-v1 readiness: ``ready``,
  ``status``, and the **one resident model's** identity
  (``model``, ``digest``, ``backend``, ``profile``,
  ``context``). HTTP 200 when the resident session is healthy,
  503 with ``status: "not_ready"`` otherwise. Aesir serves one
  resident model at a time; only that model may generate.
- ``GET /v1/models`` — the catalog as ``data[]`` ids.
  Visibility is not runnability: the matrix states plainly
  that a listed model is not thereby loaded or runnable.
- ``POST /v1/generate`` — the native generation route.
  ``stream: false`` returns one JSON answer (``text``,
  ``finish_reason``, token counts, ``backend``,
  ``cpu_offload``, model identity, service-phase
  ``timings_ms``); ``stream: true`` returns incremental NDJSON
  text records (``{"text": ..., "done": false}``) followed by
  exactly one terminal record (``done: true`` with counts,
  ``model_digest`` and timings). A stream that ends **without**
  its terminal record is incomplete, never a success — the
  matrix's own rule, enforced here.

The capability ledger feeds the census (the slice's law):
Aesir keeps a canonical capability ledger
(``CAPABILITY_LEDGER.md``) in which every capability carries
exactly one status — ``verified``, ``partial``, ``scaffold``,
``simulated``, ``missing``, or worse. Native generation and
its streaming path are governed by one entry, **AES-SRV-010**
("Authenticated native loopback CUDA generation", status
``verified`` as this adapter is written). The adapter is
constructed with a ledger snapshot (``parse_ledger()`` reads
the ledger's own markdown shape) and the rules are:

- a capability is claimed only when its governing ledger
  entry is ``verified`` or ``partial`` — the mesh never
  promises what Aesir itself marks scaffold, simulated,
  missing, blocked, withdrawn, or historical;
- ``provenance_grade`` mirrors the governing entry and is
  never laundered upward: ``verified`` → ``proven``, ``partial``
  → ``partial``, anything else (or no entry) → ``unproven``,
  and every result's provenance carries the ledger citation
  (``aesir-ledger:AES-SRV-010=verified``) so a reader can
  check the grade against Aesir's own book;
- the live surface must agree: ``generate`` is claimed only
  when the ledger allows it AND ``/health/ready`` reports a
  ready resident session. A verified ledger cannot make a
  stopped server generate; a ready server cannot outrun a
  ledger that marks its generation missing.

What the native API does not serve, the adapter does not
claim, whatever the ledger's engine-internal entries say:

- **embed** — Aesir's native health surface exposes embeddings
  as explicitly absent and no native embed route exists.
  (AES-RAG-003, query embedding generation, is ``partial``
  and engine-internal; a partial internal path is not a
  served endpoint, so the mesh is promised nothing.)
- **vision** — the native generate route rejects ``images``
  outright; there is no image input to this API as it stands.
- **model entries** — ``/v1/models`` publishes ids only: no
  family, no quant, no byte size. A registry ``ModelEntry``
  (Y-008) cannot be built from an id alone, so every listed
  model is recorded in ``skipped_models`` by name rather than
  dressed up — the same honesty rule Y-023 applies to
  family-less Ollama tags.

Authentication: the native service is bearer-authenticated
from a private key file Aesir itself owns. The adapter never
sees the key: authentication lives in the injected transport
(the seam below), exactly as Y-023's transport owns Ollama's
wire. This module carries no network client, so the Part I
gate's banned-import scan stays green untouched.

Errors, in the Y-007 taxonomy: no answer at all is
``engine_unreachable``; HTTP 404 (or a task naming a model
other than the resident one — the resident-only rule is
checked before any generation is attempted) is
``model_not_present``; 401/403 is ``scope_denied``; a
generation deadline (HTTP 504) is ``deadline_unmeetable``;
any other HTTP failure, a malformed NDJSON line, an answer
missing the fields its route owes, or a stream ending
without its terminal record is ``engine_error``. A stream
that dies after text was delivered reports ``partial: True``
with the text the caller already holds.
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

ENGINE_NAME = "aesir"

# The ledger entries that govern what this adapter may claim.
# AES-SRV-010 is the native service itself: authenticated
# loopback generation, its S18 streaming path, and its health
# routes are all inside that one entry's evidence boundary.
GENERATE_LEDGER_ENTRY = "AES-SRV-010"

# Ledger statuses under which a capability may be claimed at
# all, and the provenance grade each one earns in the mesh.
# Everything else — scaffold, simulated, missing, blocked,
# withdrawn, historical, unknown, absent — claims nothing.
_CLAIMABLE = {"verified": "proven", "partial": "partial"}
_KNOWN_STATUSES = {"verified", "partial", "scaffold", "simulated", "missing", "blocked"}

# The native generate route's accepted sampling/system fields
# (API matrix v1). A task's options are mapped through this
# whitelist only: the server rejects unknown fields with 400,
# so forwarding an unlisted key would be manufacturing an
# engine_error out of the adapter's own carelessness.
_GENERATE_OPTION_FIELDS = {
    "system",
    "max_tokens",
    "timeout_ms",
    "temperature",
    "top_k",
    "top_p",
    "min_p",
    "repeat_penalty",
    "seed",
}

_ENTRY_HEADING = re.compile(r"^###\s+(AES-[A-Z]+-\d+)\b", re.MULTILINE)
_STATUS_LINE = re.compile(r"^-\s+\*\*Status:\*\*\s+`([^`]+)`", re.MULTILINE)


def parse_ledger(text: str) -> dict[str, str]:
    """Aesir's capability ledger, as data: entry id -> status.

    Reads the ledger's own markdown shape — ``### AES-XXX-NNN``
    headings, each followed by its ``- **Status:**`` line — and
    normalises each status to its leading word when that word
    is one of the ledger's known statuses. Anything else the
    ledger writes (``historical / ...``, ``withdrawn / ...``,
    compound gate annotations) normalises to ``"other"``:
    a status the mesh cannot name is a status the mesh does
    not claim on. An entry with no status line is absent from
    the result, and absence claims nothing either.
    """
    entries: dict[str, str] = {}
    headings = list(_ENTRY_HEADING.finditer(text))
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        section = text[heading.start() : end]
        status_match = _STATUS_LINE.search(section)
        if status_match is None:
            continue
        raw = status_match.group(1).strip().lower()
        leading = re.split(r"[\s/,]", raw, maxsplit=1)[0]
        entries[heading.group(1)] = leading if leading in _KNOWN_STATUSES else "other"
    return entries


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


class AesirAdapterError(Exception):
    """Any adapter failure. ``error_code`` names it in the
    Y-007 taxonomy when the failure is a task failure."""

    def __init__(self, message: str, *, error_code: str | None = None):
        super().__init__(message)
        self.error_code = error_code


class AesirTransportError(AesirAdapterError):
    """The transport seam's failure base — a real transport
    raises these; the adapter maps them, never sees sockets."""


class AesirUnreachable(AesirTransportError):
    """No answer at all: connection refused, timed out, gone."""


class AesirHttpError(AesirTransportError):
    """The server answered with a non-success HTTP status."""

    def __init__(self, status: int, detail: str = ""):
        super().__init__(f"aesir answered HTTP {status}" + (f": {detail}" if detail else ""))
        self.status = status
        self.detail = detail


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise AesirAdapterError(f"{kind} fails its schema: {errors[0].message}")
    return record


def _task_model(task: dict) -> str | None:
    requirements = task.get("requirements") or {}
    if requirements.get("model"):
        return requirements["model"]
    payload = task.get("payload") or {}
    return payload.get("model") if isinstance(payload.get("model"), str) else None


class AesirAdapter:
    """One node's Aesir native service, wrapped. ``transport``
    is the seam described in the module docstring (it also owns
    the service's bearer key — the adapter never sees it);
    ``ledger`` is the capability-ledger snapshot (entry id ->
    status, as ``parse_ledger()`` returns); ``timer`` (seconds,
    monotonic) is injectable so timings are deterministic in
    tests and honestly measured in life."""

    engine = ENGINE_NAME
    is_simulated = False

    def __init__(
        self,
        transport,
        *,
        ledger: dict[str, str] | None = None,
        base_url: str = "http://127.0.0.1:11111",
        timer=None,
    ):
        self._transport = transport
        self.base_url = base_url.rstrip("/")
        self._timer = timer or time.perf_counter
        self._ledger = dict(ledger or {})
        self._readiness: dict | None = None  # last /health/ready answer
        self.skipped_models: list[str] = []

    # -- the ledger --------------------------------------------------------

    def ledger_status(self, entry: str) -> str | None:
        """One entry's normalised status, or None when the
        snapshot carries no such entry. Absence is reported as
        absence — never upgraded to a claim."""
        return self._ledger.get(entry)

    def _grade(self, entry: str) -> str:
        """The provenance grade an entry earns in the mesh:
        its own status, mirrored, never laundered upward."""
        return _CLAIMABLE.get(self._ledger.get(entry, ""), "unproven")

    def _claimable(self, entry: str) -> bool:
        return self._ledger.get(entry) in _CLAIMABLE

    def ledger_summary(self) -> dict[str, int]:
        """The snapshot's statuses, counted — the shape of
        Aesir's own book as this node holds it, for the census
        and the operator's door to show without re-reading the
        ledger."""
        counts: dict[str, int] = {}
        for status in self._ledger.values():
            counts[status] = counts.get(status, 0) + 1
        return counts

    # -- transport, mapped ---------------------------------------------------

    def _get_json(self, path: str) -> dict:
        return self._transport.get_json(path)

    def _post_json(self, path: str, body: dict) -> dict:
        return self._transport.post_json(path, body)

    def _post_lines(self, path: str, body: dict) -> Iterable[str]:
        return self._transport.post_lines(path, body)

    @staticmethod
    def _map_transport_error(exc: AesirTransportError, *, model: str | None = None) -> str:
        """A transport failure's Y-007 name: unreachable is its
        own code; 404 while asking for one model is that model
        not present; a refused key is scope_denied; Aesir's
        generation deadline is deadline_unmeetable; anything
        else is the engine's error to own."""
        if isinstance(exc, AesirUnreachable):
            return "engine_unreachable"
        if isinstance(exc, AesirHttpError):
            if exc.status == 404 and model is not None:
                return "model_not_present"
            if exc.status in (401, 403):
                return "scope_denied"
            if exc.status == 504:
                return "deadline_unmeetable"
        return "engine_error"

    # -- discovery -------------------------------------------------------------

    def _fetch_readiness(self) -> dict | None:
        """The /health/ready answer, or None when the resident
        session is not ready (the route answers 503 then — a
        finding about readiness, not about reachability)."""
        try:
            answer = self._get_json("/health/ready")
        except AesirHttpError as exc:
            if exc.status == 503:
                self._readiness = None
                return None
            raise
        readiness = answer if isinstance(answer, dict) else {}
        self._readiness = readiness if readiness.get("ready") is True else None
        return self._readiness

    def resident_model(self) -> str | None:
        """The one model Aesir currently serves, per
        /health/ready — the only model a generate task on this
        node may name. None when no session is ready."""
        readiness = self._fetch_readiness()
        if readiness is None:
            return None
        model = readiness.get("model")
        return model if isinstance(model, str) and model else None

    def discover(self) -> dict:
        """This engine's honest capability record (Y-008
        shape): the ledger's word and the live surface's word,
        both required, the lower of the two believed.
        Unreachable is a returned finding, never an exception."""
        record: dict = {
            "engine": ENGINE_NAME,
            "reachable": False,
            "capabilities": {
                "generate": False,
                "embed": False,
                "vision": False,
                "stream": False,
                "provenance_grade": self._grade(GENERATE_LEDGER_ENTRY),
            },
        }
        try:
            live = self._get_json("/health/live")
        except AesirTransportError:
            return _validate(_ENGINE_VALIDATOR, record, "engine capability")
        if not isinstance(live, dict) or live.get("status") != "ok":
            # Something answered on the port, but it did not
            # speak Aesir's liveness shape — not this engine.
            return _validate(_ENGINE_VALIDATOR, record, "engine capability")
        record["reachable"] = True
        try:
            readiness = self._fetch_readiness()
        except AesirTransportError:
            readiness = None  # liveness spoke; readiness could not be read
        resident = readiness.get("model") if readiness else None
        if (
            self._claimable(GENERATE_LEDGER_ENTRY)
            and readiness is not None
            and isinstance(resident, str)
            and resident
        ):
            record["capabilities"]["generate"] = True
            # Streaming generate is inside AES-SRV-010's own
            # evidence boundary (its S18 path) — the same entry
            # governs both, claimed and graded together.
            record["capabilities"]["stream"] = True
        # embed / vision stay False whatever the live surface
        # says: the native API as it stands serves neither
        # (see the module docstring).
        return _validate(_ENGINE_VALIDATOR, record, "engine capability")

    def capability_for_census(self) -> dict:
        """``discover()`` under the name the census collector's
        engines callable wants (Y-022's live-truth seam) — this
        is where the ledger reaches the census: the record the
        collector carries already bears the ledger's grade."""
        return self.discover()

    # -- models ----------------------------------------------------------------

    def list_models(self) -> list[str]:
        """The catalog ids /v1/models reports, in the server's
        own order. The matrix's warning rides with them: a
        listed model is visible, not loaded and not runnable —
        only ``resident_model()`` may be generated with."""
        answer = self._get_json("/v1/models")
        data = answer.get("data") if isinstance(answer, dict) else None
        if not isinstance(data, list):
            raise AesirAdapterError(
                "/v1/models answered without a data list", error_code="engine_error"
            )
        return [
            entry["id"]
            for entry in data
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        ]

    def model_names_for_census(self) -> list[str]:
        """``list_models()`` for the census collector's
        models_present callable (Y-022's seam)."""
        return self.list_models()

    def model_entries(self) -> list[dict]:
        """The catalog as validated registry entries (Y-008
        shape), built only from fields Aesir publishes. The
        native list publishes ids alone — no family, no quant,
        no byte size — so in practice every model is skipped
        by name in ``skipped_models``; an entry is built only
        if a future answer carries the fields an entry is made
        of, and then only with the ledger's claim for the
        resident model behind its capabilities."""
        entries: list[dict] = []
        self.skipped_models = []
        answer = self._get_json("/v1/models")
        data = answer.get("data") if isinstance(answer, dict) else None
        if not isinstance(data, list):
            raise AesirAdapterError(
                "/v1/models answered without a data list", error_code="engine_error"
            )
        resident = self.resident_model()
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            family = item.get("family")
            quant = item.get("quant")
            if (
                not isinstance(family, str)
                or not isinstance(quant, dict)
                or not isinstance(quant.get("name"), str)
                or not isinstance(quant.get("format"), str)
                or not isinstance(quant.get("size_gb"), int | float)
                or quant["size_gb"] <= 0
            ):
                self.skipped_models.append(item["id"])
                continue
            capabilities: list[str] = []
            if item["id"] == resident and self._claimable(GENERATE_LEDGER_ENTRY):
                capabilities = ["generate", "stream"]
            entry: dict = {
                "name": item["id"],
                "family": family,
                "quants": [
                    {
                        "name": quant["name"],
                        "format": quant["format"],
                        "size_gb": quant["size_gb"],
                    }
                ],
                "capabilities": capabilities,
                "runnable_on": [],
            }
            entries.append(_validate(_MODEL_VALIDATOR, entry, "model entry"))
        return entries

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
        ledger_status = self.ledger_status(GENERATE_LEDGER_ENTRY)
        if ledger_status is not None:
            # The grade's receipt: any reader can check this
            # citation against Aesir's own ledger.
            provenance["verification"] = f"aesir-ledger:{GENERATE_LEDGER_ENTRY}={ledger_status}"
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

    @staticmethod
    def _service_run_ms(answer: dict, fallback_ms: float) -> float:
        """Aesir's own service-phase timings (prefill + decode)
        when the answer carries them — the engine's measurement
        of its own run, preferred over the adapter's wall clock;
        the timer's measure otherwise."""
        timings = answer.get("timings_ms")
        if isinstance(timings, dict):
            prefill = timings.get("prefill")
            decode = timings.get("decode")
            if isinstance(prefill, int | float) and isinstance(decode, int | float):
                return float(prefill) + float(decode)
        return fallback_ms

    @staticmethod
    def _generate_result(answer: dict) -> dict:
        result: dict = {"text": answer["text"], "engine": ENGINE_NAME}
        for field in ("finish_reason", "prompt_tokens", "generated_tokens", "backend"):
            if isinstance(answer.get(field), str | int):
                result[field] = answer[field]
        if isinstance(answer.get("cpu_offload"), int | float):
            result["cpu_offload"] = answer["cpu_offload"]
        return result

    def generate(
        self, task: dict, node_id: str, *, stream: bool = True
    ) -> Iterator[tuple[dict | None, dict | None]]:
        """Run one generate task against the resident model;
        yield (stream_event, None) pairs as the engine answers,
        then a final (None, result) pair carrying the terminal
        ResultEnvelope.

        The resident-only rule is enforced before generation:
        a task naming any other model fails as
        ``model_not_present`` without a generation attempt,
        exactly as the server itself would answer 404. With
        ``stream=False`` the only event yielded is ``completed``
        — a finished answer is never re-chunked into a fake
        stream (ADR-004). A stream that ends without Aesir's
        terminal record raises ``engine_error``: incomplete is
        not success. Failures raise AesirAdapterError with a
        Y-007 ``error_code``; ``run_task()`` is the door that
        turns them into failed result envelopes instead.
        """
        _validate(_TASK_VALIDATOR, task, "task envelope")
        payload = task.get("payload") or {}
        prompt = payload.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            raise AesirAdapterError(
                "a generate task needs a nonempty payload.prompt string",
                error_code="malformed_envelope",
            )
        try:
            resident = self.resident_model()
        except AesirTransportError as exc:
            raise AesirAdapterError(
                str(exc), error_code=self._map_transport_error(exc, model=_task_model(task))
            ) from exc
        if resident is None:
            raise AesirAdapterError(
                "aesir reports no ready resident model on this node",
                error_code="engine_unreachable",
            )
        model = _task_model(task) or resident
        if model != resident:
            raise AesirAdapterError(
                f"model {model!r} is not this node's resident aesir model ({resident!r})",
                error_code="model_not_present",
            )
        body: dict = {"prompt": prompt, "stream": stream}
        options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
        for field in _GENERATE_OPTION_FIELDS:
            if field in options:
                body[field] = options[field]
        if isinstance(payload.get("system"), str):
            body["system"] = payload["system"]
        started = self._timer()
        if not stream:
            try:
                answer = self._post_json("/v1/generate", body)
            except AesirTransportError as exc:
                raise AesirAdapterError(
                    str(exc), error_code=self._map_transport_error(exc, model=model)
                ) from exc
            if not isinstance(answer, dict) or not isinstance(answer.get("text"), str):
                raise AesirAdapterError(
                    "/v1/generate answered without a text string", error_code="engine_error"
                )
            run_ms = self._service_run_ms(answer, (self._timer() - started) * 1000.0)
            result = self._result(
                task,
                node_id,
                status="completed",
                run_ms=run_ms,
                model=answer.get("model") if isinstance(answer.get("model"), str) else model,
                result=self._generate_result(answer),
            )
            yield self._stream_event(task["id"], 0, "completed"), None
            yield None, result
            return

        yield self._stream_event(task["id"], 0, "started"), None
        seq = 1
        chunks: list[str] = []
        final: dict | None = None
        try:
            lines = self._post_lines("/v1/generate", body)
            for line in lines:
                if not line or not line.strip():
                    continue
                try:
                    piece = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AesirAdapterError(
                        f"/v1/generate streamed a malformed NDJSON line: {line[:80]!r}",
                        error_code="engine_error",
                    ) from exc
                if not isinstance(piece, dict) or not isinstance(piece.get("text"), str):
                    raise AesirAdapterError(
                        "/v1/generate streamed a record without a text string",
                        error_code="engine_error",
                    )
                if piece.get("done"):
                    final = piece
                    break
                if piece["text"]:
                    chunks.append(piece["text"])
                    yield self._stream_event(
                        task["id"], seq, "token", {"text": piece["text"]}
                    ), None
                    seq += 1
        except AesirTransportError as exc:
            raise AesirAdapterError(
                str(exc), error_code=self._map_transport_error(exc, model=model)
            ) from exc
        if final is None:
            # The matrix's rule, enforced: no terminal record,
            # no success — however much text arrived.
            raise AesirAdapterError(
                "/v1/generate stream ended without its terminal record — incomplete",
                error_code="engine_error",
            )
        run_ms = self._service_run_ms(final, (self._timer() - started) * 1000.0)
        answer = dict(final)
        answer["text"] = "".join(chunks)
        result = self._result(
            task,
            node_id,
            status="completed",
            run_ms=run_ms,
            model=answer.get("model") if isinstance(answer.get("model"), str) else model,
            result=self._generate_result(answer),
        )
        yield self._stream_event(task["id"], seq, "completed"), None
        yield None, result

    def run_task(self, task: dict, node_id: str) -> tuple[dict, list[dict]]:
        """The collecting front door: one task in, (ResultEnvelope,
        StreamEvents) out — the simulator's return shape, from a
        real engine's answers. Only generate/chat tasks are
        served: embed and vision tasks are refused by name,
        because the native API as it stands has no route for
        them to be served by (a refusal is honest; a fabricated
        embedding would not be). Adapter failures become failed
        envelopes with Y-007 codes; a stream that dies after
        text was delivered reports ``partial: True`` with the
        text the caller already holds."""
        _validate(_TASK_VALIDATOR, task, "task envelope")
        kind = task.get("kind", "")
        # Kinds are dotted names (``chat.generate``, ``embed.text``,
        # ``vision.describe``): the capability a kind asks for is
        # one of its segments, at either end.
        segments = kind.split(".")
        model = _task_model(task) or "unknown"
        if "embed" in segments:
            result = self._result(
                task,
                node_id,
                status="refused",
                run_ms=0.0,
                model=model,
                error={
                    "code": "no_capable_node",
                    "detail": (
                        "aesir's native service exposes embeddings as absent "
                        f"({GENERATE_LEDGER_ENTRY}); there is no embed route to call"
                    ),
                },
                result={"engine": ENGINE_NAME},
            )
            return result, [self._stream_event(task["id"], 0, "failed")]
        if "vision" in segments:
            result = self._result(
                task,
                node_id,
                status="refused",
                run_ms=0.0,
                model=model,
                error={
                    "code": "no_capable_node",
                    "detail": (
                        "aesir's native generate route rejects image input; "
                        "no vision route exists in its API as it stands"
                    ),
                },
                result={"engine": ENGINE_NAME},
            )
            return result, [self._stream_event(task["id"], 0, "failed")]
        if not set(segments) & {"generate", "chat"}:
            result = self._result(
                task,
                node_id,
                status="refused",
                run_ms=0.0,
                model=model,
                error={
                    "code": "no_capable_node",
                    "detail": f"the aesir adapter serves generate tasks, not {kind!r}",
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
        except AesirAdapterError as exc:
            run_ms = (self._timer() - started) * 1000.0
            partial = bool(streamed_text)
            result = self._failure_result(
                task,
                node_id,
                exc,
                model=model if model != "unknown" else (self._readiness or {}).get("model", model),
                run_ms=run_ms,
                partial=partial,
                result=(
                    {"text": "".join(streamed_text), "engine": ENGINE_NAME} if partial else None
                ),
            )
            events.append(self._stream_event(task["id"], len(events), "failed"))
            return result, events
        raise AesirAdapterError(  # pragma: no cover - generate always ends with a result
            "generate ended without a result", error_code="internal"
        )

    def _failure_result(
        self,
        task: dict,
        node_id: str,
        exc: AesirAdapterError,
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
