"""Task security tests — Y-043's acceptance.

The guard and the agent must prove: (a) the policy is
data — it validates, the defaults declare no code kinds
and trust no signers, and an unshaped policy, task, or
signature block is refused by name; (b) a clean data task
is allowed as data, its verdict validated, with no event
emitted (data running is ordinary); (c) executable fields
anywhere in a data task's payload, at any depth, refuse
it as ``code_in_data_task`` / ``scope_denied`` with the
findings named by path — even when a signature travels
with it, because the *kind* must say so; (d) a declared
code kind without papers refuses ``code_kind_unsigned``,
an untrusted signer refuses ``signer_untrusted``, a
signature naming another task or a changed payload
refuses ``signature_mismatch``, a signature that does not
verify refuses ``signature_invalid``, and a properly
signed code task is allowed with ``signature_verified``
and a loud event; (e) the digest binds the exact payload
bytes; (f) the report counts and keeps every verdict, and
refusals are emitted as validated Y-013 events carrying
names and paths only, never payload content; (g) the node
agent judges before any lease or queue slot is spent — a
refused task takes no lease, is never held, and its runner
never runs — a signed code task queued with its papers is
served from the queue, and an unwired agent judges
nothing, exactly as before; (h) the VM citizen factory
wires a guard the built agent carries; (i) the founding
README is untouched.

The keys here are real generated Ed25519 keypairs (Y-011)
and the signatures really verify — the cryptography is not
simulated. No code task has run on named hardware, and
none is claimed.
"""

import base64
import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from yggdrasil.census import ProbeSet
from yggdrasil.identity import (
    LocalKeyStore,
    NodeKeypair,
    create_mesh_root,
    issue_node_certificate,
)
from yggdrasil.local_queue import LocalQueue
from yggdrasil.node_agent import NodeAgent, NodeAgentError, NodeConfig
from yggdrasil.observability import validate_event
from yggdrasil.sandbox import TaskSandbox
from yggdrasil.task_security import (
    TaskGuard,
    TaskSecurityError,
    default_policy,
    sign_task,
    task_payload_digest,
    validate_policy,
    validate_report,
    validate_verdict,
)
from yggdrasil.vm_citizen import build_vm_citizen_agent

ROOT = Path(__file__).resolve().parents[1]
FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
NODE_ID = "guard-node-1"
SIGNER_ID = "signer-node-1"
CODE_KIND = "ops.run_code"
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"
GIB = 1024**3


def _task(kind: str = "generate.text", payload: dict | None = None) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "kind": kind,
        "caller": "test-caller",
        "payload": payload if payload is not None else {"prompt": "hello"},
    }


def _code_policy(keypair: NodeKeypair) -> dict:
    policy = default_policy()
    policy["code_kinds"] = [CODE_KIND]
    policy["trusted_signers"] = [
        {
            "node_id": SIGNER_ID,
            "public_key": base64.b64encode(keypair.public_key).decode("ascii"),
        }
    ]
    return policy


# -- policy as data -----------------------------------------------------------


def test_default_policy_is_valid_and_declares_no_code():
    policy = validate_policy(default_policy())
    assert policy["code_kinds"] == []
    assert policy["trusted_signers"] == []
    assert "code" in policy["executable_fields"]
    assert "shell_command" in policy["executable_fields"]


def test_unshaped_records_are_refused_by_name():
    guard = TaskGuard(NODE_ID, now=lambda: FIXED)
    with pytest.raises(TaskSecurityError):
        TaskGuard(NODE_ID, {"version": 1}, now=lambda: FIXED)
    with pytest.raises(TaskSecurityError):
        guard.check({"id": "not-a-uuid", "kind": "x", "payload": {}, "caller": "c"})
    with pytest.raises(TaskSecurityError):
        guard.check(_task(), {"version": 1, "task_id": "nope"})
    with pytest.raises(TaskSecurityError):
        TaskGuard("", now=lambda: FIXED)


# -- data tasks -----------------------------------------------------------------


def test_a_clean_data_task_is_allowed_as_data():
    guard = TaskGuard(NODE_ID, now=lambda: FIXED)
    verdict = validate_verdict(guard.check(_task()))
    assert verdict["decision"] == "allow"
    assert verdict["reasons"] == ["data_task"]
    assert verdict["code_bearing"] is False
    assert verdict["signature_verified"] is False
    assert verdict["error_code"] is None
    assert verdict["findings"] == []
    # Data running is ordinary: no event is emitted.
    assert guard.events == []


