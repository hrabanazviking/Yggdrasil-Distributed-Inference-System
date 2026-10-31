"""Node journal — Y-039.

Every earlier Part II slice *did* work and let the record of
it scatter: the sandbox kept its reports in its own list
(Y-029), the queue counted what it held and let go (Y-031),
the failure monitor sorted the accidents (Y-032), and the
health endpoint counted executions without remembering a
single one (Y-030). Nothing on the node could answer the
simplest ancestry question — *what have you run, on which
engine and model, how long did it take, and how did it end?*
This slice is that memory, kept on purpose:

- **One entry per task the node ran.** ``record_run`` takes
  the finished run's own three records — the task envelope
  (Y-007), the terminal ResultEnvelope (Y-007), and the
  sandbox report (Y-029) when one exists — and writes one
  validated ``journal_entry`` v1: task id, kind, caller,
  criticality, the context references it carried (as
  ``collection/id@version`` names, never their content),
  engine / model / quant / verification from the result's
  own provenance, the outcome status under its Y-007 name,
  the error code when it failed, the partial flag, the
  measured timings, and — when a sandbox report rides
  along — the measured tokens/chars out and the bound hit.
  The three records must agree: a result naming a different
  task, a provenance naming a different node, a sandbox
  report for another task or another node, or a sandbox
  status that disagrees with the result's are each refused
  by name. A journal that accepts mismatched records is a
  rumour book, not ancestry.
- **Facts, never content.** The entry carries identifiers,
  names, counts, and timings only. The task's payload and
  the result's text never enter the journal — proven by
  test, in the house pattern — so the journal can be handed
  to Urðr (Part V) without handing it the household's words.
- **Durable when the operator says so.** With no path, the
  journal is in-process memory and says so (``storage:
  "memory"``, ``durable: False``). With a ``path``, every
  entry is appended as one JSON line to that local file
  before ``record_run`` returns, and a journal opened on an
  existing file *loads* it: every line is parsed and
  validated, must name this node, and must continue the
  file's own sequence (0, 1, 2, …) exactly — a corrupt,
  foreign, renumbered, or gapped line is refused by line
  number, never silently skipped or truncated away, and
  new entries continue the sequence rather than
  renumbering history. The transport is a local file; no
  network path exists in this module.
- **The numbers, for the fitness memory to come.**
  ``report()`` answers the validated ``journal_report`` v1:
  totals by status / engine / model, the partial count,
  summed and mean measured run time, summed tokens/chars
  over the entries that carry measurements (entries
  recorded without a sandbox report are counted honestly
  as ``unmeasured_entries`` and contribute nothing), the
  first and last recorded moments, and the durability
  truth. Lookups by task, model, and engine return the
  entries in journal order — the raw material Y-078's
  fitness memory and Part V's decision ancestry (Y-102)
  and outcome ledger (Y-103) are views over.
- **Every entry is an event.** Each recording is recorded
  again as a validated Y-013 ``journal.task_recorded``
  EventEnvelope (status, engine, model, run time — names
  and counts only), so the node's observability stream and
  its journal can be cross-checked against each other.

The node agent (wired at this slice) grows the seam:
``node_journal=`` at construction, and ``execute_task``
journals every finished sandbox run — completed, failed,
cancelled, expired alike — after the failure monitor has
sorted it (Y-032). A run refused before it starts (the
governor's door, a non-resident model) never reaches the
sandbox and is *not* journaled: the journal is of tasks the
node ran, and a refusal at the door is the governor's own
event, not a run. ``journal_entries()`` / ``journal_report()``
read the book back, and ``status()`` carries the report.
All four citizen factories wire a journal (an in-memory one
by default; ``journal_path=`` makes it the durable file).

Honesty: the journal is this node's own book. It is not
Urðr — no graph, no replication, no cross-node ancestry
(Part V) — and it is not the task lifecycle journal of
Part IV (Y-075, the Verðandi role-holder's single-writer
log); it is the per-node raw material both will draw on.
Durability is exactly one local append-only file: no
fsync-per-entry guarantee is claimed beyond the append
itself, no rotation or compaction exists yet, and a file
an operator edits by hand is refused at load, not
repaired. Nothing here has run on named hardware beyond
the citizens' own suites; the citizens are Y-033 – Y-036.
"""

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.observability import make_event

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

_STATUSES = ("completed", "failed", "refused", "cancelled", "expired")


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


_ENTRY_VALIDATOR = _validator("journal_entry.schema.json")
_REPORT_VALIDATOR = _validator("journal_report.schema.json")
_TASK_VALIDATOR = _validator("task_envelope.schema.json")
_RESULT_VALIDATOR = _validator("result_envelope.schema.json")
_SANDBOX_VALIDATOR = _validator("sandbox_report.schema.json")


class JournalError(Exception):
    """Any node-journal failure: a bad record, a mismatched
    run, a corrupt or foreign journal file."""


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise JournalError(f"{kind} fails its schema: {errors[0].message}")
    return record


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_entry(entry: dict) -> dict:
    """A journal entry against its schema."""
    return _validate(_ENTRY_VALIDATOR, entry, "journal entry")


