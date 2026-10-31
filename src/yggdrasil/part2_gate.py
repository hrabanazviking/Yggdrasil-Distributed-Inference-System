"""Part II gate — Y-048.

The gate Part II must pass before the fabric rises (Part III):
three real citizens (Pi, laptop, VM) run the conformance kit
green against their real engines. This module makes that gate
*checkable*, the way Y-020 made Part I's gate checkable, instead
of leaving it a claim in a commit message:

- every slice Y-021 – Y-047 names the files it was banked on,
  and every one of those files must exist and be non-empty;
- the founding README must be byte-for-byte the document the
  campaign opened on (its md5 is the campaign's standing check);
- the standing checks the earlier slices built must all still
  pass from here: every contract schema valid and versioned
  (Y-016), every registered data file valid (Y-016), the
  licence & provenance check green with nothing vendored
  (Y-018), and the campaign file a true mirror of the roadmap
  naming Y-048 as the next unbanked slice after Y-001 – Y-047
  (Y-019);
- the CI workflow must still be manual-dispatch only (Y-009,
  Volmarr's standing default) and no Docker file may exist
  anywhere in the tree (standing no-Docker law);
- no source module may import a network or process client or
  a real engine SDK: Part II's adapters speak through injected
  transports (ADR-004), so the tree still holds no socket of
  its own — deployment wiring owns the wire;
- and the gate's own exercise: each of the three citizens is
  built by its real factory (Y-033 – Y-035) from the measured
  facts of its real machine — Mimir the Pi 5, purplelongship
  the Ryzen/RTX laptop, this VM — evaluates ready, starts,
  joins, and heartbeats present; the citizen's engine adapter
  (Ollama for the Pi, Aesir for the laptop, llama.cpp for the
  VM — the engines their slices name) runs the Y-046
  conformance kit to a ``conformant`` verdict; and the citizen
  then serves one queued task end to end *through that
  adapter* (the Y-029 sandbox bridge), the streamed text
  arriving whole, with zero failure incidents (Y-032).

Honesty: the citizens were run LIVE on their real machines in
Y-033 – Y-035 and those runs stand as recorded there. This
gate re-runs the citizens in-tree from those same measured
facts, injected as probes, with transports that are test
doubles speaking each engine's real wire shapes — so every
run inside this gate is SIMULATED, and the conformance reports
say so. No live engine server is claimed here: no Aesir
service was active on the laptop at its citizen slice, no
Ollama server has been measured on Mimir, and this VM hosts
no engine at all. What the gate proves is that the tree as it
stands — citizens, adapters, kit — still fits together green,
on the shapes of the real machines.
"""

import json
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from yggdrasil.aesir_adapter import AesirAdapter, AesirHttpError, AesirUnreachable
from yggdrasil.campaign import first_unbanked, verify_campaign
from yggdrasil.census import ProbeSet
from yggdrasil.conformance import run_conformance
from yggdrasil.data_discipline import validate_repo, validate_schemas
from yggdrasil.identity import LocalKeyStore, NodeKeypair, create_mesh_root, issue_node_certificate
from yggdrasil.laptop_citizen import build_laptop_citizen_agent
from yggdrasil.node_agent import NodeConfig
from yggdrasil.ollama_adapter import OllamaAdapter, OllamaHttpError, OllamaUnreachable
from yggdrasil.openai_compat_adapter import (
    LlamaCppAdapter,
    OpenAICompatHttpError,
    OpenAICompatUnreachable,
)
from yggdrasil.part1_gate import (
    BANNED_IMPORTS,
    GateError,
    check_ci_and_no_docker,
    check_readme_unchanged,
    source_imports,
)
from yggdrasil.pi_citizen import build_pi_citizen_agent
from yggdrasil.provenance import check_provenance
from yggdrasil.sandbox import adapter_generate_runner
from yggdrasil.vm_citizen import build_vm_citizen_agent

_ROOT = Path(__file__).resolve().parents[2]

FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)

