"""Fabric observability — Y-064.

Parts I–III built the fabric and its doors, and every
component already counts what it does: the WAN shaper
(Y-063) counts crossings and drops, gossip (Y-055)
counts convergence, the rate door (Y-060) counts
throttling, provenance (Y-059) counts unsigned drops.
What the operator did not have is *one door* where
those counts stand together: message rates,
convergence, and drops, judged the same way every
time. This slice is that door: :class:`FabricObserver`.

- **An observation is data.** A node reports its
  standing over one window as a validated
  ``fabric_observation`` v1: sent / delivered /
  dropped, every drop counted apart by named reason,
  throttled, gossip rounds, its convergence word
  (``null`` when it reports none), and the registry
  entries it holds — counts and names only, never
  payload content (Y-013's rule). Counts that cannot
  be true are refused by name: delivered plus dropped
  past sent is ``counts_inconsistent``, and drop
  reasons that do not sum to the dropped count are
  ``reasons_incomplete`` — a drop the door cannot
  explain is not silently absorbed.
- **The latest word stands.** A newer observation
  from a node replaces its older one on the door;
  the report is the mesh *now*, not an archive.
  Refused observations are counted apart, never
  stored.
- **Rates and drops are computed, not reported.**
  ``sent_per_second`` is the observation's own sent
  over its own window; ``drop_rate`` is dropped over
  sent (zero where nothing was sent). The mesh rate
  is the sum of the nodes' rates, the mesh drop rate
  the totals' own ratio — no node can flatter the
  door by reporting a rate of its own invention.
- **Staleness and alerts are named.** An observation
  older than the policy's ``stale_after_seconds`` at
  report time is ``stale`` and its node is named in
  ``stale_nodes``; a node or mesh drop rate above
  ``max_drop_rate`` raises ``drop_rate_high``; nodes
  reporting ``converged: false``, or a latest gossip
  report that stands unconverged, raise
  ``nodes_not_converged``; an empty door says
  ``no_observations`` instead of pretending health.
- **Real reports feed the door.** A validated
  ``wan_report`` v1 (Y-063) is ingested per sending
  node — its crossings become that node's observation
  over a caller-named window — and a validated
  ``gossip_report`` v1 (Y-055) sets the door's
  convergence standing. Neither is rewired; the door
  reads the reports the components already produce.

Honesty: everything here is SIMULATED —
``simulated: true`` is a schema constant on the
observation and the report, the door aggregates
in-process reports, opens no socket, and crosses no
LAN (real carriage was Y-062's own proof). A node's
observation is only as true as the node's own
counting; the door judges consistency and freshness,
it does not manufacture traffic facts.
"""

import copy
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.gossip import validate_report as validate_gossip_report
from yggdrasil.observability import make_event
from yggdrasil.wan_simulation import validate_report as validate_wan_report

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


_POLICY_VALIDATOR = _validator("fabric_observability_policy.schema.json")
_OBSERVATION_VALIDATOR = _validator("fabric_observation.schema.json")
_REPORT_VALIDATOR = _validator("fabric_observability_report.schema.json")


