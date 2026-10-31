"""The operator's door — Midgard CLI v0 (Y-017).

The human is never locked out of their own tree: from the first
day there is a command that lists the mesh's nodes, the tasks it
has carried, and the health of every branch. In the role catalog
(Y-012) Midgard's function is literally "User Interface / CLI";
this module is that hat, as code.

The door is READ-ONLY in v0. It cannot submit, cancel, drain, or
retire anything — those verbs arrive with the machinery they act
on (task submission grows at Y-093, node operator tools at
Y-042). What it does is look, honestly:

- views are built only from validated contract data — the
  registry snapshot, the recorded ResultEnvelopes, and the
  HealthReports (Y-006/Y-007 shapes), never from a second,
  door-only version of the truth;
- an empty task history is reported as empty; a ``gone`` node
  is shown as gone; an unknown node is an error, not a blank.

The only mesh source that exists at Y-017 is the loopback
simulator (Y-014), so the CLI runs against a small built-in
SIMULATED demo mesh unless a caller hands it a door over their
own ``Simulator``. Every output, text and JSON alike, carries
``simulated: true`` / the SIMULATED label: no real node has been
listed by this door yet, and its output must never be read as a
live mesh. Part II/III sources will plug in behind the same
``OperatorDoor`` views when they exist.

There is no network code in this module: the door reads a
``Simulator`` in this process, exactly like the tunnel it views.
"""

import argparse
import json
import sys

from yggdrasil.simulator import TRANSPORT, Simulator, SimulatorError

SOURCE_LABEL = (
    f"source: {TRANSPORT} (SIMULATED — no real nodes are running yet)"
)


class CliError(Exception):
    """A door failure the operator should read, not a traceback."""


def build_demo_mesh() -> Simulator:
    """A small deterministic SIMULATED mesh for the door to show.

    Three fake nodes wearing three of the nine hats, and a
    two-task fixture workload run through the simulator's own
    submit path, so ``tasks`` shows real recorded results (of
    simulated work, labelled as such). One node is stopped
    after its task so ``health`` shows a ``gone`` branch beside
    the present ones. Built fresh, it is identical every time:
    fixed task ids, virtual clock, stand-in placement.
    """
    sim = Simulator()
    sim.add_fake_node(
        "sim-alfheim",
        device_class="pi",
        roles=["alfheim"],
        capabilities=("vision",),
        latency_ms=4,
    )
    sim.add_fake_node(
        "sim-jotunheim",
        device_class="laptop",
        roles=["jotunheim"],
        models=("sim-chat-1", "sim-code-1"),
        capabilities=("generate",),
        latency_ms=9,
    )
    sim.add_fake_node(
        "sim-midgard",
        device_class="vm",
        roles=["midgard"],
        models=("sim-chat-1",),
        capabilities=("generate", "stream"),
        latency_ms=5,
    )
    sim.submit(
        {
            "id": "00000000-0000-4000-8000-000000000017",
            "kind": "chat.generate",
            "payload": {"prompt": "hello from the operator's door"},
            "caller": "yggdrasil-cli-demo",
            "criticality": "standard",
            "requirements": {"model": "sim-chat-1", "capabilities": ["generate"]},
        }
    )
    sim.submit(
        {
            "id": "00000000-0000-4000-8000-000000000018",
            "kind": "vision.describe",
            "payload": {"prompt": "SIMULATED image placeholder"},
            "caller": "yggdrasil-cli-demo",
            "criticality": "standard",
            "requirements": {"capabilities": ["vision"]},
        }
    )
    sim.stop_node("sim-alfheim")
    return sim