# Each Part II slice and the files it was banked on. A slice
# whose deliverable is missing or empty has not landed,
# whatever any ledger says — the gate reads the tree, not
# the log.
PART2_DELIVERABLES: dict[str, tuple[str, ...]] = {
    "Y-021": (
        "src/yggdrasil/node_agent.py",
        "docs/NODE_AGENT.md",
        "contracts/schemas/node_config.schema.json",
        "deploy/yggdrasil-node.service",
    ),
    "Y-022": ("src/yggdrasil/census.py", "docs/CENSUS.md"),
    "Y-023": ("src/yggdrasil/ollama_adapter.py", "docs/OLLAMA_ADAPTER.md"),
    "Y-024": ("src/yggdrasil/aesir_adapter.py", "docs/AESIR_ADAPTER.md"),
    "Y-025": ("src/yggdrasil/openai_compat_adapter.py", "docs/OPENAI_COMPAT_ADAPTER.md"),
    "Y-026": (
        "src/yggdrasil/model_store.py",
        "docs/MODEL_STORE.md",
        "contracts/schemas/model_store_index.schema.json",
        "contracts/schemas/model_store_entry.schema.json",
    ),
    "Y-027": (
        "src/yggdrasil/model_lifecycle.py",
        "docs/MODEL_LIFECYCLE.md",
        "contracts/schemas/loaded_model.schema.json",
        "contracts/schemas/model_swap_fact.schema.json",
        "contracts/schemas/model_lifecycle_report.schema.json",
    ),
    "Y-028": (
        "src/yggdrasil/governor.py",
        "docs/GOVERNOR.md",
        "contracts/schemas/governor_policy.schema.json",
        "contracts/schemas/governor_state.schema.json",
        "contracts/schemas/governor_decision.schema.json",
    ),
    "Y-029": (
        "src/yggdrasil/sandbox.py",
        "docs/SANDBOX.md",
        "contracts/schemas/sandbox_policy.schema.json",
        "contracts/schemas/sandbox_report.schema.json",
    ),
    "Y-030": (
        "src/yggdrasil/health_endpoint.py",
        "docs/HEALTH_ENDPOINT.md",
        "contracts/schemas/node_health.schema.json",
    ),
    "Y-031": (
        "src/yggdrasil/local_queue.py",
        "docs/LOCAL_QUEUE.md",
        "contracts/schemas/queue_policy.schema.json",
        "contracts/schemas/queued_task.schema.json",
        "contracts/schemas/queue_report.schema.json",
    ),
    "Y-032": (
        "src/yggdrasil/failure_shapes.py",
        "docs/FAILURE_SHAPES.md",
        "contracts/schemas/failure_incident.schema.json",
        "contracts/schemas/failure_report.schema.json",
    ),
    "Y-033": (
        "src/yggdrasil/pi_citizen.py",
        "docs/PI_CITIZEN.md",
        "profiles/pi_citizen.json",
        "contracts/schemas/citizen_profile.schema.json",
        "contracts/schemas/citizen_report.schema.json",
    ),
    "Y-034": (
        "src/yggdrasil/laptop_citizen.py",
        "docs/LAPTOP_CITIZEN.md",
        "profiles/laptop_citizen.json",
    ),
    "Y-035": (
        "src/yggdrasil/vm_citizen.py",
        "docs/VM_CITIZEN.md",
        "profiles/vm_citizen.json",
    ),
    "Y-036": (
        "src/yggdrasil/phone_citizen.py",
        "docs/PHONE_CITIZEN.md",
        "profiles/phone_citizen.json",
    ),
    "Y-037": (
        "src/yggdrasil/model_fetch.py",
        "docs/MODEL_FETCH.md",
        "contracts/schemas/model_fetch_policy.schema.json",
        "contracts/schemas/model_fetch_request.schema.json",
        "contracts/schemas/model_fetch_record.schema.json",
        "contracts/schemas/model_fetch_report.schema.json",
    ),
    "Y-038": (
        "src/yggdrasil/quant_fitness.py",
        "docs/QUANT_FITNESS.md",
        "policies/quant_fitness.json",
        "contracts/schemas/quant_fitness_policy.schema.json",
        "contracts/schemas/quant_fitness_verdict.schema.json",
        "contracts/schemas/quant_fitness_report.schema.json",
    ),
    "Y-039": (
        "src/yggdrasil/node_journal.py",
        "docs/NODE_JOURNAL.md",
        "contracts/schemas/journal_entry.schema.json",
        "contracts/schemas/journal_report.schema.json",
    ),
    "Y-040": (
        "src/yggdrasil/degradation.py",
        "docs/DEGRADATION.md",
        "contracts/schemas/degradation_state.schema.json",
        "contracts/schemas/degradation_report.schema.json",
    ),
    "Y-041": (
        "src/yggdrasil/idle_generosity.py",
        "docs/IDLE_GENEROSITY.md",
        "contracts/schemas/generosity_policy.schema.json",
        "contracts/schemas/spare_capacity.schema.json",
        "contracts/schemas/generosity_report.schema.json",
    ),
    "Y-042": (
        "src/yggdrasil/operator_tools.py",
        "docs/OPERATOR_TOOLS.md",
        "contracts/schemas/operator_action.schema.json",
        "contracts/schemas/operator_report.schema.json",
        "contracts/schemas/node_inspection.schema.json",
    ),
    "Y-043": (
        "src/yggdrasil/task_security.py",
        "docs/TASK_SECURITY.md",
        "contracts/schemas/task_security_policy.schema.json",
        "contracts/schemas/task_signature.schema.json",
        "contracts/schemas/task_security_verdict.schema.json",
        "contracts/schemas/task_security_report.schema.json",
    ),
    "Y-044": (
        "src/yggdrasil/power_telemetry.py",
        "docs/POWER_TELEMETRY.md",
        "contracts/schemas/power_telemetry_reading.schema.json",
        "contracts/schemas/power_telemetry_report.schema.json",
        "contracts/schemas/governor_citation.schema.json",
    ),
    "Y-045": (
        "src/yggdrasil/multi_engine.py",
        "docs/MULTI_ENGINE.md",
        "contracts/schemas/engine_assignment.schema.json",
        "contracts/schemas/multi_engine_report.schema.json",
    ),
    "Y-046": (
        "src/yggdrasil/conformance.py",
        "docs/CONFORMANCE.md",
        "contracts/schemas/conformance_check.schema.json",
        "contracts/schemas/conformance_report.schema.json",
    ),
    "Y-047": (
        "src/yggdrasil/soak.py",
        "docs/SOAK.md",
        "contracts/schemas/soak_policy.schema.json",
        "contracts/schemas/soak_window.schema.json",
        "contracts/schemas/soak_report.schema.json",
    ),
}

