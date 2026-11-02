#!/usr/bin/env python3
"""The fabric on the real LAN — deployment wiring for Y-062.

This file is *deployment wiring*, not a source module:
the tree's standing rule is that ``src/`` holds no
socket of its own, so the wire lives here. It composes
the already-built pieces exactly as
``yggdrasil.lan_fabric.LanNode`` prescribes:

- ``provision`` (run once, by whoever holds the mesh
  root's key at bootstrap): creates the mesh root and
  one Y-011 certificate per citizen, derives the Y-051
  X.509 material, and writes one bundle per node. A
  bundle holds the node's *own* seed, its certificates,
  its derived TLS material, the public certificates of
  its peers, and its dial plan. Bundles are the only
  files that travel, each to its own machine only.
- ``run`` (run on every node, concurrently): listens
  for sessions, dials its designated peers (direct, or
  through an HTTP CONNECT proxy where the node's only
  road to the mesh is a tunnel — the VM's case), opens
  Y-062 sessions (mutual TLS 1.3, announcements
  exchanged, discovery unaided by the seed road),
  exchanges a hello publication and a direct note on
  every session, self-probes one peer with a plaintext
  connection (which the peer's TLS door must refuse),
  and writes a JSON result with every session's
  channel record and counters.
- ``judge`` (run where the results gather): assembles
  the three node results into ``lan_leg`` v1 records
  — cross-checking that each server's counters mirror
  its client's — and judges them with
  ``yggdrasil.lan_fabric.evaluate_lan_run``.

The dial plan is data in the bundles, written at
provision time from the machines' measured addresses;
paths are named honestly per leg (``lan`` for the home
network segment, ``tailnet`` for a WireGuard tunnel
leg). Nothing here upgrades a leg's path or its
``simulated`` flag: a leg this runner carries is real
carriage, and the judge's verdict stands on the
records alone.
"""

import argparse
import base64
import json
import socket
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from yggdrasil.discovery import DiscoveryService
from yggdrasil.fabric import Fabric
from yggdrasil.identity import NodeKeypair, create_mesh_root, issue_node_certificate
from yggdrasil.lan_fabric import (
    LanNode,
    default_policy,
    evaluate_lan_run,
    expected_fingerprints_from_certs,
)
from yggdrasil.tls_channels import TlsNodeChannels, mesh_root_x509_der, node_x509_der

PORT = 9417
AGENT_VERSION = "0.0.1"

# The three citizens, from their measured facts (read
# off the machines on 2026-11-02 before this run was
# built): the Pi and the laptop share the home LAN
# segment 192.168.50.0/24; this VM has no LAN
# interface at all — its only road to the home
# machines is the tailnet, reached through the
# runtime's egress proxy — so its legs are tailnet
# legs and are named as such.
NODE_SPECS = [
    {
        "node_id": "mimir-1",
        "device_class": "pi",
        "announce_host": "192.168.50.66",
        "dial": {},
        "probe": {"peer": "purplelongship-1", "host": "192.168.50.40", "proxy": False},
    },
    {
        "node_id": "purplelongship-1",
        "device_class": "laptop",
        "announce_host": "192.168.50.40",
        "dial": {"mimir-1": {"host": "192.168.50.66", "path": "lan", "proxy": False}},
        "probe": {"peer": "mimir-1", "host": "192.168.50.66", "proxy": False},
    },
    {
        "node_id": "htch-runtime-1",
        "device_class": "vm",
        "announce_host": "htch-runtime",
        "dial": {
            "mimir-1": {"host": "100.101.39.30", "path": "tailnet", "proxy": True},
            "purplelongship-1": {"host": "100.67.240.22", "path": "tailnet", "proxy": True},
        },
        "probe": {"peer": "mimir-1", "host": "100.101.39.30", "proxy": True},
    },
]

PROXY_HOST = "hatch-egress-proxy"
PROXY_PORT = 3130


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


# ---------------------------------------------------------------- provision