def test_code_fields_in_a_data_task_are_refused_with_paths_named():
    guard = TaskGuard(NODE_ID, now=lambda: FIXED)
    task = _task(
        payload={
            "prompt": "hello",
            "steps": [{"script": "rm -rf /"}, {"note": "fine"}],
            "nested": {"deeper": {"shell_command": "ls"}},
        }
    )
    verdict = validate_verdict(guard.check(task))
    assert verdict["decision"] == "refuse"
    assert verdict["reasons"] == ["code_in_data_task"]
    assert verdict["error_code"] == "scope_denied"
    assert verdict["code_bearing"] is True
    assert verdict["findings"] == ["nested.deeper.shell_command", "steps[0].script"]
    # The refusal is an event: names and paths only, never content.
    (event,) = guard.events
    validate_event(event)
    assert event["type"] == "task_security.task_refused"
    assert "rm -rf" not in str(event)


def test_a_signature_never_promotes_a_data_kind():
    signer = NodeKeypair.generate()
    guard = TaskGuard(NODE_ID, _code_policy(signer), now=lambda: FIXED)
    task = _task(payload={"code": "print(1)"})
    signature = sign_task(signer, task, signer_node_id=SIGNER_ID)
    verdict = guard.check(task, signature)
    assert verdict["decision"] == "refuse"
    assert verdict["reasons"] == ["code_in_data_task"]
    assert verdict["signature_verified"] is False


# -- signed code tasks ------------------------------------------------------------


def test_a_signed_code_task_is_allowed_and_loud():
    signer = NodeKeypair.generate()
    guard = TaskGuard(NODE_ID, _code_policy(signer), now=lambda: FIXED)
    task = _task(kind=CODE_KIND, payload={"code": "print(1)", "language": "python"})
    signature = sign_task(signer, task, signer_node_id=SIGNER_ID)
    verdict = validate_verdict(guard.check(task, signature))
    assert verdict["decision"] == "allow"
    assert verdict["reasons"] == ["signed_code_task"]
    assert verdict["code_bearing"] is True
    assert verdict["signature_verified"] is True
    assert verdict["signer_node_id"] == SIGNER_ID
    assert verdict["findings"] == ["code"]
    (event,) = guard.events
    validate_event(event)
    assert event["type"] == "task_security.code_task_allowed"


def test_a_code_kind_without_papers_is_refused():
    signer = NodeKeypair.generate()
    guard = TaskGuard(NODE_ID, _code_policy(signer), now=lambda: FIXED)
    verdict = guard.check(_task(kind=CODE_KIND, payload={"code": "print(1)"}))
    assert verdict["decision"] == "refuse"
    assert verdict["reasons"] == ["code_kind_unsigned"]
    assert verdict["error_code"] == "scope_denied"


def test_an_untrusted_signer_is_refused():
    signer = NodeKeypair.generate()
    stranger = NodeKeypair.generate()
    guard = TaskGuard(NODE_ID, _code_policy(signer), now=lambda: FIXED)
    task = _task(kind=CODE_KIND, payload={"code": "print(1)"})
    signature = sign_task(stranger, task, signer_node_id="stranger-node-1")
    verdict = guard.check(task, signature)
    assert verdict["decision"] == "refuse"
    assert verdict["reasons"] == ["signer_untrusted"]
    assert verdict["error_code"] == "scope_denied"


def test_a_changed_payload_kills_the_signature():
    signer = NodeKeypair.generate()
    guard = TaskGuard(NODE_ID, _code_policy(signer), now=lambda: FIXED)
    task = _task(kind=CODE_KIND, payload={"code": "print(1)"})
    signature = sign_task(signer, task, signer_node_id=SIGNER_ID)
    task["payload"]["code"] = "print(2)"
    verdict = guard.check(task, signature)
    assert verdict["decision"] == "refuse"
    assert verdict["reasons"] == ["signature_mismatch"]
    assert verdict["error_code"] == "verification_failed"


