"""Capability census — Y-022.

The node contract (Y-006) names the shape: what a node truly
has — CPU, RAM, accelerators (CUDA, Hailo, NPU, none), disk,
thermals — as a validated ``CapabilityCensus``. The simulator
(Y-014) has carried that shape with SIMULATED fixture values
since the wind tunnel opened; this slice is the real measurer
the census title has pointed at all along ("Y-022 produces
this", in the schema's own words).

Measured, not assumed — the slice's whole discipline:

- every number in a census comes from a *probe*: a small
  function that reads the machine (``/proc/meminfo`` for RAM,
  ``/proc/cpuinfo`` for the CPU model, ``thermal_zone`` sensors
  for heat, device nodes under ``/dev`` and ``/proc/driver``
  for accelerators, ``shutil.disk_usage`` for disk). A value no
  probe could read is *absent* from the census, never guessed:
  a VM with no thermal sensor reports no ``thermal_c`` field,
  and a node with no accelerator device node reports exactly
  one accelerator, ``{"kind": "none"}``.
- detection is not proof. An accelerator entry's ``proven``
  flag stays False for everything this module detects: a
  device node existing is a measured fact about presence, not
  a proven inference run. Proof belongs to the engines' own
  ledgers (Y-024 for Aesir) and to hardware runs; the census
  never promises it early.
- engines and models are reported only as far as they exist:
  no engine adapter lands until Y-023 and no model store until
  Y-026, so a bare census carries an empty engine list and an
  empty ``models_present`` unless the caller hands in entries
  it measured itself (each one validated against the Y-008
  engine-capability schema before it enters the census).

Refreshed on change: a ``CensusCollector`` holds a node's
current census and re-measures on ``refresh()``. The refresh
returns the new census plus the names of the top-level fields
that actually changed (``measured_at`` alone never counts —
time passing is not a hardware change), so the node agent can
announce a real change (a GPU appearing, a thermal reading
arriving, a disk filling is *reported*, through the field
diff) instead of a schedule. ``census_fingerprint`` hashes a
census without its timestamp for the same comparison.

Probes are injectable: the system probe set reads a filesystem
root (``/`` by default), and tests point it at a fake root — a
tmp tree holding ``proc/``, ``sys/`` and ``dev/`` files shaped
like a machine with a GPU and a Hailo — or replace the whole
``ProbeSet`` with lambdas. There is deliberately no process
client in this module: probes read files and stdlib counters
only, never shell out to a vendor tool whose output would have
to be trusted instead of measured.

Honesty: a census measured on this VM is a fact about this VM
at that moment. It proves the measuring logic; it is not a
citizen proof for the Pi, the laptop, or any other device —
those are Y-033 – Y-035, each measured on its own hardware.
"""

import hashlib
import json
import os
import platform
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

ACCELERATOR_KINDS = ("cuda", "hailo", "npu", "metal", "none")


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


_CENSUS_VALIDATOR = _validator("capability_census.schema.json")
_ENGINE_VALIDATOR = _validator("engine_capability.schema.json")


