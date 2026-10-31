"""Node conformance kit — Y-046.

ADR-004's gate, made a test pack: *an adapter that fails the
kit does not join the mesh, however promising.* Until this
slice the three adapters (Ollama Y-023, Aesir Y-024,
llama.cpp / OpenAI-compatible Y-025) each carried their own
acceptance suite, and a fourth adapter would have had nothing
but those suites to copy from and no single bar to clear.
This slice is that bar — one runner, :class:`ConformanceKit`,
that any engine adapter is put through before a node may host
it, checking the four families the roadmap names:

- **contract** — the adapter *has* the node contract's surface
  (``engine``, ``discover``, ``list_models``, ``model_entries``,
  ``run_task``, and the Y-022 census seams
  ``capability_for_census`` / ``model_names_for_census``);
  discovery answers a validated Y-008 EngineCapability naming
  the adapter's own engine, and answers it the same way twice;
  the model list is the engine's own names; registry entries
  are built only from listed models and claim no capability
  discovery itself does not claim (an invented entry, or one
  dressed in an unclaimed capability, fails by name); and an
  embedding model the caller names actually embeds, one vector
  per input.
- **streaming** — a generate task the caller names completes
  end to end as a validated ResultEnvelope whose provenance is
  this node, this engine, this model; its StreamEvents are
  validated, sequenced from 0 without a gap, and the token
  events' text *is* the result's text (a stream that tells a
  different story than its result fails); and a non-streamed
  answer is never re-chunked into a fake stream — ``generate``
  with ``stream=False`` yields exactly one ``completed`` event
  and no token events (ADR-004's rule, checked directly).
- **errors** — failures are named in the Y-007 taxonomy, never
  dressed as success: a task naming a model no surface lists
  ends failed or refused with an error code (never completed),
  a task kind no adapter serves is refused
  ``no_capable_node``, and — when the caller hands in the same
  adapter built unreachable — a generate task on it ends
  failed with a named code instead of raising or completing.
- **census honesty** — the claims discovery makes are the
  claims the census will carry, checked as rules: the census
  seams agree with discovery and the model list exactly;
  ``stream`` is claimed only with ``generate`` (streaming is
  generate's mode, never a decoration on an engine that cannot
  generate); a reachable engine listing no models claims no
  ``generate``; and an unreachable engine claims *nothing* —
  every capability False, discovery returned as a finding,
  never raised (checked on the unreachable adapter when one is
  handed in, or on the adapter itself when it is unreachable).

How a run goes: the caller builds the adapter the way
deployment would (transport wiring is the caller's, exactly
as in Y-023 – Y-025) and names what the kit may exercise —
``generate_model`` (a listed model a generate task may run),
``embed_model`` (likewise for an embed task), and optionally
``unreachable_adapter`` (the same adapter over a dead
transport). A check the caller's naming does not reach is
``skipped`` in the report, with the reason in its detail — a
skip is a stated absence, never a quiet pass. Every other
check runs whatever the adapter does: an adapter that raises
where the contract says return, or returns a record that
fails its schema, fails that check with the exception named
in the detail. The kit itself raises only on its own misuse
(no adapter, a bad node id).

The verdict is the validated ``conformance_report`` v1:
every check present in the kit's fixed order (passed /
failed / skipped counted apart), ``verdict`` ``conformant``
only when *no* check failed, and ``simulated`` carried from
the caller — the kit runs against whatever transport the
adapter was built with and cannot see past that seam, so a
run over fake transports is declared ``simulated: true`` by
the caller who wired them and is never dressed as a live
server run (the citizen slices own hardware proof).
``require_conformant`` raises on a non-conformant report —
the joining gate in one call. Each completed run records one
Y-013 event (names and counts only, never payload content).

Honesty: the kit is a *pre-join* gate, run at deployment and
test time. No node agent seam grows at this slice and no
citizen factory is rewired — an adapter joins when its
operator runs the kit green, and the report is the evidence.
The in-tree acceptance drives the kit over all three real
adapters (four engine names) on fake transports speaking
their own wire shapes, labelled SIMULATED, and over a stub
adapter broken one named way at a time — each break fails
exactly the check that names it.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.observability import make_event

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

GENERATE_TASK_ID = "3f6f1b2c-9d4e-4c5a-8b7d-000000000046"
NONSTREAM_TASK_ID = "3f6f1b2c-9d4e-4c5a-8b7d-000000000052"
EMBED_TASK_ID = "3f6f1b2c-9d4e-4c5a-8b7d-000000000050"
UNKNOWN_TASK_ID = "3f6f1b2c-9d4e-4c5a-8b7d-000000000048"
UNSERVABLE_TASK_ID = "3f6f1b2c-9d4e-4c5a-8b7d-000000000049"
UNREACHABLE_TASK_ID = "3f6f1b2c-9d4e-4c5a-8b7d-000000000051"
GHOST_MODEL = "conformance-ghost:latest"

_METHODS = (
    "discover",
    "list_models",
    "model_entries",
    "run_task",
    "capability_for_census",
    "model_names_for_census",
)


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


_CHECK_VALIDATOR = _validator("conformance_check.schema.json")
_REPORT_VALIDATOR = _validator("conformance_report.schema.json")
_ENGINE_VALIDATOR = _validator("engine_capability.schema.json")
_MODEL_VALIDATOR = _validator("model_entry.schema.json")
_RESULT_VALIDATOR = _validator("result_envelope.schema.json")
_STREAM_VALIDATOR = _validator("stream_event.schema.json")

_ENGINE_NAMES = tuple(_load_schema("engine_capability.schema.json")["properties"]["engine"]["enum"])
_ERROR_CODES = set(_load_schema("error_codes.schema.json")["$defs"]["error_code"]["enum"])


class ConformanceError(Exception):
    """The kit's own failure: its misuse, a record that fails
    its schema, or — through ``require_conformant`` — an
    adapter that did not pass."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise ConformanceError(f"{kind} fails its schema: {errors[0].message}")
    return record