PART2_SLICES = tuple(PART2_DELIVERABLES)  # Y-021 .. Y-047, in order


class Part2GateError(Exception):
    """Any Part II gate failure: a deliverable missing, a
    standing check red, a citizen not ready, its engine not
    conformant, or its task not served whole."""


def check_deliverables(root: Path | None = None) -> int:
    """Every file every Part II slice was banked on exists and
    is non-empty. Returns the number of files checked."""
    root = Path(root) if root is not None else _ROOT
    checked = 0
    for slice_id, paths in PART2_DELIVERABLES.items():
        for relative in paths:
            path = root / relative
            if not path.is_file():
                raise Part2GateError(f"{slice_id}: deliverable missing: {relative}")
            if path.stat().st_size == 0:
                raise Part2GateError(f"{slice_id}: deliverable is empty: {relative}")
            checked += 1
    return checked


def check_no_network_client(root: Path | None = None) -> int:
    """Part II grew real engine adapters, but the wire still
    belongs to deployment: no source module imports a network
    or process client or an engine SDK (the Y-020 scan, kept —
    adapters speak through injected transports, ADR-004).
    Returns the number of source modules scanned."""
    root = Path(root) if root is not None else _ROOT
    imports = source_imports(root)
    for module, names in sorted(imports.items()):
        banned = sorted(names & BANNED_IMPORTS)
        if banned:
            raise Part2GateError(
                f"{module} imports {banned} — the adapters speak through "
                f"injected transports; no source module holds a wire of its own"
            )
    return len(imports)


def _wrap_gate_error(fn, root):
    try:
        return fn(root)
    except GateError as exc:
        raise Part2GateError(str(exc)) from exc