class CensusError(Exception):
    """Any census failure: a probe that could not read, or a
    measured record that fails its contract schema."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise CensusError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_census(record: dict) -> dict:
    """A census record against the Y-006 contract schema."""
    return _validate(_CENSUS_VALIDATOR, record, "capability census")


# -- probes -------------------------------------------------------------
#
# A ProbeSet is the measuring hand: six small callables. The
# system set below reads a real machine through a filesystem
# root; tests inject a set of their own, or point the system
# set at a fake root shaped like another machine entirely.


@dataclass(frozen=True)
class ProbeSet:
    """The six measurements a census is built from.

    ``cpu`` returns ``{"arch": str, "cores": int}`` plus a
    ``"model"`` entry when the machine names one. The optional
    probes (disk, thermal, vram) return None when the machine
    offers no such reading — the census then omits the field
    rather than inventing a number.
    """

    cpu: Callable[[], dict]
    ram_gb: Callable[[], float]
    accelerators: Callable[[], list]
    disk_free_gb: Callable[[], float | None] | None = None
    thermal_c: Callable[[], float | None] | None = None
    vram_gb: Callable[[], float | None] | None = None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


def _system_cpu(root: Path) -> dict:
    cpu: dict = {"arch": platform.machine() or "unknown", "cores": os.cpu_count() or 1}
    cpuinfo = _read_text(root / "proc" / "cpuinfo")
    if cpuinfo:
        for line in cpuinfo.splitlines():
            if line.lower().startswith(("model name", "model\t", "hardware")):
                model = line.partition(":")[2].strip()
                if model:
                    cpu["model"] = model
                break
    return cpu


def _system_ram_gb(root: Path) -> float:
    meminfo = _read_text(root / "proc" / "meminfo")
    if meminfo:
        for line in meminfo.splitlines():
            if line.startswith("MemTotal:"):
                parts = line.split()
                # MemTotal: <n> kB — kibibytes to gibibytes.
                return int(parts[1]) / (1024 * 1024)
    # The stdlib fallback for a machine without /proc.
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError) as exc:
        raise CensusError(f"ram probe found no readable memory total: {exc}") from exc
    return (pages * page_size) / (1024**3)


def _system_thermal_c(root: Path) -> float | None:
    """The hottest thermal-zone reading, or None when the
    machine exposes no sensor (a VM commonly exposes none —
    absence is reported as absence, never as a cool fiction)."""
    readings: list[float] = []
    thermal_dir = root / "sys" / "class" / "thermal"
    try:
        zones = sorted(thermal_dir.glob("thermal_zone*"))
    except OSError:
        return None
    for zone in zones:
        raw = _read_text(zone / "temp")
        if raw is None:
            continue
        try:
            readings.append(int(raw.strip()) / 1000.0)  # millidegrees
        except ValueError:
            continue
    return max(readings) if readings else None


def _cuda_name(root: Path) -> str | None:
    """The GPU's own name from the driver's /proc listing, when
    the driver publishes one — a measured string, not a guess."""
    gpu_dir = root / "proc" / "driver" / "nvidia" / "gpus"
    try:
        infos = sorted(gpu_dir.glob("*/information"))
    except OSError:
        return None
    for info in infos:
        text = _read_text(info)
        if not text:
            continue
        for line in text.splitlines():
            if line.startswith("Model:"):
                return line.partition(":")[2].strip() or None
    return None


def _system_accelerators(root: Path) -> list:
    """Accelerators detected by their device presence.

    Each detection is a filesystem fact (a device node or the
    driver's /proc tree). ``proven`` is False for every entry:
    presence is measured here; a proven inference run on the
    device is the engine ledger's claim to make (Y-024), not
    the census's.
    """
    found: list[dict] = []
    dev = root / "dev"
    if (dev / "nvidia0").exists() or (root / "proc" / "driver" / "nvidia").exists():
        entry: dict = {"kind": "cuda", "proven": False}
        name = _cuda_name(root)
        if name:
            entry["name"] = name
        found.append(entry)
    if (dev / "hailo0").exists():
        found.append({"kind": "hailo", "name": "Hailo device node", "proven": False})
    if (dev / "rknpu").exists() or list(dev.glob("npu*")):
        found.append({"kind": "npu", "proven": False})
    if sys.platform == "darwin" and platform.machine().startswith("arm"):
        found.append({"kind": "metal", "proven": False})
    if not found:
        return [{"kind": "none"}]
    return found


def system_probe_set(root: Path | str = "/") -> ProbeSet:
    """The probe set that measures a real machine, reading
    through ``root`` (``/`` for the machine this runs on; a
    fake root in tests). Disk is measured at the root itself,
    which for a fake root is the tmp tree — the *shape* of the
    measurement is what the fake root proves."""
    base = Path(root)

    def disk_free_gb() -> float | None:
        try:
            return shutil.disk_usage(base).free / (1024**3)
        except OSError:
            return None

    return ProbeSet(
        cpu=lambda: _system_cpu(base),
        ram_gb=lambda: _system_ram_gb(base),
        accelerators=lambda: _system_accelerators(base),
        disk_free_gb=disk_free_gb,
        thermal_c=lambda: _system_thermal_c(base),
        vram_gb=lambda: None,  # no stdlib/filesystem source reads VRAM honestly
    )


# -- measurement ----------------------------------------------------------


def _probe(name: str, fn: Callable | None, *, required: bool):
    if fn is None:
        return None
    try:
        return fn()
    except CensusError:
        raise
    except Exception as exc:  # a probe that cannot read must say so
        if required:
            raise CensusError(f"{name} probe failed: {exc}") from exc
        return None


def measure_census(
    node_id: str,
    *,
    probes: ProbeSet | None = None,
    engines: list | None = None,
    models_present: list | None = None,
    at: datetime | None = None,
) -> dict:
    """Measure one node's census, here, now — a validated
    CapabilityCensus whose every value a probe actually read.

    ``engines`` entries are validated against the Y-008 engine
    schema before entering the census (an adapter reports its
    own reachability; the census does not soften it), and the
    default is an empty list — honestly, no adapter exists
    until Y-023. ``models_present`` likewise defaults empty:
    presence with hash proof is the model store's word (Y-026),
    and a name the caller cannot back does not belong here.
    """
    if not isinstance(node_id, str) or not node_id:
        raise CensusError("a census needs a node_id")
    probes = probes if probes is not None else system_probe_set()
    cpu = _probe("cpu", probes.cpu, required=True)
    if not isinstance(cpu, dict) or "arch" not in cpu or "cores" not in cpu:
        raise CensusError(f"cpu probe returned an unshaped reading: {cpu!r}")
    ram_gb = _probe("ram", probes.ram_gb, required=True)
    accelerators = _probe("accelerators", probes.accelerators, required=True)
    if not isinstance(accelerators, list):
        raise CensusError(f"accelerators probe returned a non-list: {accelerators!r}")
    cleaned: list[dict] = []
    seen: set[str] = set()
    for entry in accelerators:
        if not isinstance(entry, dict) or entry.get("kind") not in ACCELERATOR_KINDS:
            raise CensusError(f"accelerator probe named an unknown device: {entry!r}")
        if entry["kind"] in seen:
            continue
        seen.add(entry["kind"])
        cleaned.append(
            {
                "kind": entry["kind"],
                **({"name": entry["name"]} if entry.get("name") else {}),
                # Detection is never proof (see the module's law).
                "proven": bool(entry.get("proven", False)),
            }
        )
    if not cleaned:
        cleaned = [{"kind": "none", "proven": False}]
    if "none" in seen and len(cleaned) > 1:
        raise CensusError("a census cannot claim both 'none' and real accelerators")

    census: dict = {
        "node_id": node_id,
        "measured_at": _fmt(at or datetime.now(UTC)),
        "cpu": {"arch": cpu["arch"], "cores": int(cpu["cores"])},
        "ram_gb": round(float(ram_gb), 3),
        "accelerators": cleaned,
        "engines": [],
    }
    if cpu.get("model"):
        census["cpu"]["model"] = cpu["model"]
    disk = _probe("disk", probes.disk_free_gb, required=False)
    if disk is not None:
        census["disk_free_gb"] = round(float(disk), 3)
    thermal = _probe("thermal", probes.thermal_c, required=False)
    if thermal is not None:
        census["thermal_c"] = round(float(thermal), 1)
    vram = _probe("vram", probes.vram_gb, required=False)
    if vram is not None:
        census["vram_gb"] = round(float(vram), 3)
    for engine in engines or []:
        census["engines"].append(_validate(_ENGINE_VALIDATOR, engine, "engine capability"))
    census["models_present"] = list(models_present or [])
    return validate_census(census)


# -- change ----------------------------------------------------------------


def census_fingerprint(census: dict) -> str:
    """A census's identity without its timestamp: two censuses
    of an unchanged machine fingerprint alike however far
    apart they were measured."""
    body = {key: value for key, value in census.items() if key != "measured_at"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def diff_censuses(old: dict, new: dict) -> tuple[str, ...]:
    """The top-level fields that changed between two censuses,
    ``measured_at`` excluded — the refresh-on-change answer in
    plain field names, sorted for stable comparison."""
    keys = (set(old) | set(new)) - {"measured_at"}
    return tuple(sorted(key for key in keys if old.get(key) != new.get(key)))


class CensusCollector:
    """One node's census, kept current: measure once, refresh
    on demand, and learn from each refresh exactly what
    changed. The engines/models inputs may be plain lists or
    callables returning lists, so later slices (adapters
    Y-023 – Y-025, model store Y-026) can feed their live truth
    into the same refresh without the collector changing."""

    def __init__(
        self,
        node_id: str,
        *,
        probes: ProbeSet | None = None,
        engines=None,
        models_present=None,
        now=None,
    ):
        self.node_id = node_id
        self._probes = probes
        self._engines = engines
        self._models_present = models_present
        self._now = now or (lambda: datetime.now(UTC))
        self._current: dict | None = None

    @staticmethod
    def _resolve(value):
        return value() if callable(value) else value

    @property
    def current(self) -> dict | None:
        return self._current

    def measure(self) -> dict:
        """Measure now and hold the result as current."""
        self._current = measure_census(
            self.node_id,
            probes=self._probes,
            engines=self._resolve(self._engines),
            models_present=self._resolve(self._models_present),
            at=self._now(),
        )
        return self._current

    def refresh(self) -> tuple[dict, tuple[str, ...]]:
        """Re-measure; return (census, changed field names).

        The first refresh establishes the baseline and reports
        ``("initial",)`` — everything is new against nothing.
        A later refresh over an unchanged machine reports an
        empty tuple: the census was refreshed, and the honest
        answer is that nothing changed."""
        if self._current is None:
            return self.measure(), ("initial",)
        previous = self._current
        census = self.measure()
        return census, diff_censuses(previous, census)