def _schema_problems(validator, record) -> list[str]:
    if not isinstance(record, dict):
        return [f"not an object: {type(record).__name__}"]
    return [error.message for error in sorted(validator.iter_errors(record), key=str)]


def _task(task_id: str, kind: str, model: str | None, payload: dict) -> dict:
    task: dict = {
        "id": task_id,
        "kind": kind,
        "payload": payload,
        "caller": "y-046-conformance",
    }
    if model is not None:
        task["requirements"] = {"model": model}
    return task


class ConformanceKit:
    """One adapter's examination. ``adapter`` is the candidate;
    ``node_id`` is the node the kit runs its tasks as (results
    must carry it in their provenance); ``generate_model`` /
    ``embed_model`` name the listed models its execution checks
    may exercise; ``unreachable_adapter`` is the same adapter
    built over a dead transport, for the honesty checks that
    need a corpse; ``simulated`` declares the wiring the kit
    cannot see (see the module docstring); ``now`` is
    injectable so timestamps are testable."""

    def __init__(
        self,
        adapter,
        *,
        node_id: str = "conformance-node-1",
        generate_model: str | None = None,
        embed_model: str | None = None,
        unreachable_adapter=None,
        simulated: bool = False,
        now=None,
    ):
        if adapter is None:
            raise ConformanceError("the conformance kit needs an adapter to examine")
        if not isinstance(node_id, str) or not node_id:
            raise ConformanceError("the conformance kit needs a node_id")
        self._adapter = adapter
        self.node_id = node_id
        self._generate_model = generate_model
        self._embed_model = embed_model
        self._unreachable = unreachable_adapter
        self._simulated = bool(simulated)
        self._now = now or (lambda: datetime.now(UTC))
        self._events: list[dict] = []
        self._seq = 0
        # Lazily filled run caches: the generate run is shared
        # by the streaming checks, discovery by almost all.
        self._discovery: dict | None = None
        self._discovery_exc: Exception | None = None
        self._discovery_done = False
        self._models: list | None = None
        self._models_exc: Exception | None = None
        self._models_done = False
        self._generate_run: tuple | None = None
        self._generate_exc: Exception | None = None
        self._generate_done = False

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    # -- shared adapter readings -------------------------------------------

    def _discover(self) -> dict:
        if not self._discovery_done:
            self._discovery_done = True
            try:
                self._discovery = self._adapter.discover()
            except Exception as exc:  # noqa: BLE001 — the kit catches every adapter failure as its check's data (an adapter's failure is a check's data)
                self._discovery_exc = exc
        if self._discovery_exc is not None:
            raise self._discovery_exc
        return self._discovery  # type: ignore[return-value]

    def _list_models(self) -> list:
        if not self._models_done:
            self._models_done = True
            try:
                self._models = self._adapter.list_models()
            except Exception as exc:  # noqa: BLE001 — the kit catches every adapter failure as its check's data (an adapter's failure is a check's data)
                self._models_exc = exc
        if self._models_exc is not None:
            raise self._models_exc
        return self._models  # type: ignore[return-value]

    def _run_generate(self) -> tuple:
        if not self._generate_done:
            self._generate_done = True
            task = _task(
                GENERATE_TASK_ID,
                "chat.generate",
                self._generate_model,
                {"prompt": "say hello"},
            )
            try:
                self._generate_run = self._adapter.run_task(task, self.node_id)
            except Exception as exc:  # noqa: BLE001 — the kit catches every adapter failure as its check's data (an adapter's failure is a check's data)
                self._generate_exc = exc
        if self._generate_exc is not None:
            raise self._generate_exc
        return self._generate_run  # type: ignore[return-value]

    # -- the checks -----------------------------------------------------------
    # Each returns (status, detail). A check never raises: what
    # the adapter did wrong is the check's finding, in words.

    def _check_interface_surface(self):
        engine = getattr(self._adapter, "engine", None)
        if not isinstance(engine, str) or engine not in _ENGINE_NAMES:
            return "failed", (
                f"adapter.engine is {engine!r}; the contract names one of "
                f"{', '.join(_ENGINE_NAMES)}"
            )
        missing = [name for name in _METHODS if not callable(getattr(self._adapter, name, None))]
        if missing:
            return "failed", f"the adapter lacks callable contract methods: {', '.join(missing)}"
        return "passed", f"engine {engine!r} with all {len(_METHODS)} contract methods callable"

    def _check_discovery_shape(self):
        try:
            record = self._discover()
        except Exception as exc:  # noqa: BLE001 — the kit catches every adapter failure as its check's data
            return "failed", f"discover() raised {type(exc).__name__}: {exc}"
        problems = _schema_problems(_ENGINE_VALIDATOR, record)
        if problems:
            return "failed", f"discover() fails the engine capability schema: {problems[0]}"
        if record["engine"] != getattr(self._adapter, "engine", None):
            return "failed", (
                f"discover() names engine {record['engine']!r} but adapter.engine is "
                f"{getattr(self._adapter, 'engine', None)!r}"
            )
        return "passed", "discover() answers a validated EngineCapability naming its own engine"

    def _check_discovery_repeatable(self):
        try:
            first = self._discover()
            second = self._adapter.discover()
        except Exception as exc:  # noqa: BLE001 — the kit catches every adapter failure as its check's data
            return "failed", f"a repeated discover() raised {type(exc).__name__}: {exc}"
        if first != second:
            return "failed", "two discover() calls over an unchanged engine disagree"
        return "passed", "discover() answers the same record twice over an unchanged engine"

    def _check_model_list_shape(self):
        try:
            discovery = self._discover()
        except Exception as exc:  # noqa: BLE001 — the kit catches every adapter failure as its check's data
            return "failed", f"discover() raised {type(exc).__name__}: {exc}"
        if not discovery.get("reachable"):
            return "skipped", "the engine is unreachable; its model list is not interrogated"
        try:
            models = self._list_models()
        except Exception as exc:  # noqa: BLE001 — the kit catches every adapter failure as its check's data
            return "failed", f"list_models() raised {type(exc).__name__}: {exc}"
        if not isinstance(models, list) or not all(
            isinstance(name, str) and name for name in models
        ):
            return "failed", f"list_models() must answer a list of model names, got {models!r}"
        if len(models) != len(set(models)):
            return "failed", "list_models() names a model twice"
        return "passed", f"list_models() answers {len(models)} model name(s), each once"

    def _check_model_entries_honest(self):
        try:
            discovery = self._discover()
        except Exception as exc:  # noqa: BLE001 — the kit catches every adapter failure as its check's data
            return "failed", f"discover() raised {type(exc).__name__}: {exc}"
        if not discovery.get("reachable"):
            return "skipped", "the engine is unreachable; its registry entries are not interrogated"
        try:
            listed = self._list_models()
            entries = self._adapter.model_entries()
        except Exception as exc:  # noqa: BLE001 — the kit catches every adapter failure as its check's data
            return "failed", f"model_entries() raised {type(exc).__name__}: {exc}"
        if not isinstance(entries, list):
            return "failed", f"model_entries() must answer a list, got {type(entries).__name__}"
        claimed = {
            name
            for name, on in discovery.get("capabilities", {}).items()
            if on is True and name != "provenance_grade"
        }
        for entry in entries:
            problems = _schema_problems(_MODEL_VALIDATOR, entry)
            if problems:
                return "failed", f"a registry entry fails its schema: {problems[0]}"
            if entry["name"] not in listed:
                return "failed", (
                    f"registry entry {entry['name']!r} is not in list_models() — "
                    "an entry is built from a listed model, never invented"
                )
            extra = set(entry.get("capabilities", [])) - claimed
            if extra:
                return "failed", (
                    f"registry entry {entry['name']!r} claims {sorted(extra)} that "
                    "discovery itself does not claim"
                )
        skipped = getattr(self._adapter, "skipped_models", None)
        if isinstance(skipped, list):
            built = {entry["name"] for entry in entries if isinstance(entry, dict)}
            overlap = built & set(skipped)
            if overlap:
                return "failed", f"models both built into entries and skipped: {sorted(overlap)}"
            unlisted = set(skipped) - set(listed)
            if unlisted:
                return "failed", f"skipped_models names unlisted models: {sorted(unlisted)}"
        return "passed", (
            f"{len(entries)} registry entr(ies), each listed, schema-valid, and "
            "claiming nothing discovery does not claim"
        )

    def _check_embed_completes(self):
        if self._embed_model is None:
            return "skipped", "no embed model was named; the embed door is not exercised"
        try:
            discovery = self._discover()
        except Exception as exc:  # noqa: BLE001 — the kit catches every adapter failure as its check's data
            return "failed", f"discover() raised {type(exc).__name__}: {exc}"
        if not discovery.get("capabilities", {}).get("embed"):
            return "skipped", (
                f"an embed model ({self._embed_model!r}) was named but discover() "
                "claims no embed capability — the claim itself is the census checks' ground"
            )
        task = _task(
            EMBED_TASK_ID, "embed.text", self._embed_model, {"texts": ["hello", "mesh"]}
        )
        try:
            result, _events = self._adapter.run_task(task, self.node_id)
        except Exception as exc:  # noqa: BLE001 — the kit catches every adapter failure as its check's data
            return "failed", f"an embed run_task raised {type(exc).__name__}: {exc}"
        problems = _schema_problems(_RESULT_VALIDATOR, result)
        if problems:
            return "failed", f"the embed result fails its schema: {problems[0]}"
        if result["status"] != "completed":
            code = (result.get("error") or {}).get("code")
            return "failed", f"the embed task ended {result['status']} ({code}), not completed"
        vectors = (result.get("result") or {}).get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != 2 or not all(
            isinstance(vector, list) and vector for vector in vectors
        ):
            return "failed", "the embed result carries no one-vector-per-input embeddings"
        return "passed", "the embed task completed with one vector per input"

    def _check_census_seams_agree(self):
        try:
            discovery = self._discover()
            via_seam = self._adapter.capability_for_census()
        except Exception as exc:  # noqa: BLE001 — the kit catches every adapter failure as its check's data
            return "failed", f"a census seam raised {type(exc).__name__}: {exc}"
        if via_seam != discovery:
            return "failed", "capability_for_census() disagrees with discover()"
        if not discovery.get("reachable"):
            return "passed", "the capability seam agrees; the model seam is not interrogated while unreachable"
        try:
            listed = self._list_models()
            via_models_seam = self._adapter.model_names_for_census()
        except Exception as exc:  # noqa: BLE001 — the kit catches every adapter failure as its check's data
            return "failed", f"model_names_for_census() raised {type(exc).__name__}: {exc}"
        if via_models_seam != listed:
            return "failed", "model_names_for_census() disagrees with list_models()"
        return "passed", "both census seams answer exactly what discovery and the model list answer"

    def _check_stream_follows_generate(self):
        try:
            caps = self._discover().get("capabilities", {})
        except Exception as exc:  # noqa: BLE001 — the kit catches every adapter failure as its check's data
            return "failed", f"discover() raised {type(exc).__name__}: {exc}"
        if caps.get("stream") and not caps.get("generate"):
            return "failed", "stream is claimed without generate — streaming is generate's mode, not a decoration"
        if caps.get("generate") and not caps.get("stream"):
            return "passed", "generate is claimed without stream — the engine says it cannot stream, and says so (ADR-004)"
        return "passed", "stream and generate are claimed together, or neither is"

    def _check_empty_shelf_claims_nothing(self):
        try:
            discovery = self._discover()
        except Exception as exc:  # noqa: BLE001 — the kit catches every adapter failure as its check's data
            return "failed", f"discover() raised {type(exc).__name__}: {exc}"
        if not discovery.get("reachable"):
            return "skipped", "the engine is unreachable; the empty-shelf rule is the unreachable check's ground"
        try:
            listed = self._list_models()
        except Exception as exc:  # noqa: BLE001 — the kit catches every adapter failure as its check's data
            return "failed", f"list_models() raised {type(exc).__name__}: {exc}"
        if listed == [] and discovery.get("capabilities", {}).get("generate"):
            return "failed", "the engine lists no models but claims generate — an empty shelf generates nothing"
        if listed != []:
            return "passed", "the shelf is not empty; the rule has nothing to bite on in this run"
        return "passed", "an empty shelf claims no generate"

    def _check_unreachable_claims_nothing(self):
        candidate = self._unreachable
        if candidate is None:
            try:
                discovery = self._discover()
            except Exception as exc:  # noqa: BLE001 — the kit catches every adapter failure as its check's data
                return "failed", f"discover() raised {type(exc).__name__}: {exc}"
            if discovery.get("reachable"):
                return "skipped", "no unreachable adapter was handed in and this engine is reachable"
            candidate = self._adapter
        else:
            try:
                discovery = candidate.discover()
            except Exception as exc:  # noqa: BLE001 — the kit catches every adapter failure as its check's data
                return "failed", (
                    f"the unreachable adapter's discover() raised {type(exc).__name__}: {exc} — "
                    "unreachable is a returned finding, never an exception"
                )
        problems = _schema_problems(_ENGINE_VALIDATOR, discovery)
        if problems:
            return "failed", f"the unreachable discovery fails its schema: {problems[0]}"
        if discovery.get("reachable"):
            return "failed", "the adapter built unreachable reports reachable: true"
        caps = discovery.get("capabilities", {})
        claimed = [name for name in ("generate", "embed", "vision", "stream") if caps.get(name)]
        if claimed:
            return "failed", (
                f"an unreachable engine claims {claimed} — a dead engine claims nothing"
            )
        return "passed", "the unreachable engine returns reachable: false and claims nothing"

    def _generate_gate(self):
        """(status, detail) when the generate checks cannot run,
        else None — the shared door for the streaming family."""
        if self._generate_model is None:
            return "skipped", "no generate model was named; the generate door is not exercised"
        try:
            discovery = self._discover()
        except Exception as exc:  # noqa: BLE001 — the kit catches every adapter failure as its check's data
            return "failed", f"discover() raised {type(exc).__name__}: {exc}"
        if not discovery.get("reachable"):
            return "skipped", "the engine is unreachable; its generate door is the error checks' ground"
        if not discovery.get("capabilities", {}).get("generate"):
            return "skipped", (
                f"a generate model ({self._generate_model!r}) was named but discover() "
                "claims no generate capability — the claim itself is the census checks' ground"
            )
        return None

    def _check_generate_completes(self):
        gate = self._generate_gate()
        if gate is not None:
            return gate
        try:
            result, _events = self._run_generate()
        except Exception as exc:  # noqa: BLE001 — the kit catches every adapter failure as its check's data
            return "failed", f"a generate run_task raised {type(exc).__name__}: {exc}"
        problems = _schema_problems(_RESULT_VALIDATOR, result)
        if problems:
            return "failed", f"the generate result fails its schema: {problems[0]}"
        if result["task_id"] != GENERATE_TASK_ID:
            return "failed", f"the result names task {result['task_id']!r}, not the task that ran"
        provenance = result.get("provenance", {})
        expected = {
            "node_id": self.node_id,
            "engine": getattr(self._adapter, "engine", None),
            "model": self._generate_model,
        }
        for field, want in expected.items():
            if provenance.get(field) != want:
                return "failed", (
                    f"the result's provenance {field} is {provenance.get(field)!r}, "
                    f"not {want!r}"
                )
        if result["status"] != "completed":
            code = (result.get("error") or {}).get("code")
            return "failed", f"the generate task ended {result['status']} ({code}), not completed"
        return "passed", "the generate task completed with provenance naming this node, engine, and model"

    def _check_stream_shape_and_text(self):
        gate = self._generate_gate()
        if gate is not None:
            return gate
        try:
            result, events = self._run_generate()
        except Exception as exc:  # noqa: BLE001 — the kit catches every adapter failure as its check's data
            return "failed", f"a generate run_task raised {type(exc).__name__}: {exc}"
        if not isinstance(events, list) or not events:
            return "failed", "the generate run carried no stream events"
        for event in events:
            problems = _schema_problems(_STREAM_VALIDATOR, event)
            if problems:
                return "failed", f"a stream event fails its schema: {problems[0]}"
            if event["task_id"] != GENERATE_TASK_ID:
                return "failed", "a stream event names another task"
        seqs = [event["seq"] for event in events]
        if seqs != list(range(len(events))):
            return "failed", f"stream seqs are {seqs}, not 0..{len(events) - 1} without a gap"
        if events[0]["event"] not in ("accepted", "started"):
            return "failed", f"the stream opens with {events[0]['event']!r}, not accepted/started"
        if events[-1]["event"] != "completed":
            return "failed", f"the stream closes with {events[-1]['event']!r}, not completed"
        streamed = "".join(
            event["data"]["text"]
            for event in events
            if event["event"] == "token"
            and isinstance(event.get("data"), dict)
            and isinstance(event["data"].get("text"), str)
        )
        text = (result.get("result") or {}).get("text") if isinstance(result, dict) else None
        if isinstance(text, str) and text and streamed != text:
            return "failed", (
                f"the streamed tokens assemble {streamed!r} but the result claims {text!r} — "
                "the stream and its result tell different stories"
            )
        return "passed", "stream events are sequenced, open and close by the contract, and their tokens are the result's text"

    def _check_no_fake_stream(self):
        gate = self._generate_gate()
        if gate is not None:
            return gate
        generate = getattr(self._adapter, "generate", None)
        if not callable(generate):
            return "skipped", "the adapter exposes no generate() iterator; the fake-stream rule is checked through run_task only"
        task = _task(
            NONSTREAM_TASK_ID, "chat.generate", self._generate_model, {"prompt": "say hello"}
        )
        try:
            pairs = list(generate(task, self.node_id, stream=False))
        except Exception as exc:  # noqa: BLE001 — the kit catches every adapter failure as its check's data
            return "failed", f"generate(stream=False) raised {type(exc).__name__}: {exc}"
        events = [event for event, _result in pairs if event is not None]
        results = [result for _event, result in pairs if result is not None]
        tokens = [event for event in events if isinstance(event, dict) and event.get("event") == "token"]
        if tokens:
            return "failed", (
                "generate(stream=False) yielded token events — a finished answer "
                "re-chunked into a fake stream (ADR-004)"
            )
        if len(events) != 1 or events[0].get("event") != "completed":
            return "failed", (
                f"generate(stream=False) yielded {[e.get('event') for e in events if isinstance(e, dict)]}, "
                "not exactly one completed event"
            )
        if len(results) != 1 or results[0].get("status") != "completed":
            return "failed", "generate(stream=False) did not end in exactly one completed result"
        return "passed", "a non-streamed answer yields one completed event and no tokens — never re-chunked"

    def _check_unknown_model_named(self):
        task = _task(
            UNKNOWN_TASK_ID, "chat.generate", GHOST_MODEL, {"prompt": "say hello"}
        )
        try:
            result, _events = self._adapter.run_task(task, self.node_id)
        except Exception as exc:  # noqa: BLE001 — the kit catches every adapter failure as its check's data
            return "failed", f"an unknown-model task raised {type(exc).__name__}: {exc} instead of a named result"
        if not isinstance(result, dict):
            return "failed", "an unknown-model task returned no result envelope"
        if result.get("status") == "completed":
            return "failed", (
                f"a task naming unlisted model {GHOST_MODEL!r} completed — "
                "a model no surface lists cannot have run"
            )
        code = (result.get("error") or {}).get("code")
        if result.get("status") not in ("failed", "refused") or code not in _ERROR_CODES:
            return "failed", (
                f"an unknown-model task ended {result.get('status')} with error code "
                f"{code!r} — failures are named in the Y-007 taxonomy"
            )
        return "passed", f"an unknown model is refused by name ({code}), never completed"

    def _check_unservable_kind_refused(self):
        model = self._generate_model
        if model is None:
            try:
                listed = self._list_models()
                model = listed[0] if listed else GHOST_MODEL
            except Exception:  # noqa: BLE001 — the kit catches every adapter failure as its check's data
                model = GHOST_MODEL
        task = _task(
            UNSERVABLE_TASK_ID, "conformance.unservable", model, {"prompt": "say hello"}
        )
        try:
            result, _events = self._adapter.run_task(task, self.node_id)
        except Exception as exc:  # noqa: BLE001 — the kit catches every adapter failure as its check's data
            return "failed", f"an unservable-kind task raised {type(exc).__name__}: {exc} instead of a refusal"
        if not isinstance(result, dict) or result.get("status") != "refused":
            status = result.get("status") if isinstance(result, dict) else None
            return "failed", f"an unservable kind ended {status}, not refused"
        code = (result.get("error") or {}).get("code")
        if code != "no_capable_node":
            return "failed", f"an unservable kind was refused as {code!r}, not no_capable_node"
        return "passed", "a kind no adapter serves is refused no_capable_node"

    def _check_unreachable_task_named(self):
        if self._unreachable is None:
            return "skipped", "no unreachable adapter was handed in; the dead-engine task is not run"
        model = self._generate_model or GHOST_MODEL
        task = _task(
            UNREACHABLE_TASK_ID, "chat.generate", model, {"prompt": "say hello"}
        )
        try:
            result, _events = self._unreachable.run_task(task, self.node_id)
        except Exception as exc:  # noqa: BLE001 — the kit catches every adapter failure as its check's data
            return "failed", f"a task on the unreachable engine raised {type(exc).__name__}: {exc} instead of a named result"
        if not isinstance(result, dict) or result.get("status") == "completed":
            return "failed", "a task on an unreachable engine completed — a dead engine ran nothing"
        code = (result.get("error") or {}).get("code")
        if result.get("status") not in ("failed", "refused") or code not in _ERROR_CODES:
            return "failed", (
                f"a task on the unreachable engine ended {result.get('status') if isinstance(result, dict) else None} "
                f"with error code {code!r} — failures are named in the Y-007 taxonomy"
            )
        return "passed", f"a task on the unreachable engine ends by name ({code}), never completed"

    _CHECKS = (
        ("interface_surface", "contract", "_check_interface_surface"),
        ("discovery_shape", "contract", "_check_discovery_shape"),
        ("discovery_repeatable", "contract", "_check_discovery_repeatable"),
        ("model_list_shape", "contract", "_check_model_list_shape"),
        ("model_entries_honest", "contract", "_check_model_entries_honest"),
        ("embed_completes", "contract", "_check_embed_completes"),
        ("census_seams_agree", "census", "_check_census_seams_agree"),
        ("stream_follows_generate", "census", "_check_stream_follows_generate"),
        ("empty_shelf_claims_nothing", "census", "_check_empty_shelf_claims_nothing"),
        ("unreachable_claims_nothing", "census", "_check_unreachable_claims_nothing"),
        ("generate_completes", "streaming", "_check_generate_completes"),
        ("stream_shape_and_text", "streaming", "_check_stream_shape_and_text"),
        ("no_fake_stream", "streaming", "_check_no_fake_stream"),
        ("unknown_model_named", "errors", "_check_unknown_model_named"),
        ("unservable_kind_refused", "errors", "_check_unservable_kind_refused"),
        ("unreachable_task_named", "errors", "_check_unreachable_task_named"),
    )

    # -- the run ---------------------------------------------------------------

    def run(self) -> dict:
        """Run every check in the kit's fixed order and answer
        the validated ``conformance_report`` v1. A check that
        itself crashes is a failed check naming the crash — the
        kit never dies on the adapter it is examining."""
        checks: list[dict] = []
        for name, category, method_name in self._CHECKS:
            try:
                status, detail = getattr(self, method_name)()
            except Exception as exc:  # noqa: BLE001 — the kit catches every adapter failure as its check's data (the kit survives its own check's crash)
                status, detail = "failed", f"the check itself crashed: {type(exc).__name__}: {exc}"
            checks.append(
                _validate(
                    _CHECK_VALIDATOR,
                    {
                        "version": 1,
                        "name": name,
                        "category": category,
                        "status": status,
                        "detail": detail,
                    },
                    "conformance check",
                )
            )
        totals = {
            "checks": len(checks),
            "passed": sum(1 for check in checks if check["status"] == "passed"),
            "failed": sum(1 for check in checks if check["status"] == "failed"),
            "skipped": sum(1 for check in checks if check["status"] == "skipped"),
        }
        engine = getattr(self._adapter, "engine", None)
        record = {
            "version": 1,
            "node_id": self.node_id,
            "engine": engine if isinstance(engine, str) and engine else "unknown",
            "at": _fmt(self._now()),
            "simulated": self._simulated,
            "checks": checks,
            "totals": totals,
            "verdict": "conformant" if totals["failed"] == 0 else "non_conformant",
        }
        report = _validate(_REPORT_VALIDATOR, record, "conformance report")
        event_type = (
            "conformance.passed" if report["verdict"] == "conformant" else "conformance.failed"
        )
        self._events.append(
            make_event(
                type=event_type,
                node_id=self.node_id,
                component="node_agent",
                payload={
                    "engine": report["engine"],
                    "checks": totals["checks"],
                    "passed": totals["passed"],
                    "failed": totals["failed"],
                    "skipped": totals["skipped"],
                    "simulated": self._simulated,
                },
                seq=self._seq,
                at=self._now(),
            )
        )
        self._seq += 1
        return report


def run_conformance(adapter, **kwargs) -> dict:
    """One adapter's conformance report, in one call — the
    kit built, run, and answered. ``kwargs`` are
    :class:`ConformanceKit`'s."""
    return ConformanceKit(adapter, **kwargs).run()


def require_conformant(adapter, **kwargs) -> dict:
    """The joining gate in one call: the conformance report
    when the adapter passes, :class:`ConformanceError` naming
    every failed check when it does not — an adapter that
    fails the kit does not join (ADR-004)."""
    report = run_conformance(adapter, **kwargs)
    if report["verdict"] != "conformant":
        failed = [check["name"] for check in report["checks"] if check["status"] == "failed"]
        raise ConformanceError(
            f"adapter {report['engine']!r} is non-conformant; failed checks: {', '.join(failed)}"
        )
    return report


__all__ = [
    "ConformanceError",
    "ConformanceKit",
    "require_conformant",
    "run_conformance",
]
