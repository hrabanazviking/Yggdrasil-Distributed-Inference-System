"""Fake engines — Y-015.

Deterministic stub inference engines, so routing can be proven
without models: an **echo** engine (the answer is the prompt,
reflected by a fixed rule), a **latency-shaped** engine (echo's
answers, with simulated latency that is a pure function of the
task's size), and a **failure-shaped** engine (echo's answers
except where its declared failure rule fires, in the Y-007
taxonomy). Every engine is a simulator handler — it plugs into
Y-014's tunnel exactly where that slice left the plug point
(``Simulator.add_fake_node(..., handler=engine)``, or the
``add_engine_node`` helper below) — and the tunnel honours an
engine's per-task latency: a handler that offers
``latency_ms_for(task)`` sets the run's simulated latency,
replacing the node's constant.

Determinism is the point of the wind tunnel, so it is a rule
here, not a hope:

- No randomness, no wall clock, no ambient state. Latency is a
  pure function of the task; echo output is a pure function of
  the payload; embeddings are a pure function of the text
  (SHA-256 bytes mapped into [-1, 1) — a fixture shape, NOT a
  semantic embedding: two texts' vectors say nothing about the
  texts' meanings).
- The failure engine's only state is its call count, and its
  rules (always / on a trigger string / every Nth call / the
  first N calls) replay identically on a fresh engine given the
  same task sequence.

Honesty: everything here is SIMULATED. A fake engine performs no
inference, sees no image, and measures nothing on any device;
its latencies advance the simulator's virtual clock only. Its
self-description (``describe()``) is validated data shaped by
``contracts/schemas/fake_engine.schema.json`` and says
``simulated: true`` in so many words. Fake engines are handler
fixtures — they are NOT entries in a node's capability census
(the census engine names are the real wrapped engines of
ADR-004); the helper below keeps the Y-014 fixture census in
agreement with the engine's declared capabilities, nothing more.
"""

import hashlib
import json
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.simulator import SimulatedTaskFailure

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

FAKE_ENGINE_KINDS = ("echo", "latency", "failure")
CAPABILITIES = ("generate", "embed", "vision", "stream")
EMBEDDING_DIM = 8

_FAILURE_MODES = ("always", "on_trigger", "every_nth", "first_n")


def _load_schema(name: str) -> dict:
    return json.loads((_SCHEMAS / name).read_text())


def _validator(name: str):
    """A validator for one schema, with every contract schema
    registered so relative $refs resolve — the same registry the
    Y-009 contract harness builds."""
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


_DESCRIPTOR_VALIDATOR = _validator("fake_engine.schema.json")
_ERROR_CODES = set(_load_schema("error_codes.schema.json")["$defs"]["error_code"]["enum"])


class FakeEngineError(Exception):
    """A fake engine was misconfigured or misused."""


def _payload_text(payload: dict) -> str:
    """All the text a task carries, in one deterministic string:
    the prompt plus every text of an embedding batch, in order."""
    parts = []
    prompt = payload.get("prompt")
    if isinstance(prompt, str):
        parts.append(prompt)
    texts = payload.get("texts")
    if isinstance(texts, list):
        parts.extend(text for text in texts if isinstance(text, str))
    elif isinstance(payload.get("text"), str):
        parts.append(payload["text"])
    return "\n".join(parts)


def _char_count(task: dict) -> int:
    return len(_payload_text(task.get("payload") or {}))


def _pseudo_embedding(text: str) -> list[float]:
    """A deterministic fixture vector for one text: the first
    EMBEDDING_DIM bytes of its SHA-256, each mapped into [-1, 1).
    Same text, same vector, forever — and no semantic meaning."""
    digest = hashlib.sha256(text.encode()).digest()
    return [round(byte / 127.5 - 1.0, 6) for byte in digest[:EMBEDDING_DIM]]


