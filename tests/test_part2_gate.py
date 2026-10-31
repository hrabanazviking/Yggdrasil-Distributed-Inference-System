"""Part II gate tests — Y-048's acceptance.

The gate must prove: (a) every slice Y-021 – Y-047 is present
in the tree on the files it was banked on; (b) the founding
README is byte-for-byte untouched and every standing check
the earlier slices built still passes when run from the gate
— schemas, data discipline, provenance, and the campaign
mirror naming Y-048 next; (c) CI is still manual-dispatch
only, no Docker file exists, and no source module holds a
network or process client of its own; and (d) the gate's own
exercise runs green: the three citizens (Pi, laptop, VM) are
each built by their real factories from their machines'
measured shapes, evaluate ready, heartbeat present, run the
Y-046 conformance kit to a conformant verdict against their
engine adapters (Ollama, Aesir, llama.cpp — the real Y-023 –
Y-025 adapters over wire-shape transport doubles, declared
SIMULATED), and each serves one queued task end to end
through its adapter with the streamed text whole and zero
failure incidents. The gate reads the tree and runs the
citizens, not the ledger: a missing deliverable, a touched
README, or a citizen whose measured shape is not its own
fails it by name.
"""

import dataclasses
import hashlib
import shutil
import tempfile
from pathlib import Path

import pytest

from yggdrasil.census import ProbeSet
from yggdrasil.part1_gate import check_ci_and_no_docker
from yggdrasil.part2_gate import (
    CITIZENS,
    PART2_DELIVERABLES,
    PART2_SLICES,
    Part2GateError,
    check_citizens,
    check_deliverables,
    check_no_network_client,
    check_part2_gate,
    run_citizen_gate,
)

ROOT = Path(__file__).resolve().parents[1]
WORK = Path(tempfile.mkdtemp(prefix="yggdrasil-part2-gate-test-"))
REPORT = check_part2_gate(work_root=WORK)
README_MD5 = "89a37a4e18cab9a998e1751c32e7bfd5"


def _mini_tree(tmp_path: Path) -> Path:
    """A scratch copy of the tree (no venv/caches/git/scratch),
    so gate failures can be proven on damage the real tree
    must not take."""
    tree = tmp_path / "tree"
    shutil.copytree(
        ROOT,
        tree,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache",
            ".tmp_local", "*.egg-info",
        ),
    )
    return tree


def test_the_gate_passes_on_the_real_tree():
    assert REPORT.slices_checked == 27
    assert REPORT.next_slice == "Y-048"
    assert REPORT.campaign_parts == 11
    assert REPORT.campaign_slices == 244
    assert REPORT.vendored_items == 0
    assert REPORT.readme_md5 == README_MD5
    assert [record.citizen for record in REPORT.citizens] == ["pi", "laptop", "vm"]


def test_every_part2_slice_names_its_deliverables():
    assert PART2_SLICES == tuple(f"Y-{n:03d}" for n in range(21, 48))
    assert all(PART2_DELIVERABLES[slice_id] for slice_id in PART2_SLICES)
    assert REPORT.deliverable_files == sum(
        len(paths) for paths in PART2_DELIVERABLES.values()
    )
    assert check_deliverables() == REPORT.deliverable_files


def test_standing_checks_are_green_from_the_gate():
    assert REPORT.schemas_checked >= 24  # Part I's gate count, since grown
    assert REPORT.data_files >= 5
    assert REPORT.provenance_items >= 17


def test_ci_manual_only_no_docker_and_no_wire_of_our_own():
    check_ci_and_no_docker()  # raises on any fault
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "workflow_dispatch" in ci
    scanned = check_no_network_client()
    assert scanned == REPORT.source_modules
    assert scanned >= 30


def test_the_three_citizens_ran_the_kit_green_and_served():
    by_citizen = {record.citizen: record for record in REPORT.citizens}
    assert by_citizen["pi"].engine == "ollama"
    assert by_citizen["laptop"].engine == "aesir"
    assert by_citizen["vm"].engine == "llamacpp"
    for record in REPORT.citizens:
        assert record.ready is True
        assert record.heartbeat == "present"
        assert record.conformance_verdict == "conformant"
        assert record.conformance_checks == 16
        assert record.conformance_passed + record.conformance_skipped == 16
        assert record.task_status == "completed"
        assert record.task_text == "Hello, mesh"
        assert record.failure_incidents == 0
    # Aesir serves no embed route: the kit states the skip
    # instead of passing it quietly (Y-024/Y-046 honesty).
    assert by_citizen["laptop"].conformance_skipped >= 1


def test_the_citizens_can_be_re_run_from_their_specs(tmp_path):
    records = check_citizens(tmp_path / "again", citizens=CITIZENS[2:])
    assert [record.citizen for record in records] == ["vm"]
    assert records[0].conformance_verdict == "conformant"


def test_a_citizen_on_another_machines_shape_fails_the_gate(tmp_path):
    vm = CITIZENS[2]
    wrong_shape = dataclasses.replace(
        vm,
        probes=ProbeSet(
            cpu=lambda: {"arch": "x86_64", "cores": 4, "model": "Gate Test CPU"},
            ram_gb=lambda: 8124908 / (1024 * 1024),
            accelerators=lambda: [{"kind": "cuda", "name": "Some GPU"}],
            disk_free_gb=lambda: 7.346,
            thermal_c=lambda: None,
            vram_gb=lambda: 6.0,
        ),
    )
    with pytest.raises(Part2GateError, match="vm"):
        run_citizen_gate(wrong_shape, tmp_path / "wrong")


def test_an_unknown_gate_engine_fails_by_name(tmp_path):
    bogus = dataclasses.replace(CITIZENS[0], engine_kind="bogus")
    with pytest.raises(Part2GateError, match="unknown gate engine"):
        run_citizen_gate(bogus, tmp_path / "bogus")


def test_a_missing_deliverable_fails_the_gate(tmp_path):
    tree = _mini_tree(tmp_path)
    (tree / "docs" / "SOAK.md").unlink()
    with pytest.raises(Part2GateError, match="Y-047"):
        check_deliverables(tree)


def test_an_emptied_deliverable_fails_the_gate(tmp_path):
    tree = _mini_tree(tmp_path)
    (tree / "docs" / "NODE_AGENT.md").write_text("")
    with pytest.raises(Part2GateError, match="Y-021"):
        check_deliverables(tree)


def test_a_touched_readme_fails_the_gate(tmp_path):
    tree = _mini_tree(tmp_path)
    readme = tree / "README.md"
    readme.write_bytes(readme.read_bytes() + b"\n")
    with pytest.raises(Part2GateError, match="README"):
        check_part2_gate(tree, work_root=tmp_path / "w")


def test_the_founding_readme_is_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5
