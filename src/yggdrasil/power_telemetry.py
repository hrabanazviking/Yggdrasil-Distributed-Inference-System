"""Power and thermal telemetry — Y-044.

The governor (Y-028) has judged heat since it landed, but
until this slice its thermal reading was an anonymous
number in a reading set: no zones, no watts, no provenance,
and no way for a verdict to say *what it rested on*. This
slice is the node's senses for heat and power, on the two
device classes the roadmap names — the Pi citizen (Y-033)
and the laptop citizen (Y-034) wire it in their factories —
and the citation that ties a governor verdict to them.

- **Measured from the machine, files only.** The system
  snapshot reads ``/sys/class/thermal/thermal_zone*/``
  (each zone's own type name and millidegree temperature),
  ``/sys/class/power_supply/*/`` (kind, status, voltage,
  current, power, charge capacity — micro-units converted;
  a supply that publishes voltage and current but no power
  gets its watts as their product, arithmetic on measured
  values, stated here), ``/sys/class/hwmon/*/power*_input``
  (a board's own power rails, used only when no supply
  publishes power), and ``/sys/class/powercap/*/energy_uj``
  (RAPL-class energy counters). There is no process client
  in this module — the Part I gate's banned-import scan
  stays green — and a value no file offers is **null**,
  never invented: this VM exposes no thermal zone and no
  power supply, and its readings say exactly that.
- **CPU power from counter deltas.** An energy counter is
  a total, not a rate: the collector computes
  ``cpu_power_w`` between two samples as the summed counter
  delta over the measured elapsed seconds. The first
  sample, a zero-length interval, and a counter that went
  backwards (a wrap or a reset) each yield null for the
  affected figure — a rate is never fabricated from one
  reading.
- **The report is history in numbers.** Peaks, mean power,
  the thermal slope across the sampled window, the energy
  consumed between the first and last samples (counters
  matched by name), and staleness told plainly: a report
  whose newest sample is older than ``stale_after_s``
  (default 120 s, an operator's policy not a fact) says
  ``stale: true`` — old heat is never dressed as present
  heat.
- **The governor's decisions cite them.** ``cite()`` pairs
  a ``GovernorState`` (Y-028) or a ``GovernorDecision``
  with the newest telemetry sample and answers a validated
  ``governor_citation`` v1: the verdict's reasons and
  shedding flag beside the reading's moment, its age, its
  staleness, the hottest zone by name, and the watts. In
  the Pi and laptop factories, when no readings provider
  is handed in, the governor's own thermal reading *is*
  the telemetry's hottest zone, sampled at evaluation
  time — the citation and the verdict rest on the same
  measurement, not on two readings that happen to agree.

Every sample is recorded as a Y-013 EventEnvelope from the
``node_agent`` component (counts and measured numbers
only). The node agent grows the seam at this slice:
``power_telemetry=`` at construction, ``sample_power_
telemetry()``, ``power_telemetry_report()``, and
``governor_citation()``; ``status()`` carries the report.
Without telemetry wired, the agent reports None, exactly
as before — the VM and phone factories do not wire it at
this slice (their profiles name no power source; the
module itself is class-agnostic and any factory may wire
it when its hardware offers the files).

Honesty: the in-tree acceptance uses injected snapshots
shaped from the Pi's and the laptop's measured facts and
says so (``source: "injected"``, ``simulated: true`` where
a factory is told so). The system snapshot's one real
reading — this VM's — reports its zones and supplies
honestly empty. A live power/thermal run on Mimir or
purplelongship under this module is **not** claimed here;
it belongs to the Part VIII hardware road.
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

DEFAULT_STALE_AFTER_S = 120.0
MAX_SAMPLES = 512

_SUPPLY_KINDS = {"battery": "battery", "mains": "mains", "usb": "usb"}


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


_READING_VALIDATOR = _validator("power_telemetry_reading.schema.json")
_REPORT_VALIDATOR = _validator("power_telemetry_report.schema.json")
_CITATION_VALIDATOR = _validator("governor_citation.schema.json")


class PowerTelemetryError(Exception):
    """Any telemetry failure: an unshaped snapshot, a record
    that fails its schema, a citation asked of a record that
    is neither a governor state nor a governor decision."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise PowerTelemetryError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_reading(record: dict) -> dict:
    """A power-telemetry reading against its schema."""
    return _validate(_READING_VALIDATOR, record, "power telemetry reading")