def echo_result(task: dict, *, engine_kind: str) -> dict:
    """The shared echo behaviour every fake engine answers with
    (when it answers at all): a pure function of the payload.

    - an embedding task (kind ``embed.*`` or a ``texts`` payload)
      returns fixture vectors, one per text;
    - a vision task returns text saying plainly that no image
      was seen — a fake engine never claims a perception;
    - anything else returns the prompt reflected, or the whole
      payload as sorted JSON when there is no prompt to reflect.
    """
    payload = task.get("payload") or {}
    kind = task.get("kind", "")
    texts = payload.get("texts")
    if kind.startswith("embed") or isinstance(texts, list):
        batch = list(texts) if isinstance(texts, list) else [payload.get("text", "")]
        return {
            "embeddings": [_pseudo_embedding(str(text)) for text in batch],
            "dim": EMBEDDING_DIM,
            "simulated": True,
            "engine_kind": engine_kind,
        }
    prompt = payload.get("prompt")
    if kind.startswith("vision"):
        seen = prompt if isinstance(prompt, str) else ""
        return {
            "text": f"SIMULATED vision: no image was seen; echo: {seen}",
            "simulated": True,
            "engine_kind": engine_kind,
        }
    if isinstance(prompt, str):
        text = f"SIMULATED echo: {prompt}"
    else:
        text = "SIMULATED echo: " + json.dumps(payload, sort_keys=True)
    return {
        "text": text,
        "tokens": len(text.split()),
        "simulated": True,
        "engine_kind": engine_kind,
    }


class FakeEngine:
    """Base: a deterministic stub engine usable as a simulator
    handler — ``engine(task, node)`` — with a per-task simulated
    latency that is a pure function of the task's text size:
    ``base_ms + per_char_ms * chars``."""

    kind = "echo"
    is_simulated = True

    def __init__(
        self,
        *,
        capabilities: tuple[str, ...] = ("generate",),
        base_ms: float = 1.0,
        per_char_ms: float = 0.0,
    ):
        unknown = set(capabilities) - set(CAPABILITIES)
        if unknown:
            raise FakeEngineError(f"unknown capabilities: {sorted(unknown)}")
        if not capabilities:
            raise FakeEngineError("a fake engine must declare at least one capability")
        if base_ms < 0 or per_char_ms < 0:
            raise FakeEngineError("simulated latency cannot be negative")
        self.capabilities = tuple(sorted(set(capabilities)))
        self.base_ms = float(base_ms)
        self.per_char_ms = float(per_char_ms)
        self.calls = 0

    def latency_ms_for(self, task: dict) -> float:
        """This task's simulated latency — pure in the task."""
        return self.base_ms + self.per_char_ms * _char_count(task)

    def describe(self) -> dict:
        """The engine's honest self-description, validated
        against ``fake_engine.schema.json``."""
        record: dict = {
            "kind": self.kind,
            "simulated": True,
            "deterministic": True,
            "capabilities": list(self.capabilities),
            "latency": {"base_ms": self.base_ms, "per_char_ms": self.per_char_ms},
        }
        failure = self._failure_description()
        if failure is not None:
            record["failure"] = failure
        errors = sorted(_DESCRIPTOR_VALIDATOR.iter_errors(record), key=str)
        if errors:
            raise FakeEngineError(f"engine descriptor fails its schema: {errors[0].message}")
        return record

    def _failure_description(self) -> dict | None:
        return None

    def _answer(self, task: dict) -> dict:
        return echo_result(task, engine_kind=self.kind)

    def handle(self, task: dict, node=None) -> dict:
        """Run one task, simulator-handler style. ``node`` is
        accepted (the tunnel passes it) and never needed — a fake
        engine's behaviour is a function of the task alone."""
        self.calls += 1
        return self._answer(task)

    def __call__(self, task: dict, node=None) -> dict:
        # Dispatch through handle() so a subclass's override
        # (FailureEngine's) governs — an alias bound in this
        # class body would silently keep the base behaviour.
        return self.handle(task, node)


