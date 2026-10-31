"""Provenance tests — Y-018's acceptance.

The register (provenance/provenance_register.json) must (a) be a
proper Y-016 data file — registered in the manifest and validated
by validate_repo; (b) agree with LICENSE and pyproject.toml on the
project's licence and holder; (c) notice every declared dependency
with the same constraint; (d) leave no direct import undeclared —
the referencing gap Y-018 found and fixed must stay fixed; and
(e) keep vendored at zero: nothing third-party is copied into this
tree. The founding README's md5 is asserted unchanged — the spec
is the founding document and this campaign never edits it.
"""

import hashlib
import shutil
from pathlib import Path

import pytest

from yggdrasil.data_discipline import validate_repo
from yggdrasil.provenance import (
    ProvenanceError,
    assert_declared_noticed,
    assert_imports_declared,
    check_provenance,
    declared_dependencies,
    load_register,
    third_party_imports,
    validate_items,
)

ROOT = Path(__file__).resolve().parents[1]
REGISTER = load_register(ROOT)
ITEMS = REGISTER["items"]
README_MD5_AT_CAMPAIGN_OPEN = "89a37a4e18cab9a998e1751c32e7bfd5"


def test_register_is_a_registered_data_file():
    reports = {r.id: r for r in validate_repo()}
    assert reports["provenance_register"].items_checked == len(ITEMS) >= 15
    assert reports["provenance_register"].version == "yggdrasil-provenance-register-v1"


def test_check_provenance_passes_on_the_real_repo():
    report = check_provenance(ROOT)
    assert report.project_licence == "MIT"
    assert report.copyright_holder == "Volmarr Wyrd"
    assert report.items_noticed == len(ITEMS)
    assert report.vendored_items == 0


def test_founding_readme_untouched():
    digest = hashlib.md5((ROOT / "README.md").read_bytes()).hexdigest()
    assert digest == README_MD5_AT_CAMPAIGN_OPEN


def test_every_declared_dependency_is_noticed():
    declared = declared_dependencies(ROOT)
    # The known set today: build + runtime + dev, referencing included.
    assert set(declared) == {"setuptools", "jsonschema", "referencing", "pytest", "ruff"}
    assert_declared_noticed(declared, ITEMS)


def test_no_direct_import_is_undeclared():
    imports = third_party_imports(ROOT)
    assert imports == {"jsonschema", "referencing", "pytest"}
    assert_imports_declared(imports, declared_dependencies(ROOT))


def test_runtime_transitive_closure_is_noticed():
    names = {item["name"] for item in ITEMS if item["kind"] == "transitive_dependency"}
    assert names == {"attrs", "jsonschema-specifications", "rpds-py", "typing-extensions"}


def test_nothing_is_vendored():
    assert all(item["vendored"] is False for item in ITEMS)


def test_founding_documents_are_noticed_and_present():
    docs = [i for i in ITEMS if i["kind"] == "founding_document"]
    assert len(docs) == 3
    for item in docs:
        for relative in item["paths"]:
            assert (ROOT / relative).exists(), relative


def test_unnoticed_dependency_is_refused():
    declared = declared_dependencies(ROOT)
    declared["sneaky-lib"] = ">=1.0"
    with pytest.raises(ProvenanceError, match="not noticed"):
        assert_declared_noticed(declared, ITEMS)


def test_constraint_drift_is_refused():
    declared = declared_dependencies(ROOT)
    declared["jsonschema"] = ">=99.0"
    with pytest.raises(ProvenanceError, match="constraint"):
        assert_declared_noticed(declared, ITEMS)


def test_undeclared_import_is_refused():
    with pytest.raises(ProvenanceError, match="not a declared dependency"):
        assert_imports_declared({"referencing"}, {"jsonschema": ">=4.10"})


def test_bad_item_shape_is_refused():
    bad = [dict(ITEMS[0], kind="shareware")]
    with pytest.raises(ProvenanceError, match="schema"):
        validate_items(bad, ROOT)


def _mini_repo(tmp_path: Path) -> Path:
    """A copy of the real repo's provenance-relevant files, so
    tamper cases run against the checker end to end."""
    root = tmp_path / "repo"
    root.mkdir()
    for name in ("LICENSE", "README.md", "pyproject.toml"):
        shutil.copy(ROOT / name, root / name)
    shutil.copytree(ROOT / "contracts", root / "contracts")
    shutil.copytree(ROOT / "provenance", root / "provenance")
    shutil.copytree(ROOT / "src", root / "src", ignore=shutil.ignore_patterns("__pycache__"))
    (root / "Yggdrasil_WorldNet_The_Web4.0_ Architecture.md").write_text("x")
    (root / "Volmarr_Cyber_Viking_Master_Visual_Design_Lawbook.md").write_text("x")
    (root / "Volmarr_Cyber_Viking_Master_Visual_Design_Lawbook.pdf").write_text("x")
    return root


def test_licence_disagreement_is_refused(tmp_path):
    root = _mini_repo(tmp_path)
    text = (root / "pyproject.toml").read_text().replace(
        'license = { text = "MIT" }', 'license = { text = "GPL-3.0" }'
    )
    (root / "pyproject.toml").write_text(text)
    with pytest.raises(ProvenanceError, match="pyproject licence"):
        check_provenance(root)


def test_missing_noticed_path_is_refused(tmp_path):
    root = _mini_repo(tmp_path)
    (root / "Yggdrasil_WorldNet_The_Web4.0_ Architecture.md").unlink()
    with pytest.raises(ProvenanceError, match="does not exist"):
        check_provenance(root)