# -- engine wire-shape doubles -------------------------------------------------
#
# The transports below are test doubles speaking each engine's
# real wire shapes — the same shapes Y-046's kit was proven
# against — so the gate's conformance runs and citizen tasks
# exercise the REAL adapters (Y-023 – Y-025) end to end. They
# are doubles, and every run they serve is declared SIMULATED.


class _StepTimer:
    """A deterministic timer for the adapters (each call a
    quarter-second later than the last)."""

    def __init__(self):
        self.now = 100.0

    def __call__(self) -> float:
        value = self.now
        self.now += 0.25
        return value


_OLLAMA_TAGS = {
    "models": [
        {
            "name": "fixture-llm:latest",
            "size": 4_700_000_000,
            "details": {
                "family": "llama",
                "parameter_size": "7B",
                "quantization_level": "Q4_K_M",
                "format": "gguf",
            },
        },
        {
            "name": "fixture-embed:latest",
            "size": 274_000_000,
            "details": {
                "family": "nomic-bert",
                "parameter_size": "137M",
                "quantization_level": "F16",
                "format": "gguf",
            },
        },
    ]
}
_OLLAMA_SHOW = {
    "fixture-llm:latest": {"capabilities": ["completion"]},
    "fixture-embed:latest": {"capabilities": ["embedding"]},
}


class _GateOllamaTransport:
    def __init__(self, *, unreachable: bool = False):
        self.unreachable = unreachable

    def _check(self):
        if self.unreachable:
            raise OllamaUnreachable("connection refused (gate transport)")

    def get_json(self, path: str) -> dict:
        self._check()
        if path == "/api/version":
            return {"version": "0.5.1"}
        if path == "/api/tags":
            return _OLLAMA_TAGS
        raise OllamaHttpError(404, f"unknown route {path} (gate)")

    def post_json(self, path: str, body: dict) -> dict:
        self._check()
        if path == "/api/show":
            return _OLLAMA_SHOW.get(body["name"], {})
        if path == "/api/generate":
            if body["model"] not in _OLLAMA_SHOW:
                raise OllamaHttpError(404, "model not found (gate)")
            return {"model": body["model"], "response": "the whole answer", "done": True}
        if path == "/api/embed":
            if body["model"] not in _OLLAMA_SHOW:
                raise OllamaHttpError(404, "model not found (gate)")
            return {"embeddings": [[0.1, 0.2] for _ in body["input"]]}
        raise OllamaHttpError(404, f"unknown route {path} (gate)")

    def post_lines(self, path: str, body: dict):
        self._check()
        if path != "/api/generate":
            raise OllamaHttpError(404, f"unknown route {path} (gate)")
        if body["model"] not in _OLLAMA_SHOW:
            raise OllamaHttpError(404, "model not found (gate)")
        yield json.dumps({"model": body["model"], "response": "Hello", "done": False})
        yield json.dumps({"model": body["model"], "response": ", mesh", "done": False})
        yield json.dumps({"model": body["model"], "response": "", "done": True})


_AESIR_RESIDENT = "gemma4-e2b:latest"


class _GateAesirTransport:
    def __init__(self, *, unreachable: bool = False):
        self.unreachable = unreachable

    def _check(self):
        if self.unreachable:
            raise AesirUnreachable("connection refused (gate transport)")

    def get_json(self, path: str) -> dict:
        self._check()
        if path == "/health/live":
            return {"schema_version": 1, "status": "ok", "scope": "process"}
        if path == "/health/ready":
            return {
                "schema_version": 1,
                "status": "ready",
                "ready": True,
                "model": _AESIR_RESIDENT,
                "digest": "sha256:fixturedigest",
                "backend": "cuda",
                "profile": "gemma4-e2b",
                "context": 4096,
            }
        if path == "/v1/models":
            return {
                "object": "list",
                "data": [
                    {"id": _AESIR_RESIDENT, "object": "model", "created": 1, "owned_by": "aesir"},
                    {"id": "qwen3-0.6b:latest", "object": "model", "created": 1, "owned_by": "aesir"},
                ],
            }
        raise AesirHttpError(404, f"unknown route {path} (gate)")

    def post_json(self, path: str, body: dict) -> dict:
        self._check()
        if path != "/v1/generate":
            raise AesirHttpError(404, f"unknown route {path} (gate)")
        return {"text": "the whole answer", "finish_reason": "stop", "model": _AESIR_RESIDENT}

    def post_lines(self, path: str, body: dict):
        self._check()
        if path != "/v1/generate":
            raise AesirHttpError(404, f"unknown route {path} (gate)")
        yield json.dumps({"text": "Hello", "done": False})
        yield json.dumps({"text": ", mesh", "done": False})
        yield json.dumps({"text": "", "done": True, "finish_reason": "stop", "model": _AESIR_RESIDENT})


