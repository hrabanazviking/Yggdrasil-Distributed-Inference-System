"""WAN-shaped simulation — Y-063.

The fabric was proven on the real LAN at Y-062. The
road beyond the LAN — WorldNet — is not tested by
hoping the LAN's numbers travel: it is tested as
*shapes*. This slice is the shaper:
:class:`WanSimulation` carries Y-050 fabric messages
across directed links whose latency and loss follow
a declared ``wan_profile`` v1, so latency/loss
behaviour is exercised in the wind tunnel (Y-014's
simulator road) before any real WAN is crossed.

- **A profile is data, not a hope.** A profile names
  its shape (``base_latency_ms`` + a uniform jitter
  draw of up to ``jitter_ms`` per attempt), its
  ``loss_rate`` (each attempt is lost with that
  probability), and its retry discipline
  (``max_attempts`` attempts in total,
  ``retry_backoff_ms`` of simulated time between
  them). Five named shapes stand in
  :data:`NAMED_PROFILES` — ``lan``,
  ``home_wan``, ``congested``, ``intercontinental``,
  and ``lossy_mobile`` — declared numbers, labelled
  a policy not a measurement, exactly as Part III's
  other policies are. A profile outside its bounds
  (negative latency, a loss rate outside 0–1, no
  attempt at all) fails validation by name.
- **The draws are seeded, so a run replays.** Each
  directed link draws from its own stream seeded by
  ``f"{seed}:{profile}:{from}->{to}"``: the same
  seed, the same profile, and the same traffic
  produce the identical records, latencies, and
  drops, byte for byte — a WAN failure found in the
  tunnel can be replayed until it is understood,
  never chased as a flake. A different seed is a
  different weather, and says so in the report.
- **Loss is counted, never silent.** A message whose
  attempts are all lost is dropped
  ``lost_after_retries``: counted apart in the
  report, evented, and delivered nowhere — the
  shaper manufactures no delivery and hides no drop
  inside a retry. A message that fails the Y-050
  ``fabric_message`` contract, or a direct message
  whose named recipient is not the link's, is
  dropped ``malformed`` before it enters the shape
  (zero attempts, no latency). Delivered messages
  wait in the recipient's inbox, whole and exact,
  until :meth:`WanSimulation.inbox` drains them.
- **Latency is simulated time.** A record's
  ``latency_ms`` is the sum of its attempts' shaped
  latencies plus the backoff between them; the
  report aggregates delivered mean/max per link.
  These numbers are a shape's consequence on a
  seeded draw — they prove how the mesh's logic
  behaves *under* latency and loss, and nothing
  about any real network's speed.

Honesty: everything here is SIMULATED —
``simulated: true`` is a schema constant on the
profile and the report, no socket is opened, no
packet leaves this process, and no real WAN was
crossed or measured. Fabric observability over
these crossings is Y-064; real WorldNet carriage
is a later road, tested here as shapes first, per
the roadmap.
"""

import copy
import json
import random
import threading
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.fabric import validate_message
from yggdrasil.observability import make_event

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"


def _load_schema(name: str) -> dict:
    return json.loads((_SCHEMAS / name).read_text())


def _validator(name: str):
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


_PROFILE_VALIDATOR = _validator("wan_profile.schema.json")
_RECORD_VALIDATOR = _validator("wan_record.schema.json")
_REPORT_VALIDATOR = _validator("wan_report.schema.json")


