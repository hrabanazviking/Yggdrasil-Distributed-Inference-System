"""Node soak — Y-047.

One node, 1,000 tasks, mixed engines — and afterwards, the
numbers that say whether the node *leaked* or *drifted*
while it worked. Every earlier Part II slice proved a
component on a handful of tasks; nothing had run a node
long enough for slow faults to show: a lease taken and
never returned, a sandbox execution left active, a queue
that quietly keeps what it served, records that multiply
beyond the tasks that made them, a completion rate or an
output shape that sags as the run goes on. This slice is
that long run, as a reusable harness — :class:`SoakHarness`
— plus the validated ``soak_report`` v1 it answers.

How a run goes:

- **The node is real, the engines are the fake ones.** The
  harness drives a joined :class:`NodeAgent` (identity,
  lifecycle, governor admission, task guard, failure
  monitor, journal — whatever the caller wired) through
  its own ``execute_task`` door, exactly as a caller
  would. The work itself is served by Y-015's three fake
  engines in rotation — echo, latency-shaped, and
  failure-shaped (every fifth of its own calls fails with
  ``engine_error``) — under three different engine names
  (``ollama``, ``aesir``, ``llamacpp``) and three model
  names, so the run mixes engines the way a multi-engine
  node (Y-045) does. Task kinds mix too: every 25th task
  is an ``embed.text`` batch, the rest ``chat.generate``,
  with prompt lengths cycling deterministically so any
  window's expected output shape is computable. Task ids
  are deterministic, the workload is a pure function of
  the policy's ``task_count`` — a fresh harness given the
  same policy deals the same 1,000 tasks in the same
  order, and the failure engine's tally is arithmetic,
  not luck.
- **Leak, defined in numbers.** After the last task the
  harness reads the node's residual operational state:
  agent in-flight leases, sandbox active executions, and
  queue depth must all be zero — a task the node still
  holds is a leak, by name. Retained records are *not*
  leaks, but they are counted exactly: the sandbox must
  hold precisely one report per task that reached it,
  and the journal (when wired) precisely one entry per
  such task — more is multiplication, less is loss, both
  are named ``leaked``. Process memory is reported
  honestly alongside: the peak-RSS watermark before /
  after / growth, a number for the operator — a peak
  watermark is not a leak proof and gates nothing.
- **Drift, defined in numbers.** Results are partitioned
  in run order into windows (``window_size`` per the
  ``soak_policy`` v1, 100 by default → ten windows). Each
  window is a validated ``soak_window`` v1: outcomes
  counted, completion rate, and the measured means
  (run ms from the sandbox's own report, tokens, chars).
  Drift compares the *first* window against the *last*:
  the completion-rate drift and the mean-output drift
  fraction must stay within the policy's tolerances or
  the verdict is ``drifted``. The mean-run-ms drift
  fraction is reported beside them (null when the first
  window's mean is zero — no fraction is invented over a
  zero base) but never gates: wall-clock means on a
  shared machine are weather, not character.
- **The verdict.** ``leaked`` outranks ``drifted``
  outranks ``stable`` — a node that leaks is named for
  the worse fault even when it also drifted.
  ``require_stable`` raises naming the verdict, for
  callers who want the gate in one call. A task refused
  *before* it starts (the governor's door, a
  non-resident model under a wired lifecycle) never
  reaches the sandbox: it is counted ``refused`` in the
  totals, and the retained-record expectations are
  reduced by exactly the refused count — the journal's
  own Y-039 rule, applied consistently. A harness handed
  a node whose wired lifecycle has not loaded the soak
  models refuses *before* the run rather than manufacturing
  1,000 door refusals and calling it a soak.

Honesty: everything here is SIMULATED — the report says
``simulated: true`` as a schema constant. The fake engines
perform no inference, the latencies are fixture shapes,
and no live Aesir / Ollama / llama.cpp server is claimed;
on-hardware soak belongs to Part VIII. No node agent seam
grows and no citizen factory is rewired at this slice:
the harness is run by whoever wants the evidence (the
suite, an operator, a later gate), and the report is the
evidence. The Part II gate itself — three real citizens
running the conformance kit against their real engines —
is Y-048, and it does not inherit this slice's word for
anything.
"""