_COMPAT_MODELS = {
    "object": "list",
    "data": [
        {
            "id": "fixture-llm",
            "object": "model",
            "family": "llama",
            "parameter_count": "7B",
            "quant": {"name": "Q4_K_M", "format": "gguf", "size_gb": 4.4},
        },
        {
            "id": "fixture-embed",
            "object": "model",
            "family": "nomic-bert",
            "quant": {"name": "F16", "format": "gguf", "size_gb": 0.26},
        },
    ],
}
_COMPAT_KNOWN = {"fixture-llm", "fixture-embed"}


class _GateCompatTransport:
    def __init__(self, *, unreachable: bool = False):
        self.unreachable = unreachable

    def _check(self):
        if self.unreachable:
            raise OpenAICompatUnreachable("connection refused (gate transport)")

    def get_json(self, path: str) -> dict:
        self._check()
        if path == "/health":
            return {"status": "ok"}
        if path == "/v1/models":
            return _COMPAT_MODELS
        raise OpenAICompatHttpError(404, f"unknown route {path} (gate)")

    def post_json(self, path: str, body: dict) -> dict:
        self._check()
        if path == "/v1/chat/completions":
            if body["model"] not in _COMPAT_KNOWN:
                raise OpenAICompatHttpError(404, "model not found (gate)")
            return {
                "choices": [
                    {"message": {"content": "the whole answer"}, "finish_reason": "stop"}
                ]
            }
        if path == "/v1/embeddings":
            if body["model"] not in _COMPAT_KNOWN:
                raise OpenAICompatHttpError(404, "model not found (gate)")
            return {
                "data": [
                    {"index": i, "embedding": [0.1, 0.2]} for i in range(len(body["input"]))
                ]
            }
        raise OpenAICompatHttpError(404, f"unknown route {path} (gate)")

    def post_lines(self, path: str, body: dict):
        self._check()
        if path != "/v1/chat/completions":
            raise OpenAICompatHttpError(404, f"unknown route {path} (gate)")
        if body["model"] not in _COMPAT_KNOWN:
            raise OpenAICompatHttpError(404, "model not found (gate)")
        yield 'data: {"choices": [{"delta": {"content": "Hello"}}]}'
        yield 'data: {"choices": [{"delta": {"content": ", mesh"}}]}'
        yield 'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}'
        yield "data: [DONE]"


def _build_adapter(engine_kind: str, *, unreachable: bool = False):
    """One of the real Y-023 – Y-025 adapters over a gate
    transport double, in the shape its citizen hosts."""
    if engine_kind == "ollama":
        return OllamaAdapter(_GateOllamaTransport(unreachable=unreachable), timer=_StepTimer())
    if engine_kind == "aesir":
        return AesirAdapter(
            _GateAesirTransport(unreachable=unreachable),
            ledger={"AES-SRV-010": "verified"},
            timer=_StepTimer(),
        )
    if engine_kind == "llamacpp":
        return LlamaCppAdapter(
            _GateCompatTransport(unreachable=unreachable),
            timer=_StepTimer(),
            embedding_models=["fixture-embed"],
        )
    raise Part2GateError(f"unknown gate engine kind: {engine_kind}")


# -- the three citizens ---------------------------------------------------------
#
# Each spec carries its machine's measured facts exactly as
# its citizen slice recorded them (Y-033: Mimir read directly
# 2026-10-31; Y-034: purplelongship read directly 2026-10-31;
# Y-035: this VM read directly 2026-10-31), injected as probes
# — so these runs are SIMULATED, and the live runs remain the
# citizen slices' own records.