def test_a_forged_signature_does_not_verify():
    signer = NodeKeypair.generate()
    impostor = NodeKeypair.generate()
    guard = TaskGuard(NODE_ID, _code_policy(signer), now=lambda: FIXED)
    task = _task(kind=CODE_KIND, payload={"code": "print(1)"})
    # Signed with the impostor's key, wearing the trusted signer's name.
    signature = sign_task(impostor, task, signer_node_id=SIGNER_ID)
    verdict = guard.check(task, signature)
    assert verdict["decision"] == "refuse"
    assert verdict["reasons"] == ["signature_invalid"]
    assert verdict["error_code"] == "verification_failed"


def test_the_digest_binds_the_exact_payload():
    task = _task(payload={"prompt": "hello"})
    digest = task_payload_digest(task)
    assert len(digest) == 64
    assert task_payload_digest(_task(payload={"prompt": "hello"})) != digest  # new id
    same = dict(task)
    assert task_payload_digest(same) == digest
    changed = _task(payload={"prompt": "hellp"})
    changed["id"] = task["id"]
    assert task_payload_digest(changed) != digest


# -- the ledger -------------------------------------------------------------------


def test_the_report_counts_and_keeps_every_verdict():
    signer = NodeKeypair.generate()
    guard = TaskGuard(NODE_ID, _code_policy(signer), now=lambda: FIXED)
    guard.check(_task())
    guard.check(_task(payload={"exec": "x"}))
    code_task = _task(kind=CODE_KIND, payload={"code": "print(1)"})
    guard.check(code_task, sign_task(signer, code_task, signer_node_id=SIGNER_ID))
    report = validate_report(guard.report())
    assert report["checked"] == 3
    assert report["allowed"] == 2
    assert report["refused"] == 1
    assert report["signed_code_allowed"] == 1
    assert len(report["verdicts"]) == 3


def test_require_allowed_raises_with_the_verdict():
    guard = TaskGuard(NODE_ID, now=lambda: FIXED)
    with pytest.raises(TaskSecurityError) as caught:
        guard.require_allowed(_task(payload={"wasm": "AAAA"}))
    assert caught.value.verdict["reasons"] == ["code_in_data_task"]
    assert caught.value.error_code == "scope_denied"
    assert "scope_denied" in str(caught.value)


# -- the agent's door ---------------------------------------------------------------


def _provisioned_agent(tmp_path: Path, **agent_kwargs) -> NodeAgent:
    store_dir = tmp_path / "keys"
    store = LocalKeyStore(store_dir)
    root_keypair, root_cert = create_mesh_root(now=FIXED)
    node_keypair = NodeKeypair.generate()
    cert = issue_node_certificate(
        root_keypair,
        root_cert,
        node_id=NODE_ID,
        node_keypair=node_keypair,
        device_class="vm",
        agent_version="0.0.1",
        now=FIXED,
    )
    store.save_certificate(root_cert)
    store.save_seed(NODE_ID, node_keypair.seed)
    store.save_certificate(cert)
    config = NodeConfig.from_dict(
        {
            "version": 1,
            "node_id": NODE_ID,
            "device_class": "vm",
            "key_store_dir": str(store_dir),
        }
    )
    return NodeAgent(config, now=lambda: FIXED, **agent_kwargs)


def _echo_runner(task, control):
    yield "ran"


def test_the_agent_refuses_code_before_any_lease(tmp_path: Path):
    guard = TaskGuard(NODE_ID, now=lambda: FIXED)
    agent = _provisioned_agent(
        tmp_path,
        sandbox=TaskSandbox(NODE_ID, now=lambda: FIXED),
        task_guard=guard,
    )
    agent.start()
    agent.join()
    ran = []

    def runner(task, control):
        ran.append(task["id"])
        yield "ran"

    with pytest.raises(NodeAgentError) as caught:
        agent.execute_task(
            _task(payload={"script": "echo hi"}),
            runner,
            engine="fixture",
            model="fixture-model",
        )
    assert "code_in_data_task" in str(caught.value)
    assert "scope_denied" in str(caught.value)
    assert ran == []
    assert agent.in_flight == ()
    # A clean task runs, and the ledger in status counts both judgements.
    result, _stream, _report = agent.execute_task(
        _task(), runner, engine="fixture", model="fixture-model"
    )
    assert result["status"] == "completed"
    report = validate_report(agent.task_security_report())
    assert report["checked"] == 2
    assert report["refused"] == 1
    assert agent.status()["task_security"]["allowed"] == 1
    agent.stop()