def provision(out_dir: Path) -> None:
    now = datetime.now(UTC)
    root_keypair, root_cert = create_mesh_root(now=now)
    root_der = mesh_root_x509_der(root_keypair, root_cert)
    keypairs = {}
    certs = {}
    for spec in NODE_SPECS:
        keypair = NodeKeypair.generate()
        cert = issue_node_certificate(
            root_keypair,
            root_cert,
            node_id=spec["node_id"],
            node_keypair=keypair,
            device_class=spec["device_class"],
            agent_version=AGENT_VERSION,
            now=now,
        )
        keypairs[spec["node_id"]] = keypair
        certs[spec["node_id"]] = cert
    for spec in NODE_SPECS:
        node_id = spec["node_id"]
        bundle = {
            "node_id": node_id,
            "device_class": spec["device_class"],
            "agent_version": AGENT_VERSION,
            "announce_host": spec["announce_host"],
            "port": PORT,
            "seed_b64": _b64(keypairs[node_id].seed),
            "node_cert": certs[node_id],
            "root_cert": root_cert,
            "node_der_b64": _b64(node_x509_der(root_keypair, root_cert, certs[node_id])),
            "root_der_b64": _b64(root_der),
            "peer_certs": {p: c for p, c in certs.items() if p != node_id},
            "dial": spec["dial"],
            "probe": spec["probe"],
        }
        bundle_dir = out_dir / node_id
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (bundle_dir / "bundle.json").write_text(json.dumps(bundle, indent=1, sort_keys=True))
    (out_dir / "certs.json").write_text(json.dumps(certs, indent=1, sort_keys=True))
    policy = default_policy([spec["node_id"] for spec in NODE_SPECS])
    (out_dir / "policy.json").write_text(json.dumps(policy, indent=1, sort_keys=True))
    print(f"provisioned {len(NODE_SPECS)} bundles under {out_dir}")


# ---------------------------------------------------------------- sockets


def connect_direct(host: str, port: int, timeout: float = 10.0) -> socket.socket:
    return socket.create_connection((host, port), timeout=timeout)


def connect_via_proxy(host: str, port: int, timeout: float = 15.0) -> socket.socket:
    sock = socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=timeout)
    sock.sendall(
        f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode()
    )
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            raise OSError("proxy closed the CONNECT")
        response += chunk
    status = response.split(b"\r\n", 1)[0]
    if b" 200 " not in status + b" ":
        raise OSError(f"proxy refused CONNECT: {status!r}")
    return sock


def connect(plan: dict, port: int) -> socket.socket:
    if plan.get("proxy"):
        return connect_via_proxy(plan["host"], port)
    return connect_direct(plan["host"], port)


# ---------------------------------------------------------------- run


