"""Task execution sandbox — Y-029.

Everything before this slice *admits* work: the lifecycle
(Y-021) leases it, the governor (Y-028) refuses it gracefully
when the node is hot or pressed, the lifecycle manager (Y-027)
holds the model it runs against. This slice is where the work
actually *runs* — inside bounds, so a runaway, stuck, or
merely greedy task cannot take the node with it:

- **Bounds as data.** A ``SandboxPolicy`` (schema v1) names one
  node's execution bounds: ``max_run_ms`` (wall time per task,
  measured on an injectable monotonic timer), ``max_output_tokens``
  and ``max_output_chars`` (the output ceiling, counted as it
  streams). A task may only *tighten* them: its Y-007
  ``deadline_ms`` caps the time bound, and ``payload.max_tokens``
  / ``payload.max_output_chars`` cap the output bounds — a task
  can never loosen its node's policy by asking.
- **Token counting is named, never pretended.** Two counters,
  chosen in the policy: ``per_chunk`` (each chunk the runner
  yields is one token unit — the shape engine adapters stream
  in, one event per token piece) and ``whitespace_words``
  (whitespace-separated words in the text). Neither is a real
  tokeniser; the policy says which approximation is in force,
  and the report carries the counts under that counter.
- **Streamed partial results.** The runner yields text chunks;
  every chunk becomes a Y-007 ``token`` StreamEvent the moment
  it arrives (an ``on_event`` callback receives each in turn),
  so a caller holds partial output from the start — and keeps
  it, marked ``partial``, when a bound, a cancellation, or a
  failure cuts the run short.
- **Bounds bite mid-stream.** When the output ceiling is
  reached, the crossing chunk is truncated to the room left
  (or dropped when no room is left), the runner is closed,
  and the task completes with ``partial: True`` and the report
  naming the bound hit. When the time bound is reached the
  task ends ``expired`` with ``deadline_unmeetable`` — the
  honest name for "it did not finish in its time". The time
  bound is *cooperative*: it is checked between chunks, so a
  runner that never yields cannot be interrupted by this
  layer (killing a wedged engine process is Y-032's failure
  shapes and the deployment's supervisor, not claimed here).
- **Cancellable.** ``cancel(task_id)`` from any thread — or
  from an ``on_event`` callback — flags the active execution;
  the loop sees it between chunks, closes the runner (its
  ``finally`` blocks run: cleanup is the runner's, the close
  is guaranteed), and the task ends ``cancelled`` with
  ``cancelled_by_caller`` and whatever text already streamed.
  Cancelling an unknown or finished task returns False.
- **Failures keep their names.** A runner raising
  ``SimulatedTaskFailure`` (Y-014/Y-015) or any exception
  carrying a Y-007 ``error_code`` (the adapters' shape) ends
  ``failed`` under that code; any other exception ends
  ``failed`` as ``engine_error`` with its message as detail.
  Text streamed before the failure is kept, partial.

The runner protocol: ``runner(task, control)`` returns an
iterator of text chunks (strings), or a single result dict
(whose ``text`` streams as one chunk and whose other fields
ride into the result). ``control`` is the execution's
``SandboxControl`` — a runner may poll ``control.cancelled``
inside long stretches between yields. Two adapters bridge the
tree's existing shapes into the protocol:
``result_handler_runner`` wraps a dict-returning handler (the
simulator / fake-engine shape), and ``adapter_generate_runner``
wraps an engine adapter's ``generate()`` pair stream
(Y-023 – Y-025), yielding its token events' text.

Every execution leaves a validated ``sandbox_report`` v1 —
bounds applied (the policy's, tightened by the task's), the
measured run time, tokens/chars out, the bound hit if any —
and a Y-013 EventEnvelope from the ``node_agent`` component
in the sandbox's own ``events``. The node agent (wired at
this slice) runs tasks through ``execute_task``: its lease
(and the governor's admission) first, a model-lifecycle use
lease (Y-027) for the run's duration when one is wired, and
both released whatever the outcome.

Honesty: the sandbox bounds the *stream it is handed* on this
machine, in-process. It is not an OS sandbox — no process
isolation, no syscall or filesystem confinement (tasks are
data, never code, is Y-043's enforcement; engine crashes and
OOM containment are Y-032). Nothing here has run on named
hardware; the citizens are Y-033 – Y-035.
"""

import contextlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.observability import make_event
from yggdrasil.simulator import SimulatedTaskFailure

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

