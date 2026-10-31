"""Role-model tests — Y-012's acceptance.

The nine-worlds role model must (a) validate as data against its
schemas, (b) cover exactly the nine worlds the contract enum names,
(c) quote the founding spec's function for each world verbatim,
(d) bind no role to hardware, and (e) behave as an operator's hat
rack: assignable, changeable, removable — with criticality ceilings
that actually limit what a wearer may be handed. Catalog data is
SIMULATED paperwork; these tests prove the model's shape and the
assignment rules, not a running mesh (none enforces this yet).
"""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, validators
from referencing import Registry, Resource

from yggdrasil.roles import RoleBook, RoleCatalog, RoleError, validate_assignment

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "contracts" / "schemas"
CATALOG_RAW = json.loads((ROOT / "roles" / "role_catalog.json").read_text())
README = (ROOT / "README.md").read_text()
DEFINITIONS = CATALOG_RAW["roles"]


def load(name):
    return json.loads((SCHEMAS / name).read_text())


def registry():
    resources = []
    for path in sorted(SCHEMAS.glob("*.json")):
        schema = json.loads(path.read_text())
        resource = Resource.from_contents(schema)
        resources.append((path.name, resource))
        resources.append((schema["$id"], resource))
        resources.append((schema["$id"].rsplit("/", 1)[0] + "/" + path.name, resource))
    return Registry().with_resources(resources)


def make(name):
    schema = load(name)
    Draft202012Validator.check_schema(schema)
    return validators.validator_for(schema)(schema, registry=registry())


@pytest.fixture()
def catalog():
    return RoleCatalog.load()


@pytest.fixture()
def book(catalog):
    return RoleBook(catalog)


def test_schemas_are_valid():
    for name in ("role_definition.schema.json", "role_assignment.schema.json"):
        Draft202012Validator.check_schema(load(name))


def test_every_definition_validates():
    v = make("role_definition.schema.json")
    for definition in DEFINITIONS:
        assert list(v.iter_errors(definition)) == [], definition["role"]


def test_catalog_covers_exactly_the_nine_worlds():
    enum = load("roles.schema.json")["$defs"]["role"]["enum"]
    assert sorted(d["role"] for d in DEFINITIONS) == sorted(enum)
    assert len(DEFINITIONS) == 9


def test_functions_quote_the_spec_verbatim():
    # The catalog may not drift from the founding document: each
    # role's function is the README section 2.1 mapping, word for
    # word. Dash variants are normalised first — the README typesets
    # "Heavy-lift" with U+2011 non-breaking hyphens; the words, not
    # the typesetting, are what must match.
    readme = README
    for dash in "‐‑‒–—―":
        readme = readme.replace(dash, "-")
    for definition in DEFINITIONS:
        assert definition["function"] in readme, definition["role"]


def test_no_role_binds_hardware():
    assert all(d["binds_hardware"] is False for d in DEFINITIONS)
    v = make("role_definition.schema.json")
    bound = dict(DEFINITIONS[0], binds_hardware=True)
    assert list(v.iter_errors(bound)) != []


def test_catalog_loader_rejects_tampered_data(tmp_path):
    raw = json.loads((ROOT / "roles" / "role_catalog.json").read_text())
    raw["roles"] = raw["roles"][:-1]  # a world goes missing
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(RoleError):
        RoleCatalog.load(path)
    raw["roles"] = DEFINITIONS + [dict(DEFINITIONS[0])]  # duplicate hat
    path.write_text(json.dumps(raw))
    with pytest.raises(RoleError):
        RoleCatalog.load(path)


def test_assignment_schema_accepts_and_rejects():
    v = make("role_assignment.schema.json")
    good = {
        "node_id": "forge-pi-01",
        "roles": ["svartalfheim"],
        "assigned_by": "operator",
        "assigned_at": "2026-10-30T21:50:00Z",
    }
    assert list(v.iter_errors(good)) == []
    assert list(v.iter_errors(dict(good, roles=["atlantis"]))) != []
    assert list(v.iter_errors(dict(good, node_id="BAD ID!"))) != []


def test_assign_and_change_hats(book):
    book.assign("forge-pi-01", ["svartalfheim"], assigned_by="operator")
    assert book.roles_for("forge-pi-01") == ("svartalfheim",)
    # The operator changes their mind: new hats supersede, history keeps both.
    book.assign("forge-pi-01", ["jotunheim", "muspelheim"], assigned_by="operator")
    assert book.roles_for("forge-pi-01") == ("jotunheim", "muspelheim")
    assert len(book.history()) == 2


def test_unassign_removes_and_records(book):
    book.assign("forge-pi-01", ["niflheim"], assigned_by="operator")
    book.unassign("forge-pi-01", assigned_by="operator")
    assert book.roles_for("forge-pi-01") == ()
    assert book.history()[-1]["roles"] == []
    with pytest.raises(RoleError):
        book.unassign("forge-pi-01", assigned_by="operator")


def test_assign_rejects_empty_and_unknown(book):
    with pytest.raises(RoleError):
        book.assign("forge-pi-01", [], assigned_by="operator")
    with pytest.raises(RoleError):
        book.assign("forge-pi-01", ["atlantis"], assigned_by="operator")


def test_ceilings_limit_what_a_wearer_may_be_handed(book):
    book.assign("worker-01", ["svartalfheim"], assigned_by="operator")
    assert book.ceiling_for("worker-01") == "standard"
    assert book.can_accept("worker-01", "casual")
    assert book.can_accept("worker-01", "standard")
    assert not book.can_accept("worker-01", "critical")
    # A second, higher hat raises the node's effective ceiling.
    book.assign("worker-01", ["svartalfheim", "nidavellir"], assigned_by="operator")
    assert book.ceiling_for("worker-01") == "critical"
    assert book.can_accept("worker-01", "critical")


def test_hatless_node_may_be_handed_nothing(book):
    assert book.ceiling_for("stranger-01") is None
    assert not book.can_accept("stranger-01", "casual")
    assert not book.eligible("stranger-01", criticality="casual")


def test_eligible_combines_ceiling_and_capabilities(book):
    book.assign("skald-01", ["alfheim"], assigned_by="operator")
    assert book.eligible("skald-01", criticality="standard", capabilities=("vision",))
    assert not book.eligible("skald-01", criticality="standard", capabilities=("audit",))
    assert not book.eligible("skald-01", criticality="critical", capabilities=("vision",))
    with pytest.raises(RoleError):
        book.can_accept("skald-01", "dire")


def test_validate_assignment_against_catalog(catalog):
    validate_assignment(
        {
            "node_id": "forge-pi-01",
            "roles": ["asgard"],
            "assigned_by": "operator",
            "assigned_at": "2026-10-30T21:50:00Z",
        },
        catalog,
    )


def test_assignment_projects_into_node_identity(book):
    # The hats a book record grants are exactly the roles list the
    # Y-006 NodeIdentity contract carries.
    record = book.assign("forge-pi-01", ["svartalfheim"], assigned_by="operator")
    identity = {
        "node_id": "forge-pi-01",
        "public_key": "x" * 44,
        "device_class": "pi",
        "agent_version": "0.0.1",
        "roles": record["roles"],
    }
    v = make("node_identity.schema.json")
    assert list(v.iter_errors(identity)) == []