def validate_report(report: dict) -> dict:
    """A journal report against its schema."""
    return _validate(_REPORT_VALIDATOR, report, "journal report")


def _context_ref_names(task: dict) -> list[str]:
    """A task's context references as names — ``collection/id``
    with ``@v<version>`` when the reference carries one. The
    referenced content itself never enters the journal."""
    names = []
    for ref in task.get("context_refs") or []:
        name = f"{ref['collection']}/{ref['id']}"
        if ref.get("version") is not None:
            name += f"@v{ref['version']}"
        names.append(name)
    return names


class NodeJournal:
    """One node's journal: every task it ran, as validated
    entries, in the order they finished.

    ``path`` names the durable JSONL file (append-only); with
    no path the journal is in-process memory and its report
    says so. ``now`` is an injectable clock stamping each
    entry's ``recorded_at``.
    """

    def __init__(self, node_id: str, *, path: Path | str | None = None, now=None):
        if not isinstance(node_id, str) or not node_id:
            raise JournalError("a node journal needs a node_id")
        self.node_id = node_id
        self._path = Path(path) if path is not None else None
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._entries: list[dict] = []
        self._events: list[dict] = []
        self._event_seq = 0
        if self._path is not None:
            self._load()

    # -- facts ----------------------------------------------------------

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def durable(self) -> bool:
        return self._path is not None

    @property
    def storage(self) -> str:
        return "jsonl_file" if self._path is not None else "memory"

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    # -- durability -------------------------------------------------------

    def _load(self) -> None:
        """Load an existing journal file, strictly: every line
        a validated entry for this node, the sequence unbroken
        from 0. Anything else is refused by line number — a
        torn or tampered book is named, never quietly mended."""
        if not self._path.exists():
            return
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise JournalError(f"cannot read journal file {self._path}: {exc}") from exc
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                raise JournalError(
                    f"journal file {self._path} line {number} is not valid JSON: {exc}"
                ) from exc
            if not isinstance(entry, dict):
                raise JournalError(f"journal file {self._path} line {number} is not an entry")
            _validate(_ENTRY_VALIDATOR, entry, f"journal entry at line {number}")
            if entry["node_id"] != self.node_id:
                raise JournalError(
                    f"journal file {self._path} line {number} names node "
                    f"{entry['node_id']!r}, not {self.node_id!r} — a foreign book "
                    "is not this node's ancestry"
                )
            expected = len(self._entries)
            if entry["seq"] != expected:
                raise JournalError(
                    f"journal file {self._path} line {number} has seq {entry['seq']}, "
                    f"expected {expected} — the sequence is broken"
                )
            self._entries.append(entry)

    def _append(self, entry: dict) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
                handle.flush()
        except OSError as exc:
            raise JournalError(f"cannot append to journal file {self._path}: {exc}") from exc

    # -- internals --------------------------------------------------------

    def _emit(self, entry: dict) -> dict:
        event = make_event(
            type="journal.task_recorded",
            node_id=self.node_id,
            component="node_agent",
            payload={
                "seq": entry["seq"],
                "status": entry["status"],
                "engine": entry["engine"],
                "model": entry["model"],
                "run_ms": entry["timings_ms"]["run"],
                "error_code": entry["error_code"],
            },
            seq=self._event_seq,
            task_id=entry["task_id"],
            at=self._now(),
        )
        self._event_seq += 1
        self._events.append(event)
        return event

    # -- recording --------------------------------------------------------

    def record_run(self, task: dict, result: dict, sandbox_report: dict | None = None) -> dict:
        """Journal one finished run from its own records: the
        task envelope, its terminal result envelope, and the
        sandbox report when one exists. Returns the validated
        journal entry. The records must agree with each other
        and with this node — mismatches are refused by name,
        and nothing is written for a refused recording."""
        _validate(_TASK_VALIDATOR, task, "task envelope")
        _validate(_RESULT_VALIDATOR, result, "result envelope")
        if sandbox_report is not None:
            _validate(_SANDBOX_VALIDATOR, sandbox_report, "sandbox report")
        task_id = task["id"]
        if result["task_id"] != task_id:
            raise JournalError(
                f"result names task {result['task_id']}, task envelope names {task_id} — "
                "a run is journaled from records that agree"
            )
        provenance = result["provenance"]
        if provenance["node_id"] != self.node_id:
            raise JournalError(
                f"result provenance names node {provenance['node_id']!r}, "
                f"this journal belongs to {self.node_id!r}"
            )
        if sandbox_report is not None:
            if sandbox_report["task_id"] != task_id:
                raise JournalError(
                    f"sandbox report names task {sandbox_report['task_id']}, "
                    f"task envelope names {task_id}"
                )
            if sandbox_report["node_id"] != self.node_id:
                raise JournalError(
                    f"sandbox report names node {sandbox_report['node_id']!r}, "
                    f"this journal belongs to {self.node_id!r}"
                )
            if sandbox_report["status"] != result["status"]:
                raise JournalError(
                    f"sandbox report status {sandbox_report['status']!r} disagrees with "
                    f"result status {result['status']!r} for task {task_id}"
                )
            if (
                sandbox_report["error_code"] is not None
                and result.get("error") is not None
                and sandbox_report["error_code"] != result["error"]["code"]
            ):
                raise JournalError(
                    f"sandbox report error {sandbox_report['error_code']!r} disagrees with "
                    f"result error {result['error']['code']!r} for task {task_id}"
                )
        timings = result.get("timings_ms") or {}
        error_code = None
        if result.get("error") is not None:
            error_code = result["error"]["code"]
        elif sandbox_report is not None:
            error_code = sandbox_report["error_code"]
        with self._lock:
            entry = _validate(
                _ENTRY_VALIDATOR,
                {
                    "version": 1,
                    "seq": len(self._entries),
                    "task_id": task_id,
                    "node_id": self.node_id,
                    "kind": task["kind"],
                    "caller": task["caller"],
                    "criticality": task.get("criticality", "standard"),
                    "engine": provenance["engine"],
                    "model": provenance["model"],
                    "quant": provenance.get("quant"),
                    "verification": provenance.get("verification"),
                    "status": result["status"],
                    "error_code": error_code,
                    "partial": bool(result.get("partial", False)),
                    "bound_hit": sandbox_report["bound_hit"] if sandbox_report else None,
                    "timings_ms": {
                        "queue": float(timings.get("queue", 0.0)),
                        "run": float(timings.get("run", 0.0)),
                        "total": float(timings.get("total", 0.0)),
                    },
                    "tokens_out": sandbox_report["tokens_out"] if sandbox_report else None,
                    "chars_out": sandbox_report["chars_out"] if sandbox_report else None,
                    "context_refs": _context_ref_names(task),
                    "recorded_at": _fmt(self._now()),
                },
                "journal entry",
            )
            self._append(entry)
            self._entries.append(entry)
            self._emit(entry)
            return dict(entry)

    # -- reading the book back --------------------------------------------

    def entries(self) -> list[dict]:
        """Every entry, in journal (finish) order."""
        with self._lock:
            return [dict(entry) for entry in self._entries]

    def for_task(self, task_id: str) -> list[dict]:
        """Every entry for one task id — a retried task has
        more than one, and ancestry keeps them all."""
        with self._lock:
            return [dict(e) for e in self._entries if e["task_id"] == task_id]

    def for_model(self, model: str) -> list[dict]:
        """Every entry that ran on one model, in journal order."""
        with self._lock:
            return [dict(e) for e in self._entries if e["model"] == model]

    def for_engine(self, engine: str) -> list[dict]:
        """Every entry that ran on one engine, in journal order."""
        with self._lock:
            return [dict(e) for e in self._entries if e["engine"] == engine]

    # -- the numbers --------------------------------------------------------

    def report(self) -> dict:
        """The validated journal_report v1: this node's book,
        in numbers — totals by status, engine, and model, the
        measured run-time sums, and the durability truth."""
        with self._lock:
            by_status = dict.fromkeys(_STATUSES, 0)
            by_engine: dict[str, int] = {}
            by_model: dict[str, int] = {}
            partial_count = 0
            total_run_ms = 0.0
            total_tokens = 0
            total_chars = 0
            unmeasured = 0
            for entry in self._entries:
                by_status[entry["status"]] += 1
                by_engine[entry["engine"]] = by_engine.get(entry["engine"], 0) + 1
                by_model[entry["model"]] = by_model.get(entry["model"], 0) + 1
                if entry["partial"]:
                    partial_count += 1
                total_run_ms += entry["timings_ms"]["run"]
                if entry["tokens_out"] is None:
                    unmeasured += 1
                else:
                    total_tokens += entry["tokens_out"]
                    total_chars += entry["chars_out"]
            total = len(self._entries)
            return _validate(
                _REPORT_VALIDATOR,
                {
                    "version": 1,
                    "node_id": self.node_id,
                    "at": _fmt(self._now()),
                    "storage": self.storage,
                    "durable": self.durable,
                    "total_entries": total,
                    "by_status": by_status,
                    "by_engine": by_engine,
                    "by_model": by_model,
                    "partial_count": partial_count,
                    "total_run_ms": round(total_run_ms, 3),
                    "mean_run_ms": round(total_run_ms / total, 3) if total else None,
                    "total_tokens_out": total_tokens,
                    "total_chars_out": total_chars,
                    "unmeasured_entries": unmeasured,
                    "first_recorded_at": self._entries[0]["recorded_at"] if total else None,
                    "last_recorded_at": self._entries[-1]["recorded_at"] if total else None,
                    "last_seq": self._entries[-1]["seq"] if total else None,
                },
                "journal report",
            )


__all__ = [
    "JournalError",
    "NodeJournal",
    "validate_entry",
    "validate_report",
]