@dataclass(frozen=True)
class CitizenSpec:
    citizen: str
    node_id: str
    device_class: str
    probes: ProbeSet
    neighbours: dict
    readings: dict
    engine_kind: str
    generate_model: str
    embed_model: str | None
    task_id: str


CITIZENS: tuple[CitizenSpec, ...] = (
    CitizenSpec(
        citizen="pi",
        node_id="mimir-1",
        device_class="pi",
        probes=ProbeSet(
            cpu=lambda: {
                "arch": "aarch64",
                "cores": 4,
                "model": "Raspberry Pi 5 Model B Rev 1.1",
            },
            ram_gb=lambda: 16598576 / (1024 * 1024),
            accelerators=lambda: [{"kind": "none"}],
            disk_free_gb=lambda: 202.0,
            thermal_c=lambda: 41.9,
            vram_gb=lambda: None,
        ),
        neighbours={
            "samtal_service": "active",
            "aurelia_port": "listening",
            "kiosk_port": "not_listening",
            "hailo_device": "absent",
        },
        readings={
            "thermal_c": 41.9,
            "memory_used_bytes": 2 * 1024**3,
            "memory_total_bytes": 16 * 1024**3,
            "cpu_share_used": 0.25,
        },
        engine_kind="ollama",
        generate_model="fixture-llm:latest",
        embed_model="fixture-embed:latest",
        task_id="11111111-1111-4111-8111-111111111111",
    ),
    CitizenSpec(
        citizen="laptop",
        node_id="purplelongship-1",
        device_class="laptop",
        probes=ProbeSet(
            cpu=lambda: {
                "arch": "x86_64",
                "cores": 16,
                "model": "AMD Ryzen 9 4900HS with Radeon Graphics",
            },
            ram_gb=lambda: 40465868 / (1024 * 1024),
            accelerators=lambda: [
                {"kind": "cuda", "name": "NVIDIA GeForce RTX 2060 with Max-Q Design"}
            ],
            disk_free_gb=lambda: 1100.0,
            thermal_c=lambda: 40.0,
            vram_gb=lambda: 6144 / 1024,
        ),
        neighbours={"desktop_session": "active", "cuda_device": "present"},
        readings={
            "thermal_c": 40.0,
            "memory_used_bytes": 8 * 1024**3,
            "memory_total_bytes": 38 * 1024**3,
            "cpu_share_used": 0.25,
        },
        engine_kind="aesir",
        generate_model=_AESIR_RESIDENT,
        embed_model=None,  # Aesir serves no embed route — the kit skips it honestly
        task_id="22222222-2222-4222-8222-222222222222",
    ),
    CitizenSpec(
        citizen="vm",
        node_id="vm-1",
        device_class="vm",
        probes=ProbeSet(
            cpu=lambda: {
                "arch": "x86_64",
                "cores": 4,
                "model": "AMD EPYC 9D64 88-Core Processor",
            },
            ram_gb=lambda: 8124908 / (1024 * 1024),
            accelerators=lambda: [{"kind": "none"}],
            disk_free_gb=lambda: 7.346,
            thermal_c=lambda: None,
            vram_gb=lambda: None,
        ),
        neighbours={"host_daemon": "active", "thermal_sensor": "absent"},
        readings={
            "thermal_c": None,
            "memory_used_bytes": 4 * 1024**3,
            "memory_total_bytes": 8 * 1024**3,
            "cpu_share_used": 0.25,
        },
        engine_kind="llamacpp",
        generate_model="fixture-llm",
        embed_model="fixture-embed",
        task_id="33333333-3333-4333-8333-333333333333",
    ),
)

_FACTORIES = {
    "pi": build_pi_citizen_agent,
    "laptop": build_laptop_citizen_agent,
    "vm": build_vm_citizen_agent,
}


@dataclass(frozen=True)
class CitizenGateRecord:
    """One citizen at the gate: ready on its machine's measured
    shape, its engine conformant, its task served whole."""

    citizen: str
    node_id: str
    engine: str
    ready: bool
    heartbeat: str
    conformance_verdict: str
    conformance_checks: int
    conformance_passed: int
    conformance_skipped: int
    task_status: str
    task_text: str
    failure_incidents: int


