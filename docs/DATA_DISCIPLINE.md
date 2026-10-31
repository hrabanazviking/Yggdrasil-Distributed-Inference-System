# Data Discipline — Y-016

*Everything the mesh treats as standing knowledge — registries,
catalogs, profiles, policies — is a versioned data file with a
schema and a validator, never a constant buried in code. Before
this slice the rule held by habit (Y-010's threat register and
Y-012's role catalog were already built that way). Y-016 makes the
habit enforceable and names what "versioned data" means here.*

## The manifest

`data/data_manifest.json` is the register of registers. Every
versioned data file in the repo has one entry
(`contracts/schemas/data_file_entry.schema.json`) stating:

- **id / kind** — what the file is (`manifest`, `catalog`,
  `register`, `registry`, `profile`, `policy`);
- **path** — repo-relative, never escaping the tree;
- **version_field / version** — the field inside the file that
  carries its version, and the exact version the manifest expects
  to find there;
- **roadmap_slice** — the slice that owns the file, which the file
  itself also declares; the two must agree;
- **schema / items_field / item_schema** — the whole-file schema
  and/or the per-item schema its collection entries must validate
  against, from `contracts/schemas/`.

The manifest is itself a registered, versioned, schema-shaped data
file (`yggdrasil-data-manifest-v1`, shaped by
`data_manifest.schema.json`) and validates like everything else —
it registers itself first.

At Y-016 the manifest covers exactly three files: itself, the
nine-worlds role catalog (Y-012), and the threat register (Y-010).
No profile or policy data exists yet; the kinds are named in the
entry schema now so the first one has a home to register in.

## The validator

`src/yggdrasil/data_discipline.py` provides `validate_repo()`,
which checks, in order:

1. **Schemas** — every file in `contracts/schemas/` is itself a
   valid JSON Schema and carries a version in its `$id`
   (`yggdrasil/contracts/v<N>/...`).
2. **Registration** — every `*.json` under a data directory
   (`roles/`, `security/`, `data/`, and the future `profiles/` and
   `policies/`) is registered in the manifest. An unregistered
   data file fails validation: a new registry, profile or policy
   lands only by adding its manifest entry in the same slice that
   creates it.
3. **Agreement** — each registered file exists, parses, and its
   on-disk version and owning slice match the manifest exactly.
   Version drift in either direction is a failure, not a detail.
4. **Shape** — whole-file and per-item schema validation, with the
   item count and a SHA-256 fingerprint reported per file.

## Version rules

Versions are `<stem>-v<N>` (`yggdrasil-role-catalog-v1`). A change
that alters a file's meaning is a new version, never a silent
in-place edit of a version other slices cite: `require_version_bump`
refuses anything that is not the same stem at a strictly higher N,
and a bump is landed by editing the file and its manifest entry
together, in one commit, so the two can never be seen apart.

## Honest limits

- Y-016 validates paperwork at build/test time. **No running mesh
  reads these files yet** — the node agent (Part II) is the first
  runtime reader, and until then the manifest is a discipline the
  test suite enforces, not a live registry service. The fabric's
  replicated registry (Y-053) and CRDT stores (Y-056) are separate,
  later machinery; this slice governs the repo's own standing data.
- Validation proves shape, versioning and registration — never
  that a file's *contents* are wise. A perfectly valid catalog can
  still hold a bad ceiling; that judgement stays with the operator
  and the slice that owns the file.
