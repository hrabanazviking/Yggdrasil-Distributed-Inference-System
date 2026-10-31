# The Nine-Worlds Role Model — Y-012

*Roles are hats, not hardware. The founding spec (README section
2.1, "Nine Worlds Mapping") names nine worlds and the function each
serves; this slice makes that mapping data the mesh can reason
over, instead of prose only the operator can read.*

## The catalog

`roles/role_catalog.json` holds one definition per world, shaped by
`contracts/schemas/role_definition.schema.json`. Each definition
states:

- **function** — the spec's own words for what the world does
  (tested verbatim against the README mapping, so the catalog can
  never quietly drift from the founding document);
- **purpose** — what wearing the hat means in this mesh;
- **expected_capabilities** — what the mesh may expect of a wearer,
  in one vocabulary: the task contract's capabilities (`generate`,
  `embed`, `vision`, `stream`) plus the mesh functions
  (`orchestrate`, `interface`, `audit`, `map`, `store`, `fallback`);
- **criticality_ceiling** — the highest task criticality (Y-007:
  `casual` < `standard` < `critical`) a wearer may be handed under
  that hat. Ceilings bite: a forge worker (`svartalfheim`,
  `standard`) may not be handed critical work under that hat, while
  the auditor (`nidavellir`) and the emergency hat (`muspelheim`)
  are defined at `critical` because their whole job is standing
  guard over, or rescuing, the mesh's most important work;
- **binds_hardware** — `false`, always, enforced by the schema as a
  constant. No role names a device class; a Pi and a gaming laptop
  may wear the same hat, and the census (Y-006), not the hat, says
  what the hardware can actually do.

## Assignments

A node wears hats only by an operator's act, recorded as a
`role_assignment` (`contracts/schemas/role_assignment.schema.json`):
node, hats, who assigned them, and when. `RoleBook`
(`src/yggdrasil/roles.py`) keeps the current assignments and the
full history: a new assignment supersedes the node's old hats
wholesale, removal is its own recorded act, and nothing is edited
silently. A node's effective ceiling is the highest ceiling among
its hats; a node wearing no hat may be handed nothing. `eligible()`
is the single predicate — ceiling covers the criticality, hats
cover the capabilities — that the Part IV router will use at
placement time.

Assignments project directly into the Y-006 node identity: the
`roles` list a `NodeIdentity` carries is exactly a book record's
hats (tested against `node_identity.schema.json`).

## Honest limits

- Y-012 defines and validates the model; **no running mesh
  enforces it yet**. Carrying assignments across the fabric is
  Part III work; the router that refuses out-of-ceiling placements
  is Part IV (Y-075 onward), with the Muspelheim shedding rule at
  Y-091. Until those land, ceilings are a contract, not a guard.
- The catalog is this mesh's local reading of the spec's mapping.
  WorldNet's planetary expansion of the nine worlds (WorldNet
  section 2) is Part X research territory and changes nothing here.
- Changing the catalog itself — a ceiling, an expectation — is a
  data edit under version control, reviewed like code; changing
  who wears what is an operator act at runtime. The two must never
  be confused.