class Runner:
    def __init__(self, bundle: dict, material_dir: Path):
        self.bundle = bundle
        self.node_id = bundle["node_id"]
        keypair = NodeKeypair(seed=_unb64(bundle["seed_b64"]))
        channels = TlsNodeChannels(
            node_id=self.node_id,
            keypair=keypair,
            node_cert=bundle["node_cert"],
            root_cert=bundle["root_cert"],
            node_der=_unb64(bundle["node_der_b64"]),
            root_der=_unb64(bundle["root_der_b64"]),
            material_dir=material_dir,
        )
        discovery = DiscoveryService(
            cert=bundle["node_cert"],
            root_cert=bundle["root_cert"],
            host=bundle["announce_host"],
            port=bundle["port"],
        )
        self.node = LanNode(
            node_id=self.node_id,
            channels=channels,
            discovery=discovery,
            fabric=Fabric(),
            peer_certs=bundle["peer_certs"],
        )
        self.channels = channels
        self.sessions: dict[str, object] = {}
        self.session_roles: dict[str, str] = {}
        self.send_locks: dict[str, threading.Lock] = {}
        self.lock = threading.Lock()
        self.deadline = time.monotonic() + 80.0

    # -- session plumbing --------------------------------

    def _register(self, session, role: str) -> None:
        with self.lock:
            self.sessions[session.peer_node] = session
            self.session_roles[session.peer_node] = role
            self.send_locks[session.peer_node] = threading.Lock()
        threading.Thread(target=self._pump, args=(session,), daemon=True).start()

    def _send(self, session, message) -> None:
        with self.send_locks[session.peer_node]:
            session.send_message(message)

    def _pump(self, session) -> None:
        while time.monotonic() < self.deadline:
            try:
                message = session.recv_message()
            except TimeoutError:
                continue  # an idle receive window is not the end of the run
            except Exception:  # noqa: BLE001 — closed or refused: the pump's work is done either way
                return
            if message is None:
                return
            payload = message.get("payload")
            if (
                message["kind"] == "publish"
                and isinstance(payload, dict)
                and payload.get("kind") == "hello"
            ):
                reply = self.node.direct(
                    session.peer_node,
                    {"kind": "reply", "from": self.node_id},
                )
                try:
                    self._send(session, reply)
                except Exception:  # noqa: BLE001 — a failed reply ends this pump; the run's records show what crossed
                    return

    def _accept_loop(self, listener: socket.socket) -> None:
        listener.settimeout(2.0)
        while time.monotonic() < self.deadline:
            try:
                raw, _addr = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(
                target=self._accept_one, args=(raw,), daemon=True
            ).start()

    def _accept_one(self, raw: socket.socket) -> None:
        try:
            # The timeout is set before the wrap on purpose:
            # wrapping detaches the plain socket, and the
            # TLS socket inherits this timeout — 20 s is
            # the pump's idle window, not a session limit.
            raw.settimeout(20.0)
            session = self.node.open_server(raw, via="seed")
        except Exception:  # noqa: BLE001 — a refused handshake (plaintext probes included) is the TLS door's counted data, not the runner's failure
            try:
                raw.close()
            except OSError:
                pass
            return
        self._register(session, "server")

    def _dial_all(self) -> None:
        for peer, plan in sorted(self.bundle["dial"].items()):
            while time.monotonic() < self.deadline - 30.0:
                try:
                    raw = connect(plan, self.bundle["port"])
                    raw.settimeout(20.0)
                    session = self.node.open_client(raw, peer, via="seed")
                    break
                except Exception:  # noqa: BLE001 — the peer may not be listening yet; the dial plan retries until its deadline
                    time.sleep(3.0)
            else:
                continue
            self._register(session, "client")

    # -- the plaintext probe ------------------------------

    def _probe(self) -> dict:
        probe = self.bundle["probe"]
        plan = {"host": probe["host"], "proxy": probe["proxy"]}
        outcome = "unreachable"
        try:
            sock = connect(plan, self.bundle["port"])
            sock.settimeout(5.0)
            sock.sendall(b"HELLO-PLAINTEXT-NOT-TLS\r\n\r\n")
            try:
                data = sock.recv(1024)
                outcome = "closed_by_peer" if data == b"" else f"answered_{len(data)}_bytes"
            except (ConnectionResetError, BrokenPipeError):
                outcome = "reset_by_peer"
            except TimeoutError:
                outcome = "no_answer_within_5s"
            sock.close()
        except Exception as exc:  # noqa: BLE001 — the outcome is the data
            outcome = f"dial_failed_{type(exc).__name__}"
        return {"peer": probe["peer"], "host": probe["host"], "outcome": outcome}

    # -- the run -------------------------------------------

    def run(self) -> dict:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", self.bundle["port"]))
        listener.listen(8)
        accept_thread = threading.Thread(
            target=self._accept_loop, args=(listener,), daemon=True
        )
        accept_thread.start()
        dial_thread = threading.Thread(target=self._dial_all, daemon=True)
        dial_thread.start()

        time.sleep(12.0)
        probe = self._probe()

        # Traffic: when both expected peers stand in
        # sessions (or the traffic moment arrives), every
        # session carries a hello publication and a
        # direct note from this node.
        traffic_deadline = time.monotonic() + 35.0
        while time.monotonic() < traffic_deadline:
            with self.lock:
                ready = len(self.sessions) >= 2
            if ready:
                break
            time.sleep(1.0)
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            for build in (
                lambda s=session: self.node.publish(
                    "lan.hello", {"kind": "hello", "from": self.node_id}
                ),
                lambda s=session: self.node.direct(
                    s.peer_node, {"kind": "note", "from": self.node_id}
                ),
            ):
                try:
                    self._send(session, build())
                except Exception as exc:  # noqa: BLE001 — a failed traffic send is recorded, not fatal to the run
                    print(f"traffic send failed: {exc}", file=sys.stderr)

        while time.monotonic() < self.deadline:
            time.sleep(1.0)
        with self.lock:
            sessions = list(self.sessions.values())
        result_sessions = []
        for session in sessions:
            result_sessions.append(
                {
                    "peer": session.peer_node,
                    "role": self.session_roles[session.peer_node],
                    "tls_version": session.record["tls_version"],
                    "cipher": session.record["cipher"],
                    "peer_fingerprint": session.record["peer_fingerprint"],
                    "established_at": session.record["established_at"],
                    "sent": session.messages_sent,
                    "received": session.messages_received,
                }
            )
            try:
                session.close()
            except Exception as exc:  # noqa: BLE001 — closing a broken session is not a new failure
                print(f"session close failed: {exc}", file=sys.stderr)
        listener.close()
        return {
            "node_id": self.node_id,
            "sessions": result_sessions,
            "discovery_peers": sorted(
                peer["node_id"] for peer in self.node.discovery.peers()
            ),
            "tls_report": self.channels.report(),
            "lan_node_report": self.node.report(),
            "plaintext_probe": probe,
            "dial_paths": {
                peer: plan["path"] for peer, plan in self.bundle["dial"].items()
            },
        }