def _provision(work: Path, spec: CitizenSpec) -> NodeConfig:
    """A freshly provisioned identity for the gate citizen —
    the operator's act, done here by the gate: a mesh root, a
    node keypair, and a certificate for this device class, in
    a key store under the gate's work root."""
    store_dir = work / "keys"
    store = LocalKeyStore(store_dir)
    root_keypair, root_cert = create_mesh_root(now=FIXED)
    node_keypair = NodeKeypair.generate()
    cert = issue_node_certificate(
        root_keypair,
        root_cert,
        node_id=spec.node_id,
        node_keypair=node_keypair,
        device_class=spec.device_class,
        agent_version="0.0.1",
        now=FIXED,
    )
    store.save_certificate(root_cert)
    store.save_seed(spec.node_id, node_keypair.seed)
    store.save_certificate(cert)
    return NodeConfig.from_dict(
        {
            "version": 1,
            "node_id": spec.node_id,
            "device_class": spec.device_class,
            "key_store_dir": str(store_dir),
        }
    )


def run_citizen_gate(spec: CitizenSpec, work_root: Path) -> CitizenGateRecord:
    """One citizen through the gate: its engine adapter runs
    the Y-046 conformance kit to ``conformant``; the citizen
    itself is built by its real factory from its machine's
    measured shape, joins, heartbeats present, and serves one
    queued task end to end through that adapter."""
    work = Path(work_root) / spec.citizen
    work.mkdir(parents=True, exist_ok=True)
    try:
        adapter = _build_adapter(spec.engine_kind)
        dead = _build_adapter(spec.engine_kind, unreachable=True)
        report = run_conformance(
            adapter,
            node_id=spec.node_id,
            generate_model=spec.generate_model,
            embed_model=spec.embed_model,
            unreachable_adapter=dead,
            simulated=True,
            now=lambda: FIXED,
        )
        if report["verdict"] != "conformant":
            failed = [
                check["name"] for check in report["checks"] if check["status"] == "failed"
            ]
            raise Part2GateError(
                f"{spec.citizen}: engine {report['engine']} is not conformant "
                f"(failed: {', '.join(failed)})"
            )

        config = _provision(work, spec)
        factory = _FACTORIES[spec.citizen]
        kwargs: dict = {
            "store_root": work / "store",
            "probes": spec.probes,
            "readings": lambda: dict(spec.readings),
            "neighbours": dict(spec.neighbours),
            "simulated": True,
            "now": lambda: FIXED,
        }
        if spec.citizen in ("laptop", "vm"):
            # The citizen's own census carries its engine,
            # discovered through the same adapter the kit ran.
            kwargs["census_engines"] = lambda: [adapter.capability_for_census()]
        agent, citizen_report = factory(config, **kwargs)
        if citizen_report["ready"] is not True:
            failed = [
                c["name"] for c in citizen_report["checks"] if c["status"] == "fail"
            ]
            raise Part2GateError(
                f"{spec.citizen}: citizen not ready on its measured shape "
                f"(failed: {', '.join(failed)})"
            )

        agent.start()
        agent.join()
        fixture = work / "fixture-model.gguf"
        fixture.write_bytes(
            f"SIMULATED fixture model bytes for the Part II gate ({spec.citizen})".encode()
        )
        agent.model_store.add(
            fixture,
            name=spec.generate_model,
            family="fixture",
            quant="Q4_K_M",
            format="gguf",
        )
        agent.load_model(spec.generate_model)
        beat = agent.heartbeat()
        if beat["liveness"] != "present":
            raise Part2GateError(
                f"{spec.citizen}: heartbeat reads {beat['liveness']}, not present"
            )
        task = {
            "id": spec.task_id,
            "kind": "chat.generate",
            "payload": {"prompt": "gate check"},
            "caller": "part2-gate",
            "criticality": "standard",
            "requirements": {
                "model": spec.generate_model,
                "capabilities": ["generate"],
            },
        }
        agent.queue_task(task)
        outcome = agent.execute_next_queued(
            adapter_generate_runner(adapter, spec.node_id),
            engine=adapter.engine,
            model=spec.generate_model,
        )
        if outcome is None:
            raise Part2GateError(f"{spec.citizen}: the queued gate task was not served")
        result, _events, _sandbox_report = outcome
        if result["status"] != "completed":
            raise Part2GateError(
                f"{spec.citizen}: gate task ended {result['status']}, not completed"
            )
        text = (result.get("result") or {}).get("text", "")
        if text != "Hello, mesh":
            raise Part2GateError(
                f"{spec.citizen}: gate task text {text!r} is not the adapter's "
                f"streamed answer whole"
            )
        incidents = agent.failure_report()["total_incidents"]
        if incidents != 0:
            raise Part2GateError(
                f"{spec.citizen}: {incidents} failure incidents during the gate run"
            )
        agent.drain()
        agent.stop()
    except Part2GateError:
        raise
    except Exception as exc:  # a citizen that cannot be built or served fails its gate by name
        raise Part2GateError(f"{spec.citizen}: gate run failed: {exc}") from exc
    return CitizenGateRecord(
        citizen=spec.citizen,
        node_id=spec.node_id,
        engine=report["engine"],
        ready=True,
        heartbeat=beat["liveness"],
        conformance_verdict=report["verdict"],
        conformance_checks=report["totals"]["checks"],
        conformance_passed=report["totals"]["passed"],
        conformance_skipped=report["totals"]["skipped"],
        task_status=result["status"],
        task_text=text,
        failure_incidents=incidents,
    )


