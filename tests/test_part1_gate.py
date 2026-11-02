"""Part I gate tests — Y-020's acceptance.

The gate must prove: (a) every slice Y-001 – Y-019 is present in
the tree on the files it was banked on; (b) the founding README
is byte-for-byte untouched and the concordance is published
in-repo with its deferrals named; (c) every standing check the
earlier slices built still passes when run from the gate —
schemas, data discipline, provenance, and the campaign mirror
naming Y-020 next; (d) CI is still manual-dispatch only and no
Docker file exists; (e) the simulator skeleton runs a SIMULATED
task end to end; and (f) there is zero inference code yet —
banned imports are refused by name, in a scratch tree as in the
real one. The gate reads the tree, not the ledger: a missing
deliverable fails it however the log reads.
"""

import shutil
from pathlib import Path

import pytest

from yggdrasil.part1_gate import (
    PART1_DELIVERABLES,
    PART1_SLICES,
    GateError,
    check_ci_and_no_docker,
    check_concordance,
    check_deliverables,
    check_no_inference_code,
    check_part1_gate,
    check_readme_unchanged,
    check_simulator_skeleton,
    source_imports,
)

ROOT = Path(__file__).resolve().parents[1]
REPORT = check_part1_gate()


def _mini_tree(tmp_path: Path) -> Path:
    """A scratch copy of the tree (no venv/caches/git), so gate
    failures can be proven on damage the real tree must not take."""
    tree = tmp_path / "tree"
    shutil.copytree(
        ROOT,
        tree,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache",
            ".tmp", "*.egg-info",
        ),
    )
    return tree


def test_the_gate_passes_on_the_real_tree():
    assert REPORT.slices_checked == 19
    assert REPORT.next_slice == "Y-020"
    assert REPORT.campaign_parts == 11
    assert REPORT.campaign_slices == 244
    assert REPORT.vendored_items == 0
    assert REPORT.simulator_nodes == 3
    assert REPORT.readme_md5 == "89a37a4e18cab9a998e1751c32e7bfd5"


def test_every_part1_slice_names_its_deliverables():
    assert PART1_SLICES == tuple(f"Y-{n:03d}" for n in range(1, 20))
    assert all(PART1_DELIVERABLES[slice_id] for slice_id in PART1_SLICES)
    # 19 slices, each banked on named files — the count the gate
    # document reports, re-measured here.
    assert REPORT.deliverable_files == sum(
        len(paths) for paths in PART1_DELIVERABLES.values()
    )
    assert check_deliverables() == REPORT.deliverable_files


def test_readme_unchanged_and_concordance_published():
    assert check_readme_unchanged() == "89a37a4e18cab9a998e1751c32e7bfd5"
    rows = check_concordance()
    assert rows == REPORT.concordance_rows
    assert rows >= 30


def test_standing_checks_are_green_from_the_gate():
    assert REPORT.schemas_checked >= 20
    assert REPORT.data_files >= 4
    assert REPORT.provenance_items >= 17


def test_ci_manual_only_and_no_docker():
    check_ci_and_no_docker()  # raises on any fault
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "workflow_dispatch" in ci


def test_simulator_skeleton_runs_simulated():
    assert check_simulator_skeleton() == 3


def test_zero_inference_code_in_the_real_tree():
    scanned = check_no_inference_code()
    assert scanned == REPORT.source_modules
    assert scanned >= 10
    imports = source_imports()
    # The only engine-shaped module is the fake engines.
    assert "fake_engines.py" in imports


def test_a_missing_deliverable_fails_the_gate(tmp_path):
    tree = _mini_tree(tmp_path)
    (tree / "docs" / "adr" / "ADR-002-transport.md").unlink()
    with pytest.raises(GateError, match="Y-003"):
        check_deliverables(tree)


def test_an_emptied_deliverable_fails_the_gate(tmp_path):
    tree = _mini_tree(tmp_path)
    (tree / "docs" / "SIMULATOR.md").write_text("")
    with pytest.raises(GateError, match="Y-014"):
        check_deliverables(tree)


def test_a_touched_readme_fails_the_gate(tmp_path):
    tree = _mini_tree(tmp_path)
    readme = tree / "README.md"
    readme.write_bytes(readme.read_bytes() + b"\n")
    with pytest.raises(GateError, match="README"):
        check_readme_unchanged(tree)


def test_banned_imports_fail_the_gate_by_name(tmp_path):
    tree = _mini_tree(tmp_path)
    module = tree / "src" / "yggdrasil" / "sneaky.py"
    module.write_text("import torch\n\nVALUE = 1\n")
    with pytest.raises(GateError, match="torch"):
        check_no_inference_code(tree)
    module.write_text("from openai import OpenAI\n")
    with pytest.raises(GateError, match="openai"):
        check_no_inference_code(tree)
    # A banned name in a comment or a string is data, not code.
    module.write_text('# torch would go here, in Part II\nNAME = "ollama"\n')
    check_no_inference_code(tree)


def test_a_docker_file_fails_the_gate(tmp_path):
    tree = _mini_tree(tmp_path)
    (tree / "Dockerfile").write_text("FROM python:3.12\n")
    with pytest.raises(GateError, match="Docker"):
        check_ci_and_no_docker(tree)
