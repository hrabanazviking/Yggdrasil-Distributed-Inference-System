"""The transport benchmark — Y-049 (ADR-002 executed).

ADR-002 (Y-003) fixed the race before any code ran: the
fabric's transport — **MQTT vs NATS vs a minimal signed
gossip** — is chosen by a benchmark scored on six criteria
*in the ADR's order*, and a candidate failing criterion 1
(correctness under partition) is eliminated regardless of
speed. This slice is that benchmark, as a reusable harness
— :class:`TransportBenchmark` — plus the validated
``transport_benchmark_report`` v1 it answers, and the
amendment to ADR-002 that records the decision.

How a run goes:

- **The candidates are data.** Each is a validated
  ``transport_candidate`` v1: its topology (a broker the
  nodes speak through, or a peer mesh), what it does with a
  message it cannot deliver yet (the persistence that
  decides criterion 1), the hop arithmetic of its latency,
  the shape of its idle sync traffic, and its weight —
  install size and Pi-class resident memory **declared** in
  the profile and carried through the report under names
  that say ``_declared``: estimates recorded as estimates,
  never dressed as measurements.
- **Criterion 1 is run, not asserted.** A three-node mesh
  is split for the policy's partition seconds (60 s by
  default — the ADR's own split): node 2 is cut off from
  nodes 0 and 1, and a broker, where the candidate has
  one, sits on node 0. During the split every node
  registers with every other and produces terminal results
  addressed across and within the split. A persistent
  candidate queues what it cannot deliver — in the
  sender's outbox, or at the broker for an offline
  subscriber — and the heal flushes every queue and checks
  the two things the ADR demands: **zero lost terminal
  results** and **registry convergence** (every node holds
  every registration). A candidate whose persistence is
  ``none`` drops what it cannot deliver on the spot, loses
  results, and is eliminated *by the run* — the suite
  proves the gate bites on exactly such a variant.
- **Latency is the model's hop arithmetic.** On an
  unpartitioned 3-node LAN shape, ``latency_tasks`` offers
  travel origin → claimer and their results travel back;
  each delivery costs the candidate's hops × its per-hop
  latency plus serialising (header + payload) at a
  Pi-class 100 Mbit/s link rate, with payload sizes
  cycling deterministically. Median and p95 are computed
  from those per-task numbers. This is a model of the
  topologies — one direct hop against two brokered hops —
  not a packet capture, and the report says so.
- **Chatter is the model's own sync traffic, counted.**
  Idle bytes per node per minute at each of the policy's
  mesh sizes: a broker candidate's node exchanges
  keepalive ping + ack with its broker on its sync
  interval; a gossip node's anti-entropy round sends a
  digest to ``sync_fanout`` peers (capped at the peers
  that exist) and answers the digests it receives — the
  fanout cap is why gossip chatter is flat from 10 to 30
  nodes instead of growing with the mesh.
- **The decision follows the ADR's order.** Criterion 1
  eliminates; among the survivors the lowest combined
  p95 (offer→claim + result) wins, ties broken by chatter
  at the largest mesh size, then by declared resident
  memory, then install size. TLS + per-node identity and
  one-human operability are recorded per candidate (every
  default candidate carries TLS as a first-class feature;
  the record states each one's identity story and failure
  modes so the operator's midnight judgement is on paper).

Honesty: everything here is SIMULATED — the report says
``simulated: true`` as a schema constant. No MQTT or NATS
broker was installed or run, no socket was opened, and no
LAN was measured: the three candidates race as faithful
in-process models of their topologies and persistence
rules, driven by declared parameters. The real-broker,
on-hardware race — brokers native on Mimir and
purplelongship under the citizen suites — belongs to
Part VIII, and the loser stays swappable through the
Y-069 fabric conformance kit, exactly as ADR-002
promised. The chosen transport's fabric itself is Y-050.
"""

import json
import math
import statistics
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

from yggdrasil.observability import make_event

_ROOT = Path(__file__).resolve().parents[2]
_SCHEMAS = _ROOT / "contracts" / "schemas"

# The ADR's six criteria, in the ADR's own order — the
# report carries them verbatim so the decision's basis is
# checkable against Y-003's record.
CRITERIA_ORDER = (
    "correctness_under_partition",
    "latency",
    "chatter_cost",
    "weight",
    "tls_and_per_node_identity",
    "operability_by_one_human",
)