def check_citizens(
    work_root: Path, citizens: tuple[CitizenSpec, ...] = CITIZENS
) -> tuple[CitizenGateRecord, ...]:
    """All three citizens through the gate, in roadmap order:
    Pi, laptop, VM."""
    return tuple(run_citizen_gate(spec, Path(work_root)) for spec in citizens)


@dataclass(frozen=True)
class Part2GateReport:
    """The gate, passed: what was re-measured, in numbers a
    reader can re-measure from the tree."""

    slices_checked: int
    deliverable_files: int
    schemas_checked: int
    data_files: int
    provenance_items: int
    vendored_items: int
    campaign_parts: int
    campaign_slices: int
    next_slice: str
    source_modules: int
    citizens: tuple[CitizenGateRecord, ...]
    readme_md5: str


def check_part2_gate(
    root: Path | None = None, work_root: Path | None = None
) -> Part2GateReport:
    """The whole Y-048 gate in one call: Part II deliverables
    present, founding document untouched, every standing check
    from Y-016/Y-018/Y-019 still green, CI manual-only, no
    Docker, no source module holding a wire of its own, and
    the three citizens — Pi, laptop, VM — each running the
    conformance kit green against its engine and serving a
    task through it."""
    root = Path(root) if root is not None else _ROOT
    files = check_deliverables(root)
    readme_md5 = _wrap_gate_error(check_readme_unchanged, root)
    schemas = validate_schemas(root)
    data_files = len(validate_repo(root))
    provenance = check_provenance(root)
    if provenance.vendored_items != 0:
        raise Part2GateError(
            f"provenance reports {provenance.vendored_items} vendored items"
        )
    campaign = verify_campaign(root)
    banked = tuple(f"Y-{n:03d}" for n in range(1, 48))  # Y-001 .. Y-047
    nxt = first_unbanked(campaign, banked)
    if nxt is None or nxt["id"] != "Y-048":
        raise Part2GateError(
            f"campaign mirror names next slice {nxt and nxt['id']!r}, not Y-048 — "
            f"the gate is being run from the wrong point on the road"
        )
    _wrap_gate_error(check_ci_and_no_docker, root)
    modules = check_no_network_client(root)
    if work_root is None:
        work_root = Path(tempfile.mkdtemp(prefix="yggdrasil-part2-gate-"))
    citizens = check_citizens(Path(work_root))
    return Part2GateReport(
        slices_checked=len(PART2_DELIVERABLES),
        deliverable_files=files,
        schemas_checked=schemas,
        data_files=data_files,
        provenance_items=provenance.items_noticed,
        vendored_items=provenance.vendored_items,
        campaign_parts=campaign["total_parts"],
        campaign_slices=campaign["total_slices"],
        next_slice=nxt["id"],
        source_modules=modules,
        citizens=citizens,
        readme_md5=readme_md5,
    )