class WanError(Exception):
    """The shaper's own failure: a profile, record,
    or report that fails its schema."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record, kind: str):
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise WanError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_profile(record: dict) -> dict:
    return _validate(_PROFILE_VALIDATOR, record, "WAN profile")


def validate_record(record: dict) -> dict:
    return _validate(_RECORD_VALIDATOR, record, "WAN record")


def validate_report(record: dict) -> dict:
    return _validate(_REPORT_VALIDATOR, record, "WAN report")


def _profile(name, base, jitter, loss, attempts, backoff) -> dict:
    return validate_profile(
        {
            "version": 1,
            "name": name,
            "base_latency_ms": base,
            "jitter_ms": jitter,
            "loss_rate": loss,
            "max_attempts": attempts,
            "retry_backoff_ms": backoff,
            "simulated": True,
        }
    )


NAMED_PROFILES: dict[str, dict] = {
    # Declared shapes — a policy, not a measurement
    # of any real network (the roadmap's own rule).
    "lan": _profile("lan", 1.0, 1.0, 0.0, 3, 5.0),
    "home_wan": _profile("home_wan", 25.0, 10.0, 0.001, 3, 50.0),
    "congested": _profile("congested", 80.0, 50.0, 0.02, 3, 100.0),
    "intercontinental": _profile("intercontinental", 180.0, 30.0, 0.005, 4, 200.0),
    "lossy_mobile": _profile("lossy_mobile", 120.0, 80.0, 0.08, 4, 250.0),
}


def named_profile(name: str) -> dict:
    """One of the standing declared shapes, by name."""
    try:
        return copy.deepcopy(NAMED_PROFILES[name])
    except KeyError:
        raise WanError(
            f"unknown WAN profile {name!r}; the named shapes are "
            f"{sorted(NAMED_PROFILES)}"
        ) from None


def default_profile() -> dict:
    """The tunnel's ordinary weather: the home-WAN
    shape — the first step off the LAN, not the
    worst road the mesh may meet."""
    return named_profile("home_wan")


class WanSimulation:
    """Fabric messages carried across WAN-shaped
    directed links, in-process, on seeded draws.
    ``profile`` is a validated ``wan_profile`` v1
    (or a named shape's dict); ``seed`` names the
    weather; ``now`` is injectable so record times
    are replayable too."""

    def __init__(self, profile: dict | None = None, *, seed: int = 1, now=None):
        self._profile = validate_profile(
            copy.deepcopy(profile) if profile is not None else default_profile()
        )
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise WanError("seed must be an integer")
        self._seed = seed
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._streams: dict[tuple[str, str], random.Random] = {}
        self._inboxes: dict[str, list[dict]] = {}
        self._records: list[dict] = []
        self._events: list[dict] = []
        self._event_seq = 0

    @property
    def profile(self) -> dict:
        return copy.deepcopy(self._profile)

    @property
    def seed(self) -> int:
        return self._seed

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    def _emit(self, type_: str, node_id: str, payload: dict) -> None:
        self._event_seq += 1
        self._events.append(
            make_event(
                type=type_,
                node_id=node_id,
                component="fabric",
                seq=self._event_seq,
                at=self._now(),
                payload=payload,
            )
        )

    def _stream(self, from_node: str, to_node: str) -> random.Random:
        key = (from_node, to_node)
        stream = self._streams.get(key)
        if stream is None:
            stream = random.Random(
                f"ygg-wan:{self._seed}:{self._profile['name']}:{from_node}->{to_node}"
            )
            self._streams[key] = stream
        return stream

    def _record(self, **fields) -> dict:
        record = validate_record(
            {"version": 1, "at": _fmt(self._now()), "profile": self._profile["name"], **fields}
        )
        self._records.append(record)
        return copy.deepcopy(record)

    # -- the crossing --------------------------------------

    def send(self, message, to_node: str) -> dict:
        """Carry one Y-050 fabric message to one named
        recipient across this simulation's shape, and
        return its validated ``wan_record`` v1.

        The message is judged against the fabric
        contract first, and a direct message must name
        ``to_node`` as its recipient — anything else is
        dropped ``malformed`` and enters no draw. Each
        attempt then draws its fate from the link's
        seeded stream: lost with the profile's
        ``loss_rate``, else delivered after the shaped
        latency; a lost attempt costs its backoff
        before the next. Delivered messages wait in
        the recipient's inbox, whole and exact."""
        with self._lock:
            usable_to = to_node if isinstance(to_node, str) and to_node else None
            try:
                checked = validate_message(copy.deepcopy(message))
            except Exception:  # noqa: BLE001 — any contract failure is one verdict: malformed
                checked = None
            if checked is None or usable_to is None:
                message_id = (
                    message.get("message_id")
                    if isinstance(message, dict)
                    and isinstance(message.get("message_id"), str)
                    else None
                )
                from_node = (
                    message.get("from_node")
                    if isinstance(message, dict)
                    and isinstance(message.get("from_node"), str)
                    else None
                )
                record = self._record(
                    message_id=message_id,
                    from_node=from_node,
                    to_node=usable_to,
                    verdict="dropped",
                    reason="malformed",
                    attempts=0,
                    latency_ms=None,
                )
                self._emit(
                    "wan.message_dropped",
                    from_node or "unknown",
                    {"reason": "malformed", "to_node": usable_to or "unknown"},
                )
                return record
            if checked["kind"] == "direct" and checked["to_node"] != usable_to:
                record = self._record(
                    message_id=checked["message_id"],
                    from_node=checked["from_node"],
                    to_node=usable_to,
                    verdict="dropped",
                    reason="malformed",
                    attempts=0,
                    latency_ms=None,
                )
                self._emit(
                    "wan.message_dropped",
                    checked["from_node"],
                    {"reason": "malformed", "to_node": usable_to},
                )
                return record

            profile = self._profile
            stream = self._stream(checked["from_node"], usable_to)
            total_ms = 0.0
            attempts = 0
            delivered = False
            for attempt in range(1, profile["max_attempts"] + 1):
                attempts = attempt
                lost = stream.random() < profile["loss_rate"]
                latency = profile["base_latency_ms"] + stream.random() * profile["jitter_ms"]
                total_ms += latency
                if not lost:
                    delivered = True
                    break
                self._emit(
                    "wan.attempt_lost",
                    checked["from_node"],
                    {
                        "to_node": usable_to,
                        "attempt": attempt,
                        "message_id": checked["message_id"],
                    },
                )
                if attempt < profile["max_attempts"]:
                    total_ms += profile["retry_backoff_ms"]
            total_ms = round(total_ms, 3)
            if delivered:
                self._inboxes.setdefault(usable_to, []).append(copy.deepcopy(checked))
                record = self._record(
                    message_id=checked["message_id"],
                    from_node=checked["from_node"],
                    to_node=usable_to,
                    verdict="delivered",
                    reason=None,
                    attempts=attempts,
                    latency_ms=total_ms,
                )
                self._emit(
                    "wan.message_delivered",
                    checked["from_node"],
                    {
                        "to_node": usable_to,
                        "attempts": attempts,
                        "latency_ms": total_ms,
                        "message_id": checked["message_id"],
                    },
                )
                return record
            record = self._record(
                message_id=checked["message_id"],
                from_node=checked["from_node"],
                to_node=usable_to,
                verdict="dropped",
                reason="lost_after_retries",
                attempts=attempts,
                latency_ms=total_ms,
            )
            self._emit(
                "wan.message_dropped",
                checked["from_node"],
                {
                    "reason": "lost_after_retries",
                    "to_node": usable_to,
                    "attempts": attempts,
                    "message_id": checked["message_id"],
                },
            )
            return record

    def inbox(self, node_id: str) -> list[dict]:
        """Drain one recipient's inbox: every message
        the shape delivered to it, in delivery order,
        whole and exact. A second drain is empty —
        delivery is once."""
        with self._lock:
            drained, self._inboxes[node_id] = self._inboxes.get(node_id, []), []
            return drained

    # -- report ----------------------------------------------

    def report(self) -> dict:
        with self._lock:
            links: dict[tuple[str, str], dict] = {}
            dropped_reasons: dict[str, int] = {}
            sent = delivered = dropped = attempts_total = 0
            latencies: dict[tuple[str, str], list[float]] = {}
            for record in self._records:
                if record["from_node"] is None or record["to_node"] is None:
                    # a malformed crossing with no usable
                    # end names no link; it still counts
                    # in the totals and the reasons.
                    sent += 1
                    attempts_total += record["attempts"]
                    if record["verdict"] == "dropped":
                        dropped += 1
                        dropped_reasons[record["reason"]] = (
                            dropped_reasons.get(record["reason"], 0) + 1
                        )
                    continue
                key = (record["from_node"], record["to_node"])
                link = links.setdefault(
                    key,
                    {
                        "from_node": key[0],
                        "to_node": key[1],
                        "sent": 0,
                        "delivered": 0,
                        "dropped": 0,
                        "attempts": 0,
                        "retries": 0,
                    },
                )
                sent += 1
                link["sent"] += 1
                attempts_total += record["attempts"]
                link["attempts"] += record["attempts"]
                link["retries"] += max(0, record["attempts"] - 1)
                if record["verdict"] == "delivered":
                    delivered += 1
                    link["delivered"] += 1
                    latencies.setdefault(key, []).append(record["latency_ms"])
                else:
                    dropped += 1
                    link["dropped"] += 1
                    dropped_reasons[record["reason"]] = (
                        dropped_reasons.get(record["reason"], 0) + 1
                    )
            link_rows = []
            for key in sorted(links):
                link = links[key]
                values = latencies.get(key, [])
                link_rows.append(
                    {
                        **link,
                        "mean_latency_ms": (
                            round(sum(values) / len(values), 3) if values else None
                        ),
                        "max_latency_ms": (max(values) if values else None),
                    }
                )
            return validate_report(
                {
                    "version": 1,
                    "at": _fmt(self._now()),
                    "simulated": True,
                    "seed": self._seed,
                    "profile": copy.deepcopy(self._profile),
                    "links": link_rows,
                    "records": copy.deepcopy(self._records),
                    "sent": sent,
                    "delivered": delivered,
                    "dropped": dropped,
                    "attempts": attempts_total,
                    "dropped_reasons": dict(sorted(dropped_reasons.items())),
                }
            )