# A Pi-class LAN link, for the serialisation term of the
# latency model: 100 Mbit/s = 12,500 bytes per millisecond.
_LINK_BYTES_PER_MS = 12_500

# The benchmark mesh's node names — fixture identities for
# a simulated mesh, never real nodes.
_BENCH_NODES = ("bench-node-0", "bench-node-1", "bench-node-2")

# The broker, where a candidate has one, sits on node 0 —
# so the partition (node 2 cut off) strands it on the far
# side of the split from node 2, the hard case.
_BROKER_NODE = "bench-node-0"


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


_CANDIDATE_VALIDATOR = _validator("transport_candidate.schema.json")
_POLICY_VALIDATOR = _validator("transport_benchmark_policy.schema.json")
_RESULT_VALIDATOR = _validator("transport_benchmark_result.schema.json")
_REPORT_VALIDATOR = _validator("transport_benchmark_report.schema.json")


class TransportBenchmarkError(Exception):
    """The benchmark's own failure: a candidate, policy,
    result, or report that fails its schema, or a run whose
    misuse would manufacture a decision."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate(validator, record: dict, kind: str) -> dict:
    errors = sorted(validator.iter_errors(record), key=str)
    if errors:
        raise TransportBenchmarkError(f"{kind} fails its schema: {errors[0].message}")
    return record


def validate_candidate(record: dict) -> dict:
    return _validate(_CANDIDATE_VALIDATOR, record, "transport candidate")


def validate_policy(record: dict) -> dict:
    return _validate(_POLICY_VALIDATOR, record, "transport benchmark policy")


def validate_result(record: dict) -> dict:
    return _validate(_RESULT_VALIDATOR, record, "transport benchmark result")


def validate_report(record: dict) -> dict:
    return _validate(_REPORT_VALIDATOR, record, "transport benchmark report")


def default_policy() -> dict:
    """ADR-002's race, as data: 100 latency tasks on the
    3-node LAN shape, the ADR's own 60 s partition split,
    four terminal results per node during the split, and
    idle chatter counted at 3, 10, and 30 nodes. A policy,
    labelled as one — not a fact about any network."""
    return {
        "version": 1,
        "latency_tasks": 100,
        "partition_seconds": 60.0,
        "terminal_results_per_node": 4,
        "node_counts": [3, 10, 30],
    }


def default_candidates() -> list[dict]:
    """The three candidates ADR-002 named, each in the
    configuration the mesh would actually run — MQTT with
    QoS-1 persistent sessions, NATS with its durable
    JetStream store, and the minimal signed gossip with a
    store-and-forward outbox. The latency, chatter, and
    weight numbers are the benchmark's declared model
    parameters (estimates recorded as estimates); the
    persistence and topology are each system's real rules,
    and those are what criterion 1 runs on."""
    return [
        validate_candidate(
            {
                "version": 1,
                "id": "mqtt",
                "topology": "broker",
                "persistence": "persistent-session",
                "hops_per_delivery": 2,
                "hop_latency_ms": 4.0,
                "header_bytes": 32,
                "sync_interval_s": 30.0,
                "sync_bytes": 16,
                "sync_fanout": 1,
                "install_size_mb_declared": 12.0,
                "resident_memory_mb_pi_declared": 24.0,
                "tls_first_class": True,
                "identity": "TLS with per-node client certificates (Y-011) "
                "verified by the broker; broker ACLs bind a node to its topics",
                "failure_modes": [
                    ("broker process down: its partition of the mesh is dark "
                    "until the service is restarted"),
                    ("broker host lost: persistent sessions on that host are "
                    "gone with it"),
                ],
                "operability_notes": "One extra always-on service (a broker) "
                "to install natively, configure, certificate, and diagnose; "
                "its logs are the first midnight stop for any fabric fault",
            }
        ),
        validate_candidate(
            {
                "version": 1,
                "id": "nats",
                "topology": "broker",
                "persistence": "durable-stream",
                "hops_per_delivery": 2,
                "hop_latency_ms": 3.0,
                "header_bytes": 48,
                "sync_interval_s": 10.0,
                "sync_bytes": 24,
                "sync_fanout": 1,
                "install_size_mb_declared": 16.0,
                "resident_memory_mb_pi_declared": 40.0,
                "tls_first_class": True,
                "identity": "TLS with per-node certificates or NKeys (Y-011 "
                "keys mapped at deployment); JetStream stores are "
                "account-scoped",
                "failure_modes": [
                    ("server down: its partition of the mesh is dark until "
                    "restart; a single server is not a cluster"),
                    ("JetStream store corrupt or full: durable delivery "
                    "degrades until an operator repairs the store"),
                ],
                "operability_notes": "One extra always-on service (server "
                "plus JetStream store) to install natively, certificate, "
                "and watch; richer tooling than MQTT, more of it to learn",
            }
        ),
        validate_candidate(
            {
                "version": 1,
                "id": "minimal-gossip",
                "topology": "peer-mesh",
                "persistence": "store-and-forward",
                "hops_per_delivery": 1,
                "hop_latency_ms": 3.0,
                "header_bytes": 88,
                "sync_interval_s": 5.0,
                "sync_bytes": 48,
                "sync_fanout": 3,
                "install_size_mb_declared": 0.0,
                "resident_memory_mb_pi_declared": 6.0,
                "tls_first_class": True,
                "identity": "Every message signed with the node's Y-011 "
                "Ed25519 key and carried over TLS between node "
                "certificates; no third party holds mesh trust",
                "failure_modes": [
                    ("a node down: only its own outboxes wait; the rest of "
                    "the mesh keeps working"),
                    ("whole-mesh partition longer than outbox retention: "
                    "queued traffic ages in the open, counted, never "
                    "silently dropped"),
                ],
                "operability_notes": "No service beyond the node agents "
                "themselves: nothing extra to install, certificate, or "
                "restart at midnight; the fabric's health is the nodes' "
                "own health reports",
            }
        ),
    ]


class _PartitionRun:
    """One candidate's mesh under the ADR's split, in
    miniature. Messages are delivered, queued, or dropped
    by the candidate's own topology and persistence rules —
    nothing else decides their fate:

    - peer-mesh: a message travels when sender and
      receiver are on the same side of the split;
    - broker: a message travels when *both* ends can reach
      the broker's side — the broker is the road;
    - a message that cannot travel is held (the sender's
      outbox, or the broker's queue for an offline
      subscriber — one pending list here, both are the
      candidate's persistence at work) when the candidate
      persists, and dropped, counted, when it does not.
    """

    def __init__(self, candidate: dict):
        self._candidate = candidate
        self._pending: list[tuple[str, str, str]] = []
        self.received_registrations: dict[str, set[str]] = {
            node: set() for node in _BENCH_NODES
        }
        self.results_sent = 0
        self.results_delivered = 0
        self.results_lost = 0

    def _group(self, node: str, *, healed: bool) -> int:
        if healed:
            return 0
        return 1 if node == _BENCH_NODES[2] else 0

    def _can_travel(self, src: str, dst: str, *, healed: bool) -> bool:
        if self._candidate["topology"] == "broker":
            broker_group = self._group(_BROKER_NODE, healed=healed)
            return (
                self._group(src, healed=healed) == broker_group
                and self._group(dst, healed=healed) == broker_group
            )
        return self._group(src, healed=healed) == self._group(dst, healed=healed)

    def _deliver(self, kind: str, src: str, dst: str) -> None:
        if kind == "registration":
            self.received_registrations[dst].add(src)
        else:
            self.results_delivered += 1

    def send(self, kind: str, src: str, dst: str) -> None:
        if kind == "terminal_result":
            self.results_sent += 1
        if self._can_travel(src, dst, healed=False):
            self._deliver(kind, src, dst)
        elif self._candidate["persistence"] == "none":
            if kind == "terminal_result":
                self.results_lost += 1
        else:
            self._pending.append((kind, src, dst))

    def heal(self) -> None:
        """The split ends: every held message is retried on
        the whole mesh. A held message that still cannot
        travel on a healed mesh is a benchmark defect, not
        a candidate's loss, and is raised as one."""
        pending, self._pending = self._pending, []
        for kind, src, dst in pending:
            if not self._can_travel(src, dst, healed=True):
                raise TransportBenchmarkError(
                    f"held {kind} from {src} to {dst} cannot travel on a healed mesh"
                )
            self._deliver(kind, src, dst)

    @property
    def registrations_converged(self) -> bool:
        everyone = set(_BENCH_NODES)
        return all(
            received == everyone - {node}
            for node, received in self.received_registrations.items()
        )


def _run_partition(candidate: dict, policy: dict) -> dict:
    """The criterion-1 scenario: during the split every
    node registers with every other, and produces the
    policy's terminal results per node, addressed
    round-robin — across the split and within it, the way
    real results fall. Then the mesh heals and the queues
    flush."""
    run = _PartitionRun(candidate)
    for src in _BENCH_NODES:
        for dst in _BENCH_NODES:
            if dst != src:
                run.send("registration", src, dst)
    for index, src in enumerate(_BENCH_NODES):
        for k in range(policy["terminal_results_per_node"]):
            dst = _BENCH_NODES[(index + 1 + k) % len(_BENCH_NODES)]
            run.send("terminal_result", src, dst)
    run.heal()
    lost = run.results_lost
    converged = run.registrations_converged
    passed = lost == 0 and converged
    return {
        "passed": passed,
        "lost_terminal_results": lost,
        "terminal_results_sent": run.results_sent,
        "registrations_converged": converged,
        # Convergence completes on the candidate's next sync
        # cycle after the heal — a gossip anti-entropy round,
        # a broker keepalive/reconnect cycle.
        "convergence_seconds_after_heal": candidate["sync_interval_s"],
    }


def _latency_ms(candidate: dict, policy: dict) -> dict:
    """The latency model: per task, hops × per-hop latency
    + serialising (header + payload) at the link rate.
    Payload sizes cycle deterministically over ten values,
    so the median and p95 are the model's own distribution,
    replayable exactly."""

    def samples() -> list[float]:
        out = []
        for i in range(policy["latency_tasks"]):
            payload_bytes = 256 + (i % 10) * 128
            serialise = (candidate["header_bytes"] + payload_bytes) / _LINK_BYTES_PER_MS
            out.append(candidate["hops_per_delivery"] * candidate["hop_latency_ms"] + serialise)
        return sorted(out)

    def p95(values: list[float]) -> float:
        return values[max(0, math.ceil(0.95 * len(values)) - 1)]

    offer = samples()
    # A result carries roughly the offer's shape back over
    # the same road — the same model, the same numbers.
    result = samples()
    return {
        "offer_claim_median": round(statistics.median(offer), 3),
        "offer_claim_p95": round(p95(offer), 3),
        "result_median": round(statistics.median(result), 3),
        "result_p95": round(p95(result), 3),
    }


def _chatter_bytes(candidate: dict, node_count: int) -> int:
    """Idle bytes per node per minute, counted from the
    candidate's sync shape: rounds per minute × the peers
    each round speaks to (the fanout, capped at the peers
    that exist; a broker candidate's one peer is its
    broker) × the sync message in both directions."""
    rounds = round(60 / candidate["sync_interval_s"])
    peers = min(candidate["sync_fanout"], node_count - 1)
    return rounds * peers * candidate["sync_bytes"] * 2


def _result_for(candidate: dict, policy: dict) -> dict:
    criterion1 = _run_partition(candidate, policy)
    eliminated = not criterion1["passed"]
    reason = None
    if eliminated:
        failures = []
        if criterion1["lost_terminal_results"]:
            failures.append(
                f"{criterion1['lost_terminal_results']} terminal results lost "
                "under partition"
            )
        if not criterion1["registrations_converged"]:
            failures.append("registry did not converge after the heal")
        reason = (
            "criterion 1 (correctness under partition) failed: "
            + "; ".join(failures)
            + " — eliminated regardless of speed (ADR-002)"
        )
    return validate_result(
        {
            "version": 1,
            "candidate_id": candidate["id"],
            "topology": candidate["topology"],
            "simulated": True,
            "eliminated": eliminated,
            "elimination_reason": reason,
            "criterion1_partition": criterion1,
            "latency_ms": _latency_ms(candidate, policy),
            "chatter_bytes_per_node_minute": {
                str(n): _chatter_bytes(candidate, n) for n in policy["node_counts"]
            },
            "weight": {
                "install_size_mb_declared": candidate["install_size_mb_declared"],
                "resident_memory_mb_pi_declared": candidate[
                    "resident_memory_mb_pi_declared"
                ],
            },
            "tls_first_class": candidate["tls_first_class"],
        }
    )


def choose(results: list[dict]) -> tuple[str | None, str]:
    """The ADR's decision rule over validated results:
    criterion 1 eliminates; among survivors the lowest
    combined p95 latency wins, ties broken by chatter at
    the largest counted mesh, then declared resident
    memory, then declared install size. Returns the chosen
    candidate id (None when every candidate was
    eliminated) and the basis, in words."""
    survivors = [r for r in results if not r["eliminated"]]
    if not survivors:
        return None, (
            "every candidate failed criterion 1 (correctness under "
            "partition); ADR-002 leaves no transport to choose — the "
            "field must change, not the rule"
        )

    def rank(result: dict):
        largest = max(
            result["chatter_bytes_per_node_minute"],
            key=lambda k: int(k),
        )
        return (
            result["latency_ms"]["offer_claim_p95"] + result["latency_ms"]["result_p95"],
            result["chatter_bytes_per_node_minute"][largest],
            result["weight"]["resident_memory_mb_pi_declared"],
            result["weight"]["install_size_mb_declared"],
            result["candidate_id"],
        )

    winner = min(survivors, key=rank)
    basis = (
        f"{winner['candidate_id']} wins under ADR-002's order: criterion 1 "
        "eliminated "
        + (
            ", ".join(r["candidate_id"] for r in results if r["eliminated"])
            or "no candidate"
        )
        + f"; among the survivors {winner['candidate_id']} carries the "
        "lowest combined p95 (offer→claim + result) latency, with ties "
        "broken by chatter at the largest counted mesh, then declared "
        "resident memory, then declared install size"
    )
    return winner["candidate_id"], basis


class TransportBenchmark:
    """ADR-002's race, run once. ``candidates`` defaults to
    the ADR's three in their mesh configurations;
    ``policy`` to the ADR's own numbers; ``now`` is
    injectable so timestamps are testable. One validated
    Y-013 event per run, from the ``fabric`` component,
    names and counts only."""

    def __init__(
        self,
        candidates: list[dict] | None = None,
        *,
        policy: dict | None = None,
        now=None,
    ):
        self._candidates = [
            validate_candidate(dict(c))
            for c in (candidates if candidates is not None else default_candidates())
        ]
        if not self._candidates:
            raise TransportBenchmarkError("the benchmark needs at least one candidate")
        ids = [c["id"] for c in self._candidates]
        if len(set(ids)) != len(ids):
            raise TransportBenchmarkError("candidate ids must be unique")
        self._policy = validate_policy(
            dict(policy) if policy is not None else default_policy()
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._events: list[dict] = []
        self._seq = 0

    @property
    def events(self) -> list[dict]:
        return list(self._events)

    def run(self) -> dict:
        results = [_result_for(c, self._policy) for c in self._candidates]
        chosen, basis = choose(results)
        report = validate_report(
            {
                "version": 1,
                "at": _fmt(self._now()),
                "simulated": True,
                "policy": self._policy,
                "results": results,
                "criteria_order": list(CRITERIA_ORDER),
                "chosen": chosen,
                "decision_basis": basis,
            }
        )
        self._seq += 1
        self._events.append(
            make_event(
                type="transport.benchmark_completed",
                node_id=_BENCH_NODES[0],
                component="fabric",
                seq=self._seq,
                at=self._now(),
                payload={
                    "candidates": len(results),
                    "eliminated": sum(1 for r in results if r["eliminated"]),
                    "chosen": chosen or "none",
                    "simulated": True,
                },
            )
        )
        return report


def run_benchmark(
    candidates: list[dict] | None = None,
    *,
    policy: dict | None = None,
    now=None,
) -> dict:
    """The race in one call: the validated report, chosen
    transport included."""
    return TransportBenchmark(candidates, policy=policy, now=now).run()


def require_transport(report: dict) -> str:
    """The decision as a gate: the chosen candidate's id,
    or a refusal naming why the report chooses nothing."""
    validate_report(report)
    if report["chosen"] is None:
        raise TransportBenchmarkError(
            "the benchmark chose no transport: " + report["decision_basis"]
        )
    return report["chosen"]