# An operator's starting bounds, not a fact about any machine:
# two minutes, 4096 token units, 64 KiB of text per task.
DEFAULT_POLICY = {
    "version": 1,
    "max_run_ms": 120_000,
    "max_output_tokens": 4096,
    "max_output_chars": 65_536,
    "token_counting": "per_chunk",
}

_ERROR_CODES = set(
    json.loads((_SCHEMAS / "error_codes.schema.json").read_text())["$defs"]["error_code"]["enum"]
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


_POLICY_VALIDATOR = _validator("sandbox_policy.schema.json")
_REPORT_VALIDATOR = _validator("sandbox_report.schema.json")
_TASK_VALIDATOR = _validator("task_envelope.schema.json")
_RESULT_VALIDATOR = _validator("result_envelope.schema.json")
_STREAM_VALIDATOR = _validator("stream_event.schema.json")


class SandboxError(Exception):
    """Any sandbox failure: a bad policy, a malformed task, a
    duplicate execution, an unshaped runner."""


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise SandboxError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_policy(policy: dict) -> dict:
    """A sandbox policy against its schema (positivity rides
    on the schema's own bounds)."""
    return _validate(_POLICY_VALIDATOR, policy, "sandbox policy")


def default_policy() -> dict:
    """The starting bounds, validated — an operator's policy,
    not a measured fact about any machine."""
    return validate_policy(dict(DEFAULT_POLICY))


class SandboxControl:
    """One execution's cancellation flag, shared between the
    sandbox loop, the runner, and any cancelling thread."""

    def __init__(self):
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True


def _token_cost(chunk: str, counting: str) -> int:
    if counting == "per_chunk":
        return 1 if chunk else 0
    return len(chunk.split())


def _truncate_to_tokens(chunk: str, room: int, counting: str) -> str:
    """Shorten a chunk to at most ``room`` token units under
    the named counter. Per-chunk counting cannot shorten a
    chunk (it is one unit), so room 0 drops it entirely;
    whitespace counting keeps the first ``room`` words, joined
    by single spaces — the truncation is a cut, not a copy."""
    if room <= 0:
        return ""
    if counting == "per_chunk":
        return chunk
    words = chunk.split()
    if len(words) <= room:
        return chunk
    return " ".join(words[:room])


class TaskSandbox:
    """One node's task execution sandbox: bounds, streaming,
    cancellation, and a measured report per execution.

    ``timer`` is a monotonic-seconds callable (the adapters'
    seam); ``now`` stamps the Y-013 events. Both injectable so
    bounds are provable without real waiting.
    """

    def __init__(self, node_id: str, policy: dict | None = None, *, timer=None, now=None):
        if not isinstance(node_id, str) or not node_id:
            raise SandboxError("a sandbox needs a node_id")
        self.node_id = node_id
        self._policy = validate_policy(dict(policy) if policy is not None else default_policy())
        self._timer = timer or time.monotonic
        self._now = now or (lambda: datetime.now(UTC))
        self._active: dict[str, SandboxControl] = {}
        self._events: list[dict] = []
        self._seq = 0
        self._reports: list[dict] = []

    # -- facts ----------------------------------------------------------

    @property
    def policy(self) -> dict:
        return dict(self._policy)

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    @property
    def reports(self) -> list[dict]:
        """Every execution's SandboxReport, in finish order."""
        return list(self._reports)

    @property
    def active_task_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._active))

    # -- cancellation -----------------------------------------------------

    def cancel(self, task_id: str) -> bool:
        """Flag one active execution to stop between chunks.
        False when no such execution is running — a finished
        task cannot be cancelled retroactively."""
        control = self._active.get(task_id)
        if control is None:
            return False
        control.cancel()
        return True

    # -- internals --------------------------------------------------------

    def _emit(self, type: str, payload: dict, *, task_id: str) -> dict:
        event = make_event(
            type=type,
            node_id=self.node_id,
            component="node_agent",
            payload=payload,
            seq=self._seq,
            task_id=task_id,
            at=self._now(),
        )
        self._seq += 1
        self._events.append(event)
        return event

    def _stream(self, task_id: str, seq: int, event: str, data=None) -> dict:
        record: dict = {"task_id": task_id, "seq": seq, "event": event}
        if data is not None:
            record["data"] = data
        return _validate(_STREAM_VALIDATOR, record, "stream event")

    def _bounds_for(self, task: dict) -> dict:
        """The policy's bounds, tightened by anything the task
        itself declares — never loosened by it."""
        bounds = {
            "max_run_ms": self._policy["max_run_ms"],
            "max_output_tokens": self._policy["max_output_tokens"],
            "max_output_chars": self._policy["max_output_chars"],
            "deadline_ms": None,
        }
        deadline = task.get("deadline_ms")
        if isinstance(deadline, int) and deadline > 0:
            bounds["deadline_ms"] = deadline
            bounds["max_run_ms"] = min(bounds["max_run_ms"], deadline)
        payload = task.get("payload") or {}
        for key, bound in (
            ("max_tokens", "max_output_tokens"),
            ("max_output_tokens", "max_output_tokens"),
            ("max_output_chars", "max_output_chars"),
        ):
            value = payload.get(key)
            if isinstance(value, int) and value > 0:
                bounds[bound] = min(bounds[bound], value)
        return bounds

    # -- execution --------------------------------------------------------

    def run(self, task: dict, runner, *, engine: str, model: str, on_event=None):
        """Run one task inside the bounds; return
        (ResultEnvelope, StreamEvents, SandboxReport).

        ``runner`` follows the module's protocol; ``engine``
        and ``model`` name the provenance the result carries —
        the sandbox runs what it is handed and claims no engine
        it did not see. ``on_event`` receives each StreamEvent
        as it is produced (and may call :meth:`cancel`).
        """
        _validate(_TASK_VALIDATOR, task, "task envelope")
        if not callable(runner):
            raise SandboxError("a sandbox runner must be callable")
        if not isinstance(engine, str) or not engine:
            raise SandboxError("a sandboxed run names its engine for provenance")
        if not isinstance(model, str) or not model:
            raise SandboxError("a sandboxed run names its model for provenance")
        task_id = task["id"]
        if task_id in self._active:
            raise SandboxError(f"task {task_id} is already executing in this sandbox")
        bounds = self._bounds_for(task)
        counting = self._policy.get("token_counting", "per_chunk")
        control = SandboxControl()
        self._active[task_id] = control

        stream: list[dict] = []
        seq = 0

        def emit(event: str, data=None) -> dict:
            nonlocal seq
            record = self._stream(task_id, seq, event, data)
            seq += 1
            stream.append(record)
            if on_event is not None:
                on_event(record)
            return record

        started = self._timer()
        texts: list[str] = []
        extra: dict = {}
        tokens_out = 0
        chars_out = 0
        status = "completed"
        bound_hit: str | None = None
        error: dict | None = None
        iterator = None

        def elapsed_ms() -> float:
            return (self._timer() - started) * 1000.0

        try:
            emit("accepted")
            outcome = runner(task, control)
            if isinstance(outcome, dict):
                # A single result dict: its text is one chunk,
                # its other fields ride into the result.
                extra = {k: v for k, v in outcome.items() if k != "text"}
                text = outcome.get("text")
                iterator = iter([text] if isinstance(text, str) and text else [])
            else:
                iterator = iter(outcome)
            emit("started")
            while True:
                if control.cancelled:
                    status, bound_hit = "cancelled", "cancelled"
                    error = {
                        "code": "cancelled_by_caller",
                        "detail": "the caller cancelled this task; partial output kept",
                    }
                    break
                if elapsed_ms() > bounds["max_run_ms"]:
                    status, bound_hit = "expired", "time"
                    error = {
                        "code": "deadline_unmeetable",
                        "detail": (
                            f"the task passed its {bounds['max_run_ms']} ms bound "
                            "between chunks; partial output kept"
                        ),
                    }
                    break
                try:
                    chunk = next(iterator)
                except StopIteration:
                    # The runner is done — but a runner that
                    # cancelled itself (or was cancelled) and
                    # then returned has still been cancelled:
                    # the flag outranks the clean return.
                    if control.cancelled:
                        status, bound_hit = "cancelled", "cancelled"
                        error = {
                            "code": "cancelled_by_caller",
                            "detail": "the caller cancelled this task; partial output kept",
                        }
                    break
                if elapsed_ms() > bounds["max_run_ms"]:
                    # The chunk arrived too late to be the caller's:
                    # it is dropped, not streamed.
                    status, bound_hit = "expired", "time"
                    error = {
                        "code": "deadline_unmeetable",
                        "detail": (
                            f"the task passed its {bounds['max_run_ms']} ms bound "
                            "mid-run; partial output kept"
                        ),
                    }
                    break
                if not isinstance(chunk, str):
                    raise SandboxError(
                        f"a sandbox runner yields text chunks (str), not {type(chunk).__name__}"
                    )
                # The output bounds, applied to this chunk before
                # it becomes the caller's.
                room_chars = bounds["max_output_chars"] - chars_out
                if len(chunk) > room_chars:
                    chunk = chunk[:room_chars] if room_chars > 0 else ""
                    if bound_hit is None:
                        bound_hit = "output_chars"
                cost = _token_cost(chunk, counting)
                room_tokens = bounds["max_output_tokens"] - tokens_out
                if cost > room_tokens:
                    chunk = _truncate_to_tokens(chunk, room_tokens, counting)
                    cost = _token_cost(chunk, counting)
                    if bound_hit is None:
                        bound_hit = "tokens"
                if not chunk:
                    # No room, or nothing left after truncation:
                    # the run is complete at its bound.
                    if bound_hit is None:
                        bound_hit = "output_chars" if room_chars <= 0 else "tokens"
                    break
                texts.append(chunk)
                tokens_out += cost
                chars_out += len(chunk)
                emit("token", {"text": chunk})
                if bound_hit is not None:
                    break
        except SimulatedTaskFailure as failure:
            status = "failed"
            error = {"code": failure.error_code, "detail": failure.detail}
        except SandboxError:
            raise
        except Exception as exc:  # noqa: BLE001 — catching runner failures is the sandbox's job: each keeps its Y-007 name, or engine_error
            status = "failed"
            code = getattr(exc, "error_code", None)
            if code not in _ERROR_CODES:
                code = "engine_error"
            error = {"code": code, "detail": str(exc) or code}
        finally:
            if iterator is not None and hasattr(iterator, "close"):
                # The runner's cleanup failing changes no verdict.
                with contextlib.suppress(Exception):
                    iterator.close()
            self._active.pop(task_id, None)

        run_ms = round(max(elapsed_ms(), 0.0), 3)
        partial = bool(texts) and (status != "completed" or bound_hit is not None)
        result_record: dict = {
            "task_id": task_id,
            "status": status,
            "provenance": {"node_id": self.node_id, "engine": engine, "model": model},
            "timings_ms": {"queue": 0.0, "run": run_ms, "total": run_ms},
            "partial": partial,
        }
        if error is not None:
            result_record["error"] = error
        if texts or status == "completed":
            body: dict = {
                "text": "".join(texts),
                "tokens_out": tokens_out,
                "chars_out": chars_out,
                "engine": engine,
            }
            body.update(extra)
            result_record["result"] = body
        _validate(_RESULT_VALIDATOR, result_record, "result envelope")
        emit("completed" if status == "completed" else "failed")

        report = _validate(
            _REPORT_VALIDATOR,
            {
                "version": 1,
                "task_id": task_id,
                "node_id": self.node_id,
                "status": status,
                "bounds": bounds,
                "tokens_out": tokens_out,
                "chars_out": chars_out,
                "run_ms": run_ms,
                "bound_hit": bound_hit,
                "partial": partial,
                "error_code": error["code"] if error else None,
            },
            "sandbox report",
        )
        self._reports.append(report)
        self._emit(
            f"sandbox.task_{status}",
            {
                "status": status,
                "bound_hit": bound_hit,
                "tokens_out": tokens_out,
                "chars_out": chars_out,
                "run_ms": run_ms,
                "partial": partial,
                "error_code": error["code"] if error else None,
            },
            task_id=task_id,
        )
        return result_record, stream, report