class EchoEngine(FakeEngine):
    """The plain stub: every task answered by the echo rule, at
    a flat simulated latency unless shaped otherwise."""

    kind = "echo"


class LatencyEngine(FakeEngine):
    """Echo's answers at a size-shaped latency: the engine whose
    timings let routing be proven — a bigger task provably takes
    longer (in simulated milliseconds) than a smaller one, by an
    exact declared formula, on any machine, forever."""

    kind = "latency"

    def __init__(self, *, base_ms: float = 10.0, per_char_ms: float = 0.5, **kwargs):
        super().__init__(base_ms=base_ms, per_char_ms=per_char_ms, **kwargs)


class FailureEngine(FakeEngine):
    """Echo's answers, except where the failure rule fires — and
    then a ``SimulatedTaskFailure`` with a named Y-007 code, which
    the tunnel turns into a ``failed`` ResultEnvelope.

    Modes (all deterministic; the call count is the only state):

    - ``always`` — every call fails;
    - ``on_trigger`` — a call fails when ``trigger`` appears in
      the task's text;
    - ``every_nth`` — every Nth call fails (N >= 2);
    - ``first_n`` — the first N calls fail, the rest succeed:
      the transient-failure shape a retry policy is proven
      against.
    """

    kind = "failure"

    def __init__(
        self,
        *,
        mode: str = "always",
        error_code: str = "engine_error",
        trigger: str | None = None,
        n: int | None = None,
        detail: str = "SIMULATED failure",
        **kwargs,
    ):
        super().__init__(**kwargs)
        if mode not in _FAILURE_MODES:
            raise FakeEngineError(f"unknown failure mode: {mode}")
        if error_code not in _ERROR_CODES:
            raise FakeEngineError(f"unknown error_code: {error_code}")
        if mode == "on_trigger" and not trigger:
            raise FakeEngineError("on_trigger mode needs a trigger string")
        if mode == "every_nth" and (n is None or n < 2):
            raise FakeEngineError("every_nth mode needs n >= 2")
        if mode == "first_n" and (n is None or n < 1):
            raise FakeEngineError("first_n mode needs n >= 1")
        self.mode = mode
        self.error_code = error_code
        self.trigger = trigger
        self.n = n
        self.detail = detail

    def _failure_description(self) -> dict:
        return {
            "mode": self.mode,
            "error_code": self.error_code,
            "trigger": self.trigger,
            "n": self.n,
        }

    def _should_fail(self, task: dict) -> bool:
        if self.mode == "always":
            return True
        if self.mode == "on_trigger":
            return self.trigger in _payload_text(task.get("payload") or {})
        if self.mode == "every_nth":
            return self.calls % self.n == 0
        return self.calls <= self.n  # first_n

    def handle(self, task: dict, node=None) -> dict:
        self.calls += 1
        if self._should_fail(task):
            raise SimulatedTaskFailure(self.error_code, self.detail)
        return self._answer(task)


_KINDS = {"echo": EchoEngine, "latency": LatencyEngine, "failure": FailureEngine}


def make_fake_engine(kind: str, **config) -> FakeEngine:
    """Build one fake engine by name; an unknown name is refused,
    never guessed."""
    try:
        return _KINDS[kind](**config)
    except KeyError:
        raise FakeEngineError(
            f"unknown fake engine kind {kind!r}: one of {sorted(_KINDS)}"
        ) from None


def add_engine_node(simulator, node_id: str, engine: FakeEngine, **kwargs):
    """Raise one simulated node whose handler is ``engine``.

    The node's Y-014 fixture census is built with the engine's
    declared capabilities, so the tunnel's capability checks and
    the engine agree; census values remain fixture data (the
    engine descriptor, not the census, is the engine's truth).
    Extra keyword arguments pass through to ``add_fake_node``
    (models, roles, device class); the node's constant latency
    is only a fallback — the engine's per-task latency governs.
    """
    kwargs.setdefault("capabilities", engine.capabilities)
    return simulator.add_fake_node(node_id, handler=engine, **kwargs)