def test_the_agent_never_holds_a_refused_task(tmp_path: Path):
    signer = NodeKeypair.generate()
    guard = TaskGuard(NODE_ID, _code_policy(signer), now=lambda: FIXED)
    queue = LocalQueue(NODE_ID, now=lambda: FIXED)
    agent = _provisioned_agent(
        tmp_path,
        sandbox=TaskSandbox(NODE_ID, now=lambda: FIXED),
        local_queue=queue,
        task_guard=guard,
    )
    agent.start()
    agent.join()
    with pytest.raises(NodeAgentError):
        agent.queue_task(_task(payload={"bytecode": "AA=="}))
    assert agent.queue_report()["depth"] == 0
    # A signed code task is held with its papers, then served
    # from the queue without being re-judged unsigned.
    code_task = _task(kind=CODE_KIND, payload={"code": "print(1)"})
    signature = sign_task(signer, code_task, signer_node_id=SIGNER_ID)
    agent.queue_task(code_task, signature)
    assert agent.queue_report()["depth"] == 1
    result, _stream, _report = agent.execute_next_queued(
        _echo_runner, engine="fixture", model="fixture-model"
    )
    assert result["status"] == "completed"
    assert agent.queue_report()["depth"] == 0
    agent.stop()


def test_an_unwired_agent_judges_nothing(tmp_path: Path):
    agent = _provisioned_agent(
        tmp_path, sandbox=TaskSandbox(NODE_ID, now=lambda: FIXED)
    )
    assert agent.task_guard is None
    assert agent.task_security_report() is None
    assert agent.status()["task_security"] is None
    agent.start()
    agent.join()
    result, _stream, _report = agent.execute_task(
        _task(payload={"script": "echo hi"}),
        _echo_runner,
        engine="fixture",
        model="fixture-model",
    )
    assert result["status"] == "completed"
    agent.stop()


# -- the citizen factories -----------------------------------------------------------


def vm_probes() -> ProbeSet:
    """Probe set shaped from this VM's measured facts (the
    same fixture Y-035's tests use, duplicated here — the
    Y-018 provenance check rightly refuses cross-test
    imports of an undeclared ``tests`` package)."""
    return ProbeSet(
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
    )


def test_the_vm_citizen_factory_wires_a_guard(tmp_path: Path):
    store_dir = tmp_path / "keys"
    store = LocalKeyStore(store_dir)
    root_keypair, root_cert = create_mesh_root(now=FIXED)
    node_keypair = NodeKeypair.generate()
    cert = issue_node_certificate(
        root_keypair,
        root_cert,
        node_id="vm-citizen-1",
        node_keypair=node_keypair,
        device_class="vm",
        agent_version="0.0.1",
        now=FIXED,
    )
    store.save_certificate(root_cert)
    store.save_seed("vm-citizen-1", node_keypair.seed)
    store.save_certificate(cert)
    config = NodeConfig.from_dict(
        {
            "version": 1,
            "node_id": "vm-citizen-1",
            "device_class": "vm",
            "key_store_dir": str(store_dir),
        }
    )
    agent, _report = build_vm_citizen_agent(
        config,
        store_root=tmp_path / "models",
        probes=vm_probes(),
        readings=lambda: {
            "thermal_c": None,
            "memory_used_bytes": 2 * GIB,
            "memory_total_bytes": 8 * GIB,
            "cpu_share_used": 0.1,
        },
        simulated=True,
        now=lambda: FIXED,
        key_store=store,
    )
    assert agent.task_guard is not None
    # The citizen's guard runs the default policy: no code kinds.
    assert agent.task_guard.policy["code_kinds"] == []
    agent.start()
    agent.join()
    with pytest.raises(NodeAgentError):
        agent.execute_task(
            _task(payload={"code": "print(1)"}),
            _echo_runner,
            engine="fixture",
            model="fixture-model",
        )
    assert agent.status()["task_security"]["refused"] == 1
    agent.stop()


# -- the founding document ----------------------------------------------------------


def test_the_founding_readme_is_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