class OperatorDoor:
    """Read-only operator views over one mesh source.

    Today the source is a ``Simulator``; the views speak only
    its contract-shaped outputs (registry snapshot, recorded
    results, health reports), so a later real source that
    produces the same shapes can stand behind the same door.
    """

    def __init__(self, sim: Simulator):
        self._sim = sim

    @property
    def source(self) -> str:
        return self._sim.transport

    def list_nodes(self) -> list[dict]:
        """One row per known node, node_id order: identity
        facts (device class, hats) beside present-tense truth
        (liveness, queue, capabilities, models)."""
        rows = []
        snapshot = self._sim.registry_snapshot()
        for node_id, entry in sorted(snapshot.items()):
            identity = entry["identity"]
            census = entry["census"]
            health = entry["health"]
            capabilities: set[str] = set()
            for engine in census.get("engines", []):
                if not engine.get("reachable"):
                    continue
                for name, offered in engine.get("capabilities", {}).items():
                    if offered is True:
                        capabilities.add(name)
            rows.append(
                {
                    "node_id": node_id,
                    "device_class": identity["device_class"],
                    "roles": sorted(identity.get("roles", [])),
                    "liveness": health["liveness"],
                    "queue_depth": health["queue_depth"],
                    "capabilities": sorted(capabilities),
                    "models_present": list(census.get("models_present", [])),
                    "models_loaded": list(health["models_loaded"]),
                }
            )
        return rows

    def list_tasks(self) -> list[dict]:
        """The tasks this source has recorded, in the order
        they finished: the ResultEnvelopes' own facts (status,
        provenance, total time, error code) and nothing else."""
        rows = []
        for result in self._sim.results:
            provenance = result["provenance"]
            row = {
                "task_id": result["task_id"],
                "status": result["status"],
                "node_id": provenance["node_id"],
                "engine": provenance["engine"],
                "model": provenance["model"],
                "total_ms": result["timings_ms"].get("total", 0.0),
                "error_code": (result.get("error") or {}).get("code"),
                "simulated": True,
            }
            rows.append(row)
        return rows

    def health(self, node_id: str | None = None) -> list[dict]:
        """Every node's HealthReport, or one node's. An unknown
        node is a CliError naming it — the door never answers a
        health question about a node it cannot see."""
        if node_id is not None:
            try:
                return [self._sim.health(node_id)]
            except SimulatorError as error:
                raise CliError(str(error)) from error
        snapshot = self._sim.registry_snapshot()
        return [snapshot[key]["health"] for key in sorted(snapshot)]


def _join(values: list[str]) -> str:
    return ", ".join(values) if values else "-"


def _render_nodes(door: OperatorDoor) -> str:
    rows = door.list_nodes()
    present = sum(1 for row in rows if row["liveness"] == "present")
    lines = [SOURCE_LABEL, f"{len(rows)} nodes: {present} present, {len(rows) - present} not"]
    for row in rows:
        lines.append(
            f"{row['node_id']}  {row['liveness']}  {row['device_class']}"
            f"  roles: {_join(row['roles'])}"
            f"  capabilities: {_join(row['capabilities'])}"
            f"  models: {_join(row['models_present'])}"
            f"  queue: {row['queue_depth']}"
        )
    return "\n".join(lines)


def _render_tasks(door: OperatorDoor) -> str:
    rows = door.list_tasks()
    lines = [SOURCE_LABEL]
    if not rows:
        lines.append("no tasks recorded in this source")
    for row in rows:
        line = (
            f"{row['task_id']}  {row['status']}  node {row['node_id']}"
            f"  {row['engine']}/{row['model']}  total {row['total_ms']} ms"
        )
        if row["error_code"]:
            line += f"  error: {row['error_code']}"
        lines.append(line)
    return "\n".join(lines)


def _render_health(door: OperatorDoor, node_id: str | None) -> str:
    reports = door.health(node_id)
    lines = [SOURCE_LABEL]
    for report in reports:
        lines.append(
            f"{report['node_id']}  {report['liveness']}"
            f"  queue: {report['queue_depth']}"
            f"  models loaded: {_join(report['models_loaded'])}"
            f"  at {report['at']}"
        )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON instead of text"
    )
    parser = argparse.ArgumentParser(
        prog="yggdrasil",
        description="The operator's door: list the mesh's nodes, tasks, and health.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("nodes", parents=[shared], help="list nodes")
    commands.add_parser("tasks", parents=[shared], help="list recorded tasks")
    health = commands.add_parser("health", parents=[shared], help="list health")
    health.add_argument("node_id", nargs="?", help="only this node")
    return parser


def main(argv: list[str] | None = None, *, door: OperatorDoor | None = None) -> int:
    """The CLI entry point. Returns a process exit code: 0 for
    a view shown, 2 for a question the door cannot answer."""
    args = _parser().parse_args(argv)
    if door is None:
        door = OperatorDoor(build_demo_mesh())
    try:
        if args.command == "nodes":
            payload = {"source": door.source, "simulated": True, "nodes": door.list_nodes()}
            text = _render_nodes(door)
        elif args.command == "tasks":
            payload = {"source": door.source, "simulated": True, "tasks": door.list_tasks()}
            text = _render_tasks(door)
        else:  # health
            reports = door.health(args.node_id)
            payload = {"source": door.source, "simulated": True, "health": reports}
            text = _render_health(door, args.node_id)
    except CliError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True) if args.json else text)
    return 0