import json
import resource
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.fake_engines import EchoEngine, FailureEngine, LatencyEngine
from yggdrasil.observability import make_event
from yggdrasil.sandbox import result_handler_runner

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

# The mixed workload's rotation: (engine name the provenance
# carries, model name, fake engine kind). The failure engine
# fails every fifth of *its own* calls, so its tally over a
# run is floor(calls / 5) — arithmetic the tests check.
_ROTATION = (
    ("ollama", "soak-echo", "echo"),
    ("aesir", "soak-latency", "latency"),
    ("llamacpp", "soak-failure", "failure"),
)
_FAILURE_EVERY_NTH = 5

_STATUSES = ("completed", "failed", "refused", "cancelled", "expired")


def _load_schema(name: str) -> dict:
    return json.loads((_SCHEMAS / name).read_text())


def _validator(name: str):
    """A validator for one schema, with every contract schema
    registered so relative $refs resolve — the registry
    pattern the earlier slices build."""
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


_POLICY_VALIDATOR = _validator("soak_policy.schema.json")
_WINDOW_VALIDATOR = _validator("soak_window.schema.json")
_REPORT_VALIDATOR = _validator("soak_report.schema.json")


class SoakError(Exception):
    """The harness's own failure: its misuse, a record that
    fails its schema, or — through ``require_stable`` — a
    run whose verdict was not stable."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise SoakError(f"{kind} fails its schema: {errors[0].message}")
    return record


def default_policy() -> dict:
    """The roadmap's soak, as data: 1,000 tasks in windows
    of 100, a completion rate that may drift by at most a
    tenth, a mean output that may drift by at most a
    quarter. A policy, labelled as one — not a fact about
    any node."""
    return {
        "version": 1,
        "task_count": 1000,
        "window_size": 100,
        "max_completion_rate_drift": 0.1,
        "max_output_drift_fraction": 0.25,
    }


def validate_policy(record: dict) -> dict:
    return _validate(_POLICY_VALIDATOR, record, "soak policy")


def validate_window(record: dict) -> dict:
    return _validate(_WINDOW_VALIDATOR, record, "soak window")


def validate_report(record: dict) -> dict:
    return _validate(_REPORT_VALIDATOR, record, "soak report")


def _peak_rss_kb() -> int:
    """This process's peak resident set size, in KiB (the
    Linux reading of ``ru_maxrss``) — a watermark, reported
    as a number and claimed as nothing more."""
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def _task_for(index: int) -> dict:
    """The workload's task at ``index`` — a pure function,
    so the whole run replays identically on a fresh
    harness. Every 25th task embeds; the rest generate,
    with prompt lengths cycling over ten values so every
    full window carries the same expected output shape."""
    task_id = f"00000000-0000-4000-8000-{index:012d}"
    if index % 25 == 24:
        return {
            "id": task_id,
            "kind": "embed.text",
            "caller": "y-047-soak",
            "payload": {"texts": [f"soak text {index}", f"soak text {index + 1}"]},
        }
    return {
        "id": task_id,
        "kind": "chat.generate",
        "caller": "y-047-soak",
        "payload": {"prompt": f"soak task {index} " + "x" * ((index % 10) * 4)},
    }


def _fresh_engines() -> dict:
    """One fresh fake engine per rotation entry, keyed by
    engine name — fresh, because the failure engine's call
    count is its rule, and a reused engine would carry one
    run's tally into the next."""
    engines = {}
    for engine_name, _model, kind in _ROTATION:
        if kind == "echo":
            engines[engine_name] = EchoEngine()
        elif kind == "latency":
            engines[engine_name] = LatencyEngine()
        else:
            engines[engine_name] = FailureEngine(
                mode="every_nth", n=_FAILURE_EVERY_NTH, error_code="engine_error"
            )
    return engines