class FabricObservabilityError(Exception):
    """The door's own failure: a policy, observation,
    or report that fails its schema, or counts that
    cannot be true."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(moment: str) -> datetime:
    return datetime.strptime(moment, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _validate(validator, record, kind: str):
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise FabricObservabilityError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_policy(record: dict) -> dict:
    return _validate(_POLICY_VALIDATOR, record, "fabric observability policy")


def validate_observation(record: dict) -> dict:
    return _validate(_OBSERVATION_VALIDATOR, record, "fabric observation")


def validate_report(record: dict) -> dict:
    return _validate(_REPORT_VALIDATOR, record, "fabric observability report")


def default_policy() -> dict:
    """The door's ordinary watch: an observation goes
    stale after two minutes unrefreshed, and a drop
    rate above one in twenty raises the alert — a
    policy, not a measurement."""
    return validate_policy(
        {
            "version": 1,
            "stale_after_seconds": 120,
            "max_drop_rate": 0.05,
            "alerts_enabled": True,
        }
    )


class FabricObserver:
    """The operator's door onto the fabric: the latest
    observation per node, judged under one policy.
    ``now`` is injectable so staleness is testable."""

    def __init__(self, policy: dict | None = None, *, now=None):
        self._policy = validate_policy(
            copy.deepcopy(policy) if policy is not None else default_policy()
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._observations: dict[str, dict] = {}
        self._observations_seen = 0
        self._refused = 0
        self._convergence: dict | None = None
        self._events: list[dict] = []
        self._event_seq = 0

    @property
    def policy(self) -> dict:
        return copy.deepcopy(self._policy)

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

    # -- intake ------------------------------------------

    def observe(self, observation) -> dict:
        """Take one node's ``fabric_observation`` v1
        onto the door. The latest observation from a
        node replaces its older one. Counts that cannot
        be true are refused by name and counted apart:
        delivered plus dropped past sent
        (``counts_inconsistent``), and dropped reasons
        not summing to the dropped count
        (``reasons_incomplete``)."""
        with self._lock:
            try:
                checked = validate_observation(copy.deepcopy(observation))
                if checked["delivered"] + checked["dropped"] > checked["sent"]:
                    raise FabricObservabilityError(
                        "counts_inconsistent: delivered + dropped exceeds sent"
                    )
                if sum(checked["dropped_reasons"].values()) != checked["dropped"]:
                    raise FabricObservabilityError(
                        "reasons_incomplete: dropped_reasons must sum to dropped"
                    )
            except Exception:
                self._refused += 1
                raise
            self._observations[checked["node_id"]] = checked
            self._observations_seen += 1
            self._emit(
                "fabric_observability.observation_recorded",
                checked["node_id"],
                {"sent": checked["sent"], "dropped": checked["dropped"]},
            )
            return copy.deepcopy(checked)

    def observe_wan_report(self, report, *, window_seconds: int) -> list[dict]:
        """Ingest a validated ``wan_report`` v1 (Y-063):
        each sending node's crossings on the shaper
        become that node's observation over
        ``window_seconds`` — sent, delivered, and
        dropped from its links, drops counted apart by
        the report's own reasons apportioned per node
        from its dropped records. Convergence is not
        the shaper's word, so it is reported ``None``."""
        with self._lock:
            checked = validate_wan_report(copy.deepcopy(report))
            per_node: dict[str, dict] = {}
            for link in checked["links"]:
                row = per_node.setdefault(
                    link["from_node"],
                    {"sent": 0, "delivered": 0, "dropped": 0, "reasons": {}},
                )
                row["sent"] += link["sent"]
                row["delivered"] += link["delivered"]
                row["dropped"] += link["dropped"]
            for record in checked["records"]:
                if record["verdict"] == "dropped" and record["from_node"] in per_node:
                    reasons = per_node[record["from_node"]]["reasons"]
                    reasons[record["reason"]] = reasons.get(record["reason"], 0) + 1
            taken = []
            for node_id in sorted(per_node):
                row = per_node[node_id]
                taken.append(
                    self.observe(
                        {
                            "version": 1,
                            "node_id": node_id,
                            "at": checked["at"],
                            "window_seconds": window_seconds,
                            "sent": row["sent"],
                            "delivered": row["delivered"],
                            "dropped": row["dropped"],
                            "dropped_reasons": row["reasons"],
                            "throttled": 0,
                            "gossip_rounds": 0,
                            "converged": None,
                            "registry_entries": 0,
                            "simulated": True,
                        }
                    )
                )
            return taken

    def observe_gossip_report(self, report) -> dict:
        """Ingest a validated ``gossip_report`` v1
        (Y-055) as the door's convergence standing:
        the latest such report's word stands until a
        newer one replaces it."""
        with self._lock:
            checked = validate_gossip_report(copy.deepcopy(report))
            self._convergence = {
                "reported": True,
                "converged": checked["converged"],
                "rounds_run": checked["rounds_run"],
                "messages_exchanged": checked["messages_exchanged"],
                "node_count": len(checked["node_ids"]),
            }
            self._emit(
                "fabric_observability.convergence_recorded",
                checked["node_ids"][0] if checked["node_ids"] else "unknown",
                {
                    "converged": checked["converged"],
                    "rounds_run": checked["rounds_run"],
                },
            )
            return copy.deepcopy(self._convergence)

    # -- the door ------------------------------------------

    def report(self) -> dict:
        with self._lock:
            now = self._now()
            nodes = []
            stale_nodes = []
            dropped_reasons: dict[str, int] = {}
            sent = delivered = dropped = throttled = 0
            rate_total = 0.0
            converged_nodes = 0
            reporting_nodes = 0
            any_not_converged = False
            for node_id in sorted(self._observations):
                obs = self._observations[node_id]
                age = (now - _parse(obs["at"])).total_seconds()
                stale = age > self._policy["stale_after_seconds"]
                rate = round(obs["sent"] / obs["window_seconds"], 6)
                drop_rate = round(obs["dropped"] / obs["sent"], 6) if obs["sent"] else 0.0
                alerts = []
                if drop_rate > self._policy["max_drop_rate"]:
                    alerts.append("drop_rate_high")
                if obs["converged"] is False:
                    alerts.append("not_converged")
                    any_not_converged = True
                if obs["converged"] is True:
                    converged_nodes += 1
                if obs["converged"] is not None:
                    reporting_nodes += 1
                if stale:
                    alerts.append("stale")
                    stale_nodes.append(node_id)
                sent += obs["sent"]
                delivered += obs["delivered"]
                dropped += obs["dropped"]
                throttled += obs["throttled"]
                rate_total += rate
                for reason, count in obs["dropped_reasons"].items():
                    dropped_reasons[reason] = dropped_reasons.get(reason, 0) + count
                nodes.append(
                    {
                        "node_id": node_id,
                        "at": obs["at"],
                        "window_seconds": obs["window_seconds"],
                        "sent": obs["sent"],
                        "delivered": obs["delivered"],
                        "dropped": obs["dropped"],
                        "dropped_reasons": dict(sorted(obs["dropped_reasons"].items())),
                        "throttled": obs["throttled"],
                        "gossip_rounds": obs["gossip_rounds"],
                        "registry_entries": obs["registry_entries"],
                        "sent_per_second": rate,
                        "drop_rate": drop_rate,
                        "converged": obs["converged"],
                        "stale": stale,
                        "alerts": alerts,
                    }
                )
            drop_rate = round(dropped / sent, 6) if sent else 0.0
            convergence = self._convergence or {
                "reported": False,
                "converged": None,
                "rounds_run": None,
                "messages_exchanged": None,
                "node_count": None,
            }
            if convergence["converged"] is False:
                any_not_converged = True
            alerts = []
            if not nodes:
                alerts.append("no_observations")
            if drop_rate > self._policy["max_drop_rate"]:
                alerts.append("drop_rate_high")
            if any_not_converged:
                alerts.append("nodes_not_converged")
            if stale_nodes:
                alerts.append("stale_observations")
            all_converged = None
            if reporting_nodes:
                all_converged = converged_nodes == reporting_nodes
            if convergence["reported"]:
                all_converged = bool(all_converged) and bool(convergence["converged"]) \
                    if all_converged is not None else convergence["converged"]
            return validate_report(
                {
                    "version": 1,
                    "at": _fmt(now),
                    "simulated": True,
                    "policy": copy.deepcopy(self._policy),
                    "nodes": nodes,
                    "observations": self._observations_seen,
                    "refused_observations": self._refused,
                    "sent": sent,
                    "delivered": delivered,
                    "dropped": dropped,
                    "throttled": throttled,
                    "dropped_reasons": dict(sorted(dropped_reasons.items())),
                    "sent_per_second": round(rate_total, 6),
                    "drop_rate": drop_rate,
                    "converged_nodes": converged_nodes,
                    "reporting_nodes": reporting_nodes,
                    "all_converged": all_converged,
                    "convergence": copy.deepcopy(convergence),
                    "stale_nodes": stale_nodes,
                    "alerts": alerts,
                }
            )