def run_node(bundle_path: Path, out_path: Path) -> None:
    bundle = json.loads(bundle_path.read_text())
    runner = Runner(bundle, bundle_path.parent / "material")
    result = runner.run()
    out_path.write_text(json.dumps(result, indent=1, sort_keys=True))
    print(f"{bundle['node_id']}: result written to {out_path}")


# ---------------------------------------------------------------- judge


def judge(prov_dir: Path, result_paths) -> None:
    certs = json.loads((prov_dir / "certs.json").read_text())
    policy = json.loads((prov_dir / "policy.json").read_text())
    results = {}
    for path in result_paths:
        record = json.loads(Path(path).read_text())
        results[record["node_id"]] = record
    legs = []
    mirror = {}
    for node_id, result in sorted(results.items()):
        for session in result["sessions"]:
            if session["role"] != "client":
                continue
            peer = session["peer"]
            server = next(
                (
                    s
                    for s in results[peer]["sessions"]
                    if s["peer"] == node_id and s["role"] == "server"
                ),
                None,
            )
            mirror[f"{node_id}<->{peer}"] = bool(
                server
                and server["received"] == session["sent"]
                and server["sent"] == session["received"]
            )
            legs.append(
                {
                    "version": 1,
                    "node_a": node_id,
                    "node_b": peer,
                    "path": result["dial_paths"][peer],
                    "discovery_via": "seed",
                    "tls_version": session["tls_version"],
                    "cipher": session["cipher"],
                    "a_saw_b_fingerprint": session["peer_fingerprint"],
                    "b_saw_a_fingerprint": server["peer_fingerprint"] if server else "0" * 64,
                    "announcements_exchanged": bool(
                        peer in result["discovery_peers"]
                        and node_id in results[peer]["discovery_peers"]
                    ),
                    "messages_a_to_b": session["sent"],
                    "messages_b_to_a": session["received"],
                    "simulated": False,
                    "established_at": session["established_at"],
                }
            )
    report = evaluate_lan_run(
        policy,
        expected_fingerprints=expected_fingerprints_from_certs(certs),
        legs=legs,
    )
    print(json.dumps({"legs": legs, "mirror_ok": mirror, "report": report}, indent=1, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prov = sub.add_parser("provision")
    prov.add_argument("--out", type=Path, required=True)
    run = sub.add_parser("run")
    run.add_argument("--bundle", type=Path, required=True)
    run.add_argument("--out", type=Path, required=True)
    jud = sub.add_parser("judge")
    jud.add_argument("--prov", type=Path, required=True)
    jud.add_argument("--results", type=Path, nargs="+", required=True)
    args = parser.parse_args()
    if args.command == "provision":
        provision(args.out)
    elif args.command == "run":
        run_node(args.bundle, args.out)
    elif args.command == "judge":
        judge(args.prov, args.results)


if __name__ == "__main__":
    main()