class SoakHarness:
    """One node's long run. ``agent`` is a joined
    :class:`NodeAgent` with a task sandbox wired (the
    caller's wiring is the node's truth, exactly as in
    Y-046); ``policy`` is the ``soak_policy`` v1 the run
    obeys (the roadmap's 1,000-task default when none is
    handed in); ``now`` is injectable so timestamps are
    testable."""

    def __init__(self, agent, *, policy: dict | None = None, now=None):
        if agent is None:
            raise SoakError("the soak harness needs a node agent to run")
        self._agent = agent
        self._policy = validate_policy(
            dict(policy) if policy is not None else default_policy()
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._events: list[dict] = []
        self._seq = 0

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    @property
    def policy(self) -> dict:
        return dict(self._policy)

    # -- preflight -----------------------------------------------------------

    def _preflight(self) -> None:
        agent = self._agent
        if agent.sandbox is None:
            raise SoakError("the soak node has no task sandbox wired")
        if agent.state != "joined":
            raise SoakError(
                f"the soak node must be joined before the run, not {agent.state}"
            )
        lifecycle = agent.model_lifecycle
        if lifecycle is not None:
            resident = set(lifecycle.loaded_models())
            needed = {model for _engine, model, _kind in _ROTATION}
            missing = sorted(needed - resident)
            if missing:
                raise SoakError(
                    "the soak node's lifecycle has not loaded the soak "
                    f"models: {', '.join(missing)} — load them first, or "
                    "run the soak on a node without residency gating"
                )

    # -- the run ---------------------------------------------------------------

    def run(self) -> dict:
        """Deal the policy's tasks to the one node, in
        order, and answer the validated ``soak_report`` v1.
        A task the agent refuses before it starts is counted
        refused and the run continues — a soak that stopped
        at its first refusal would measure nothing."""
        self._preflight()
        agent = self._agent
        sandbox = agent.sandbox
        task_count = self._policy["task_count"]
        engines = _fresh_engines()
        runners = {
            name: result_handler_runner(engine) for name, engine in engines.items()
        }
        journal_before = (
            len(agent.journal_entries()) if agent.node_journal is not None else None
        )
        reports_before = len(sandbox.reports)
        rss_before = _peak_rss_kb()

        records: list[dict] = []
        for index in range(task_count):
            engine_name, model, _kind = _ROTATION[index % len(_ROTATION)]
            task = _task_for(index)
            try:
                result, _stream, sandbox_report = agent.execute_task(
                    task, runners[engine_name], engine=engine_name, model=model
                )
                records.append(
                    {
                        "engine": engine_name,
                        "status": result["status"],
                        "partial": bool(result["partial"]),
                        "run_ms": float(sandbox_report["run_ms"]),
                        "tokens_out": int(sandbox_report["tokens_out"]),
                        "chars_out": int(sandbox_report["chars_out"]),
                    }
                )
            except Exception:  # noqa: BLE001 — a door refusal is the run's data, counted refused under the agent's own name for it
                records.append(
                    {
                        "engine": engine_name,
                        "status": "refused",
                        "partial": False,
                        "run_ms": 0.0,
                        "tokens_out": 0,
                        "chars_out": 0,
                    }
                )

        rss_after = _peak_rss_kb()
        queue_report = agent.queue_report()
        residuals = {
            "in_flight": len(agent.in_flight),
            "sandbox_active": len(sandbox.active_task_ids),
            "queue_depth": queue_report["depth"] if queue_report is not None else None,
            "journal_entries": (
                len(agent.journal_entries()) - journal_before
                if journal_before is not None
                else None
            ),
            "sandbox_reports": len(sandbox.reports) - reports_before,
        }
        totals = {status: 0 for status in _STATUSES}
        totals["partial"] = 0
        for record in records:
            totals[record["status"]] += 1
            if record["partial"]:
                totals["partial"] += 1
        by_engine: dict[str, dict] = {}
        for engine_name, _model, _kind in _ROTATION:
            mine = [r for r in records if r["engine"] == engine_name]
            by_engine[engine_name] = {
                "tasks": len(mine),
                "completed": sum(1 for r in mine if r["status"] == "completed"),
                "failed": sum(1 for r in mine if r["status"] == "failed"),
                "refused": sum(1 for r in mine if r["status"] == "refused"),
            }
        windows = self._windows(records)
        drift = self._drift(windows)
        verdict = self._verdict(totals, residuals, drift, task_count)
        report = _validate(
            _REPORT_VALIDATOR,
            {
                "version": 1,
                "node_id": agent.node_id,
                "at": _fmt(self._now()),
                "simulated": True,
                "policy": self._policy,
                "totals": {"tasks": task_count, **totals},
                "by_engine": by_engine,
                "windows": windows,
                "drift": drift,
                "residuals": residuals,
                "memory": {
                    "peak_rss_before_kb": rss_before,
                    "peak_rss_after_kb": rss_after,
                    "peak_rss_growth_kb": max(rss_after - rss_before, 0),
                },
                "verdict": verdict,
            },
            "soak report",
        )
        self._events.append(
            make_event(
                type=f"soak.{verdict}",
                node_id=agent.node_id,
                component="node_agent",
                payload={
                    "tasks": task_count,
                    "completed": totals["completed"],
                    "failed": totals["failed"],
                    "refused": totals["refused"],
                    "verdict": verdict,
                    "simulated": True,
                },
                seq=self._seq,
                at=self._now(),
            )
        )
        self._seq += 1
        return report

    # -- the numbers -----------------------------------------------------------

    def _windows(self, records: list[dict]) -> list[dict]:
        size = self._policy["window_size"]
        windows = []
        for start in range(0, len(records), size):
            chunk = records[start : start + size]
            counts = {status: 0 for status in _STATUSES}
            partial = 0
            for record in chunk:
                counts[record["status"]] += 1
                if record["partial"]:
                    partial += 1
            n = len(chunk)
            windows.append(
                _validate(
                    _WINDOW_VALIDATOR,
                    {
                        "version": 1,
                        "index": len(windows),
                        "tasks": n,
                        "completed": counts["completed"],
                        "failed": counts["failed"],
                        "refused": counts["refused"],
                        "cancelled": counts["cancelled"],
                        "expired": counts["expired"],
                        "partial": partial,
                        "completion_rate": round(counts["completed"] / n, 6),
                        "mean_run_ms": round(sum(r["run_ms"] for r in chunk) / n, 3),
                        "mean_tokens_out": round(
                            sum(r["tokens_out"] for r in chunk) / n, 3
                        ),
                        "mean_chars_out": round(
                            sum(r["chars_out"] for r in chunk) / n, 3
                        ),
                    },
                    "soak window",
                )
            )
        return windows

    @staticmethod
    def _drift_fraction(first: float, last: float) -> float:
        if first == 0:
            return 0.0 if last == 0 else 1.0
        return round(abs(last - first) / first, 6)

    def _drift(self, windows: list[dict]) -> dict:
        first, last = windows[0], windows[-1]
        return {
            "completion_rate_drift": round(
                abs(last["completion_rate"] - first["completion_rate"]), 6
            ),
            "mean_chars_out_drift_fraction": self._drift_fraction(
                first["mean_chars_out"], last["mean_chars_out"]
            ),
            "mean_run_ms_drift_fraction": (
                None
                if first["mean_run_ms"] == 0
                else self._drift_fraction(first["mean_run_ms"], last["mean_run_ms"])
            ),
        }

    def _verdict(
        self, totals: dict, residuals: dict, drift: dict, task_count: int
    ) -> str:
        # The reached count is every task minus the door
        # refusals, which never reach the sandbox or the
        # journal (Y-039's rule).
        reached = task_count - totals["refused"]
        leaked = (
            residuals["in_flight"] != 0
            or residuals["sandbox_active"] != 0
            or residuals["queue_depth"] not in (None, 0)
            or residuals["sandbox_reports"] != reached
            or (
                residuals["journal_entries"] is not None
                and residuals["journal_entries"] != reached
            )
        )
        if leaked:
            return "leaked"
        drifted = (
            drift["completion_rate_drift"] > self._policy["max_completion_rate_drift"]
            or drift["mean_chars_out_drift_fraction"]
            > self._policy["max_output_drift_fraction"]
        )
        return "drifted" if drifted else "stable"


def run_soak(agent, **kwargs) -> dict:
    """One node's soak report, in one call — the harness
    built, run, and answered. ``kwargs`` are
    :class:`SoakHarness`'s."""
    return SoakHarness(agent, **kwargs).run()


def require_stable(agent, **kwargs) -> dict:
    """The soak gate in one call: the report when the run
    was stable, :class:`SoakError` naming the verdict when
    it was not."""
    report = run_soak(agent, **kwargs)
    if report["verdict"] != "stable":
        raise SoakError(
            f"the soak of node {report['node_id']!r} is {report['verdict']}, "
            "not stable — see the report's residuals and drift"
        )
    return report


__all__ = [
    "SoakError",
    "SoakHarness",
    "default_policy",
    "require_stable",
    "run_soak",
    "validate_policy",
    "validate_report",
    "validate_window",
]