def validate_report(record: dict) -> dict:
    """A power-telemetry report against its schema."""
    return _validate(_REPORT_VALIDATOR, record, "power telemetry report")


def validate_citation(record: dict) -> dict:
    """A governor citation against its schema."""
    return _validate(_CITATION_VALIDATOR, record, "governor citation")


# -- the system snapshot ---------------------------------------------------
#
# A snapshot is the raw measured shape the collector turns
# into a reading: thermal zones, power supplies, a whole-
# machine power figure, and energy counters. The system
# snapshot reads a real machine through a filesystem root;
# tests inject snapshots of their own, or point the system
# snapshot at a fake root shaped like another machine.


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return None


def _read_float(path: Path) -> float | None:
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _system_thermal_zones(base: Path) -> list[dict]:
    zones: list[dict] = []
    thermal_dir = base / "sys" / "class" / "thermal"
    try:
        entries = sorted(thermal_dir.glob("thermal_zone*"))
    except OSError:
        return zones
    for entry in entries:
        millidegrees = _read_float(entry / "temp")
        if millidegrees is None:
            continue
        name = _read_text(entry / "type") or entry.name
        zones.append({"name": name, "temp_c": round(millidegrees / 1000.0, 1)})
    return zones


def _system_power_supplies(base: Path) -> list[dict]:
    supplies: list[dict] = []
    supply_dir = base / "sys" / "class" / "power_supply"
    try:
        entries = sorted(supply_dir.iterdir())
    except OSError:
        return supplies
    for entry in entries:
        if not entry.is_dir():
            continue
        raw_kind = (_read_text(entry / "type") or "").lower()
        kind = _SUPPLY_KINDS.get(raw_kind, "other")
        voltage = _read_float(entry / "voltage_now")
        current = _read_float(entry / "current_now")
        power = _read_float(entry / "power_now")
        voltage_v = round(voltage / 1e6, 3) if voltage is not None else None
        current_a = round(current / 1e6, 3) if current is not None else None
        if power is not None:
            power_w = round(power / 1e6, 3)
        elif voltage_v is not None and current_a is not None:
            # Watts as the product of two measured values —
            # arithmetic, stated in the module docstring.
            power_w = round(voltage_v * current_a, 3)
        else:
            power_w = None
        capacity = _read_float(entry / "capacity")
        supplies.append(
            {
                "name": entry.name,
                "kind": kind,
                "status": _read_text(entry / "status"),
                "voltage_v": voltage_v,
                "current_a": current_a,
                "power_w": power_w,
                "capacity_percent": capacity,
            }
        )
    return supplies


def _system_hwmon_power_w(base: Path) -> float | None:
    """The sum of a board's hwmon power rails, or None when
    no rail publishes — used only when no power supply
    publishes its own power figure."""
    total: float | None = None
    hwmon_dir = base / "sys" / "class" / "hwmon"
    try:
        entries = sorted(hwmon_dir.glob("hwmon*"))
    except OSError:
        return None
    for entry in entries:
        try:
            rails = sorted(entry.glob("power*_input"))
        except OSError:
            continue
        for rail in rails:
            microwatts = _read_float(rail)
            if microwatts is not None:
                total = (total or 0.0) + microwatts / 1e6
    return round(total, 3) if total is not None else None


def _system_energy_counters(base: Path) -> list[dict]:
    counters: list[dict] = []
    cap_dir = base / "sys" / "class" / "powercap"
    try:
        entries = sorted(cap_dir.glob("*rapl*")) + sorted(cap_dir.glob("intel-rapl*"))
    except OSError:
        return counters
    seen: set[Path] = set()
    for entry in entries:
        if entry in seen or not entry.is_dir():
            continue
        seen.add(entry)
        energy = _read_float(entry / "energy_uj")
        if energy is None:
            continue
        name = _read_text(entry / "name") or entry.name
        counters.append({"name": name, "energy_uj": int(energy)})
    return counters