def result_handler_runner(handler):
    """Wrap a dict-returning handler — the simulator /
    fake-engine shape, ``handler(task)`` or ``handler(task,
    node)`` — as a sandbox runner. The handler's result dict
    is the run's outcome (its ``text`` streams as one chunk,
    its other fields ride into the result). A handler raising
    ``SimulatedTaskFailure`` fails the task under its code."""

    def runner(task, control):
        del control  # a one-shot handler has no in-run moment to poll
        try:
            outcome = handler(task)
        except TypeError:
            outcome = handler(task, None)
        if not isinstance(outcome, dict):
            raise SandboxError("a result handler must return a result object")
        return outcome

    return runner


def adapter_generate_runner(adapter, node_id: str):
    """Wrap an engine adapter's ``generate()`` (Y-023 – Y-025:
    yields (stream_event, result) pairs) as a sandbox runner:
    its token events' text streams through the sandbox, and
    the adapter's own terminal result is superseded by the
    sandbox's bounded one — the bounds, not the adapter,
    decide when this run ends. Adapter failures raise through
    with their Y-007 ``error_code`` attached."""

    def runner(task, control):
        del control  # cancellation lands between the adapter's chunks
        for event, _result in adapter.generate(task, node_id):
            if event is not None and event.get("event") == "token":
                text = (event.get("data") or {}).get("text")
                if isinstance(text, str) and text:
                    yield text

    return runner
