"""Nine-worlds role model — Y-012.

Roles are hats, not hardware. The founding spec (README section
2.1) names nine worlds and the function each serves in the mesh;
this slice turns that mapping into *data* — a validated catalog of
role definitions (``roles/role_catalog.json``, shaped by
``contracts/schemas/role_definition.schema.json``) — plus an
operator-changeable assignment book: which node currently wears
which hats (``contracts/schemas/role_assignment.schema.json``).

A role definition states the capabilities the mesh may expect of a
wearer and a *criticality ceiling*: the highest task criticality
(Y-007: casual < standard < critical) a wearer may be handed under
that hat. A node's effective ceiling is the highest ceiling among
its hats. Nothing here binds a role to a device class — any node
may wear any hat the operator gives it, and the operator may move
hats at any time by issuing a new assignment, which supersedes the
old one and is kept in the book's history.

Honesty: Y-012 defines and validates the model. Carrying
assignments over the fabric is Part III work, and the router that
*enforces* ceilings when placing tasks is Part IV (Y-075 onward).
No running mesh enforces any of this yet, and this module does not
claim one does.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from jsonschema import validators
from referencing import Registry, Resource

_ROOT = Path(__file__).resolve().parents[2]
_CATALOG_PATH = _ROOT / "roles" / "role_catalog.json"
_SCHEMAS = _ROOT / "contracts" / "schemas"


def _load_schema(name: str) -> dict:
    return json.loads((_SCHEMAS / name).read_text())


def _validator(name: str):
    """A validator for one schema, with every contract schema
    registered so relative $refs (e.g. roles.schema.json) resolve —
    the same registry the Y-009 contract harness builds."""
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


_DEFINITION_VALIDATOR = _validator("role_definition.schema.json")
_ASSIGNMENT_VALIDATOR = _validator("role_assignment.schema.json")
_KNOWN_ROLES = set(
    _load_schema("roles.schema.json")["$defs"]["role"]["enum"]
)

# Y-007's criticality ladder, lowest first.
CRITICALITY_ORDER = {"casual": 0, "standard": 1, "critical": 2}


class RoleError(Exception):
    """Any role-catalog or role-assignment failure."""


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class RoleCatalog:
    """The nine role definitions, validated as data."""

    def __init__(self, definitions: list[dict]):
        self._by_role: dict[str, dict] = {}
        for definition in definitions:
            errors = sorted(
                _DEFINITION_VALIDATOR.iter_errors(definition), key=str
            )
            if errors:
                raise RoleError(
                    f"role definition fails its schema: {errors[0].message}"
                )
            role = definition["role"]
            if role in self._by_role:
                raise RoleError(f"duplicate role definition: {role}")
            self._by_role[role] = definition
        missing = _KNOWN_ROLES - set(self._by_role)
        extra = set(self._by_role) - _KNOWN_ROLES
        if missing or extra:
            raise RoleError(
                f"catalog must define exactly the nine worlds; "
                f"missing={sorted(missing)} extra={sorted(extra)}"
            )

    @classmethod
    def load(cls, path: Path | None = None) -> "RoleCatalog":
        """Load and validate a catalog file (default: the repo's)."""
        path = path or _CATALOG_PATH
        try:
            raw = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise RoleError(f"cannot load role catalog {path}: {exc}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("roles"), list):
            raise RoleError(f"role catalog {path} has no 'roles' list")
        return cls(raw["roles"])

    def roles(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_role))

    def definition(self, role: str) -> dict:
        try:
            return self._by_role[role]
        except KeyError:
            raise RoleError(f"unknown role: {role}") from None

    def ceiling(self, role: str) -> str:
        return self.definition(role)["criticality_ceiling"]

    def expected_capabilities(self, role: str) -> frozenset[str]:
        return frozenset(self.definition(role)["expected_capabilities"])


def validate_assignment(assignment: dict, catalog: RoleCatalog) -> None:
    """Raise RoleError unless an assignment record is well-formed
    and every hat it names exists in the catalog."""
    errors = sorted(_ASSIGNMENT_VALIDATOR.iter_errors(assignment), key=str)
    if errors:
        raise RoleError(f"assignment fails its schema: {errors[0].message}")
    for role in assignment["roles"]:
        catalog.definition(role)  # raises RoleError on an unknown hat


class RoleBook:
    """Who wears which hats, as the operator currently has it.

    Assignments are operator acts: ``assign`` supersedes the node's
    previous hats wholesale, ``unassign`` removes them, and every
    act is preserved in ``history`` so a change is a fact with an
    author and a time, never a silent edit. A removal appears in
    history as a record with an empty roles list.
    """

    def __init__(self, catalog: RoleCatalog):
        self.catalog = catalog
        self._current: dict[str, dict] = {}
        self._history: list[dict] = []

    def assign(
        self,
        node_id: str,
        roles: list[str],
        *,
        assigned_by: str,
        at: datetime | None = None,
        note: str | None = None,
    ) -> dict:
        if not roles:
            raise RoleError("assign at least one hat; use unassign to remove hats")
        record = {
            "node_id": node_id,
            "roles": sorted(set(roles)),
            "assigned_by": assigned_by,
            "assigned_at": _fmt(at or datetime.now(UTC)),
        }
        if note is not None:
            record["note"] = note
        validate_assignment(record, self.catalog)
        self._current[node_id] = record
        self._history.append(record)
        return record

    def unassign(self, node_id: str, *, assigned_by: str) -> None:
        if node_id not in self._current:
            raise RoleError(f"node {node_id} wears no hats to remove")
        del self._current[node_id]
        self._history.append(
            {
                "node_id": node_id,
                "roles": [],
                "assigned_by": assigned_by,
                "assigned_at": _fmt(datetime.now(UTC)),
                "note": "all hats removed",
            }
        )

    def history(self) -> tuple[dict, ...]:
        return tuple(self._history)

    def roles_for(self, node_id: str) -> tuple[str, ...]:
        record = self._current.get(node_id)
        if record is None:
            return ()
        return tuple(record["roles"])

    def ceiling_for(self, node_id: str) -> str | None:
        """The node's effective ceiling: its highest hat's ceiling,
        or None when it wears no hat and may be handed nothing."""
        hats = self.roles_for(node_id)
        if not hats:
            return None
        return max(
            (self.catalog.ceiling(hat) for hat in hats),
            key=lambda name: CRITICALITY_ORDER[name],
        )

    def expected_capabilities_for(self, node_id: str) -> frozenset[str]:
        found: set[str] = set()
        for hat in self.roles_for(node_id):
            found |= self.catalog.expected_capabilities(hat)
        return frozenset(found)

    def can_accept(self, node_id: str, criticality: str) -> bool:
        """True when the node's hats' ceiling covers this criticality."""
        if criticality not in CRITICALITY_ORDER:
            raise RoleError(f"unknown criticality: {criticality}")
        ceiling = self.ceiling_for(node_id)
        if ceiling is None:
            return False
        return CRITICALITY_ORDER[criticality] <= CRITICALITY_ORDER[ceiling]

    def eligible(
        self, node_id: str, *, criticality: str, capabilities: tuple[str, ...] = ()
    ) -> bool:
        """The router's question, in one place: may this node be
        handed work of this criticality needing these capabilities
        under the hats it currently wears? (Enforcement at placement
        time is Part IV; this is the predicate it will use.)"""
        if not self.can_accept(node_id, criticality):
            return False
        return set(capabilities) <= self.expected_capabilities_for(node_id)