def system_power_snapshot(root: Path | str = "/") -> dict:
    """One real machine's power/thermal snapshot, from files
    only, reading through ``root`` (``/`` for the machine
    this runs on; a fake root in tests). Anything unreadable
    is absent from the snapshot — never a guessed number."""
    base = Path(root)
    supplies = _system_power_supplies(base)
    supply_power = [s["power_w"] for s in supplies if s["power_w"] is not None]
    if supply_power:
        power_w: float | None = round(sum(supply_power), 3)
    else:
        power_w = _system_hwmon_power_w(base)
    return {
        "thermal_zones": _system_thermal_zones(base),
        "power_supplies": supplies,
        "power_w": power_w,
        "energy_counters": _system_energy_counters(base),
    }


# -- the collector -----------------------------------------------------------


class PowerTelemetry:
    """One node's power/thermal senses: sampled readings,
    a history report, and governor citations.

    ``snapshot`` is a callable returning a snapshot dict
    (the seam tests and factories use); the default reads
    the real machine via :func:`system_power_snapshot`.
    ``simulated`` is carried into every reading verbatim —
    an injected snapshot shaped from another machine's
    facts is labelled, never dressed as a live reading.
    The clock is injectable so ages, slopes, and counter
    rates are testable.
    """

    def __init__(
        self,
        node_id: str,
        *,
        snapshot=None,
        simulated: bool = False,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
        now=None,
    ):
        if not isinstance(node_id, str) or not node_id:
            raise PowerTelemetryError("power telemetry needs a node_id")
        if stale_after_s <= 0:
            raise PowerTelemetryError("stale_after_s must be positive")
        self.node_id = node_id
        self._snapshot = snapshot if snapshot is not None else system_power_snapshot
        self._source = "injected" if snapshot is not None else "system_files"
        self._simulated = bool(simulated)
        self._stale_after_s = float(stale_after_s)
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._samples: list[tuple[datetime, dict]] = []
        self._events: list[dict] = []
        self._event_seq = 0

    # -- facts ----------------------------------------------------------

    @property
    def source(self) -> str:
        return self._source

    @property
    def simulated(self) -> bool:
        return self._simulated

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    def latest(self) -> dict | None:
        """The newest reading, or None before any sample."""
        with self._lock:
            return self._samples[-1][1] if self._samples else None

    # -- internals --------------------------------------------------------

    def _emit(self, reading: dict) -> None:
        event = make_event(
            type="power_telemetry.sampled",
            node_id=self.node_id,
            component="node_agent",
            payload={
                "thermal_c": reading["hottest_c"],
                "hottest_zone": reading["hottest_zone"],
                "power_w": reading["power_w"],
                "cpu_power_w": reading["cpu_power_w"],
                "zones": len(reading["thermal_zones"]),
                "supplies": len(reading["power_supplies"]),
                "simulated": reading["simulated"],
            },
            seq=self._event_seq,
            at=self._now(),
        )
        self._event_seq += 1
        self._events.append(event)

    @staticmethod
    def _clean_snapshot(raw) -> dict:
        if not isinstance(raw, dict):
            raise PowerTelemetryError(
                f"a snapshot is a dict, got {type(raw).__name__}"
            )
        zones = []
        for zone in raw.get("thermal_zones") or []:
            if not isinstance(zone, dict) or "name" not in zone or "temp_c" not in zone:
                raise PowerTelemetryError(f"unshaped thermal zone in snapshot: {zone!r}")
            zones.append({"name": str(zone["name"]), "temp_c": round(float(zone["temp_c"]), 1)})
        supplies = []
        for supply in raw.get("power_supplies") or []:
            if not isinstance(supply, dict) or "name" not in supply or "kind" not in supply:
                raise PowerTelemetryError(f"unshaped power supply in snapshot: {supply!r}")
            if supply["kind"] not in ("battery", "mains", "usb", "other"):
                raise PowerTelemetryError(
                    f"unknown power supply kind in snapshot: {supply['kind']!r}"
                )
            supplies.append(
                {
                    "name": str(supply["name"]),
                    "kind": supply["kind"],
                    "status": supply.get("status"),
                    "voltage_v": supply.get("voltage_v"),
                    "current_a": supply.get("current_a"),
                    "power_w": supply.get("power_w"),
                    "capacity_percent": supply.get("capacity_percent"),
                }
            )
        counters = []
        for counter in raw.get("energy_counters") or []:
            if (
                not isinstance(counter, dict)
                or "name" not in counter
                or not isinstance(counter.get("energy_uj"), int)
                or counter["energy_uj"] < 0
            ):
                raise PowerTelemetryError(
                    f"unshaped energy counter in snapshot: {counter!r}"
                )
            counters.append(
                {"name": str(counter["name"]), "energy_uj": counter["energy_uj"]}
            )
        power_w = raw.get("power_w")
        if power_w is not None:
            power_w = round(float(power_w), 3)
        return {
            "thermal_zones": zones,
            "power_supplies": supplies,
            "power_w": power_w,
            "energy_counters": counters,
        }

    def _cpu_power_w(self, counters: list[dict], moment: datetime) -> float | None:
        """The CPU power rate since the previous sample, from
        energy-counter deltas over measured elapsed time —
        null on the first sample, a non-positive interval,
        or when no counter pair reads forward."""
        if not self._samples:
            return None
        prev_moment, prev_reading = self._samples[-1]
        elapsed = (moment - prev_moment).total_seconds()
        if elapsed <= 0:
            return None
        previous = {c["name"]: c["energy_uj"] for c in prev_reading["energy_counters"]}
        delta_uj = 0
        matched = False
        for counter in counters:
            before = previous.get(counter["name"])
            if before is not None and counter["energy_uj"] >= before:
                delta_uj += counter["energy_uj"] - before
                matched = True
        if not matched:
            return None
        return round(delta_uj / elapsed / 1e6, 3)

    # -- sampling -----------------------------------------------------------

    def sample(self) -> dict:
        """Take one reading now: clean the snapshot, name the
        hottest zone, derive the CPU power rate from the last
        sample's counters, validate, ledger, and emit."""
        moment = self._now()
        try:
            raw = self._snapshot()
        except PowerTelemetryError:
            raise
        except Exception as exc:
            raise PowerTelemetryError(f"snapshot provider failed: {exc}") from exc
        cleaned = self._clean_snapshot(raw)
        zones = cleaned["thermal_zones"]
        hottest = max(zones, key=lambda z: z["temp_c"]) if zones else None
        reading = {
            "version": 1,
            "node_id": self.node_id,
            "at": _fmt(moment),
            "source": self._source,
            "simulated": self._simulated,
            "thermal_zones": zones,
            "hottest_c": hottest["temp_c"] if hottest else None,
            "hottest_zone": hottest["name"] if hottest else None,
            "power_supplies": cleaned["power_supplies"],
            "power_w": cleaned["power_w"],
            "cpu_power_w": self._cpu_power_w(cleaned["energy_counters"], moment),
            "energy_counters": cleaned["energy_counters"],
        }
        validate_reading(reading)
        with self._lock:
            self._samples.append((moment, reading))
            del self._samples[:-MAX_SAMPLES]
            self._emit(reading)
            return reading

    # -- the governor's doors -------------------------------------------------

    def readings_for_governor(self, extra: dict | None = None) -> dict:
        """A Y-028 reading set whose thermal value is the
        newest sample's hottest zone — sampled now when no
        sample exists yet. ``extra`` may carry the memory /
        CPU keys the governor also judges; telemetry never
        invents them, so they pass through or stay unknown."""
        reading = self.latest() or self.sample()
        readings = {
            "thermal_c": reading["hottest_c"],
            "memory_used_bytes": None,
            "memory_total_bytes": None,
            "cpu_share_used": None,
        }
        if extra:
            unknown = set(extra) - set(readings)
            if unknown:
                raise PowerTelemetryError(
                    f"unknown governor reading key(s): {sorted(unknown)}"
                )
            readings.update(extra)
        return readings

    def cite(self, record: dict) -> dict:
        """Cite the newest telemetry beside one governor
        state or decision (auto-detected by the decision's
        ``admitted`` field). A citation with no sample yet
        says so — every telemetry field null, stale true —
        rather than sampling unasked or inventing a reading."""
        if not isinstance(record, dict) or "reasons" not in record or "at" not in record:
            raise PowerTelemetryError(
                "a citation needs a governor state or a governor decision"
            )
        subject = "decision" if "admitted" in record else "state"
        moment = self._now()
        with self._lock:
            newest = self._samples[-1] if self._samples else None
        if newest is None:
            telemetry = {
                "reading_at": None,
                "age_s": None,
                "stale": True,
                "thermal_c": None,
                "hottest_zone": None,
                "power_w": None,
                "cpu_power_w": None,
                "source": None,
                "simulated": self._simulated,
            }
        else:
            sampled_at, reading = newest
            age = round((moment - sampled_at).total_seconds(), 3)
            telemetry = {
                "reading_at": reading["at"],
                "age_s": age,
                "stale": age > self._stale_after_s,
                "thermal_c": reading["hottest_c"],
                "hottest_zone": reading["hottest_zone"],
                "power_w": reading["power_w"],
                "cpu_power_w": reading["cpu_power_w"],
                "source": reading["source"],
                "simulated": reading["simulated"],
            }
        citation = {
            "version": 1,
            "node_id": self.node_id,
            "at": _fmt(moment),
            "subject": subject,
            "subject_at": record["at"],
            "shedding": bool(record.get("shedding", False)),
            "reasons": list(record["reasons"]),
            "telemetry": telemetry,
        }
        return validate_citation(citation)

    # -- history ------------------------------------------------------------

    def report(self) -> dict:
        """The validated power_telemetry_report v1: the whole
        sampled window in numbers. Asking never samples."""
        moment = self._now()
        with self._lock:
            samples = list(self._samples)
        if not samples:
            return validate_report(
                {
                    "version": 1,
                    "node_id": self.node_id,
                    "at": _fmt(moment),
                    "samples": 0,
                    "first_at": None,
                    "last_at": None,
                    "latest": None,
                    "peak_thermal_c": None,
                    "peak_power_w": None,
                    "mean_power_w": None,
                    "thermal_slope_c_per_min": None,
                    "energy_consumed_uj": None,
                    "stale": True,
                    "simulated": self._simulated,
                }
            )
        readings = [reading for _, reading in samples]
        thermals = [r["hottest_c"] for r in readings if r["hottest_c"] is not None]
        powers = [r["power_w"] for r in readings if r["power_w"] is not None]
        slope = None
        if (
            len(samples) >= 2
            and readings[0]["hottest_c"] is not None
            and readings[-1]["hottest_c"] is not None
        ):
            minutes = (samples[-1][0] - samples[0][0]).total_seconds() / 60.0
            if minutes > 0:
                slope = round((readings[-1]["hottest_c"] - readings[0]["hottest_c"]) / minutes, 4)
        energy = None
        first_counters = {c["name"]: c["energy_uj"] for c in readings[0]["energy_counters"]}
        last_counters = {c["name"]: c["energy_uj"] for c in readings[-1]["energy_counters"]}
        shared = set(first_counters) & set(last_counters)
        if shared and len(samples) >= 2:
            energy = sum(
                max(0, last_counters[name] - first_counters[name]) for name in shared
            )
        age = (moment - samples[-1][0]).total_seconds()
        return validate_report(
            {
                "version": 1,
                "node_id": self.node_id,
                "at": _fmt(moment),
                "samples": len(samples),
                "first_at": readings[0]["at"],
                "last_at": readings[-1]["at"],
                "latest": readings[-1],
                "peak_thermal_c": max(thermals) if thermals else None,
                "peak_power_w": max(powers) if powers else None,
                "mean_power_w": round(sum(powers) / len(powers), 3) if powers else None,
                "thermal_slope_c_per_min": slope,
                "energy_consumed_uj": energy,
                "stale": age > self._stale_after_s,
                "simulated": any(r["simulated"] for r in readings),
            }
        )


__all__ = [
    "DEFAULT_STALE_AFTER_S",
    "MAX_SAMPLES",
    "PowerTelemetry",
    "PowerTelemetryError",
    "system_power_snapshot",
    "validate_citation",
    "validate_reading",
    "validate_report",
]
