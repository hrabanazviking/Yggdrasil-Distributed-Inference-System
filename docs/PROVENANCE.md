# Licence & Provenance — Y-018

*The repo's licence read and honoured; third-party pieces noticed;
nothing vendored silently.*

## The project's own licence

`LICENSE` is the **MIT License**, copyright **2026 Volmarr Wyrd**.
Read plainly, it grants anyone use, copy, modification, merging,
publishing, distribution, sublicensing and sale — subject to one
condition: **the copyright notice and the permission notice must be
included in all copies or substantial portions of the Software**,
and the software is offered without warranty. `pyproject.toml`
declares the same licence (`license = { text = "MIT" }`), and the
register carries exactly one project-licence entry naming the same
holder — the three are checked for agreement by tests, so the
licence cannot drift between files unnoticed.

Every dependency below was chosen compatibly with offering this
project under MIT: the whole set is MIT, PSF-2.0 (permissive), or
the author's own work. There is no copyleft dependency anywhere in
the project, declared or transitive.

## The register

`provenance/provenance_register.json` (versioned
`yggdrasil-provenance-register-v1`, registered in the Y-016 data
manifest, each item shaped by
`contracts/schemas/provenance_record.schema.json`) notices 17
pieces:

- **The licence and the founding documents (P-01–P-04):**
  `LICENSE`; the v3.1 spec `README.md` — the founding document,
  never edited by this campaign (its md5 at campaign open is
  asserted unchanged by this slice's tests); the WorldNet addendum;
  and Volmarr's Cyber-Viking Master Visual Design Lawbook (`.md` +
  `.pdf`, the author's own document, rendered — not a third-party
  blob). All the author's own work.
- **Declared dependencies (P-05, P-06, P-11–P-13):** `jsonschema`
  and `referencing` at runtime, `setuptools` to build, `pytest`
  and `ruff` in development — all MIT. Version constraints in the
  register must match `pyproject.toml` exactly.
- **The runtime transitive closure (P-07–P-10):** `attrs`,
  `jsonschema-specifications`, `rpds-py` (all MIT) and
  `typing-extensions` (PSF-2.0, only on Python < 3.13) — what
  actually ships to a node alongside the two direct dependencies.
  (Development tools' own transitive dependencies are not
  registered individually: they never ship, and the register says
  so on the pytest entry.)
- **The language runtime (P-14):** Python ≥ 3.11 and its standard
  library, PSF-2.0 — the only other thing the code assumes.
- **Standards referenced, not copied (P-15, P-16):** RFC 8032
  (EdDSA), whose algorithm the identity code implements and whose
  published test vectors prove it, and JSON Schema Draft 2020-12,
  referenced by `$schema` URI in every contract schema. No
  specification text lives in this repo.
- **The one hand-written implementation (P-17):**
  `src/yggdrasil/ed25519.py`, written in-repo at Y-011 following
  RFC 8032 — deliberately written rather than vendored, with its
  honest limit (not constant-time) stated in its docstring.

Licences in the register were read at Y-018 from the installed
distributions' own metadata (`License-Expression`, licence files,
classifiers), not recalled from memory.

## The gap this slice found and fixed

`referencing` was imported directly by project modules since Y-009
but was **declared nowhere** — it merely arrived transitively as
`jsonschema`'s dependency. Code that imports a package must declare
it. Y-018 declares `referencing>=0.28.4` in `pyproject.toml`, and
the new checks refuse this class of gap from now on.

## The checks (`src/yggdrasil/provenance.py`)

`check_provenance()` proves, at build/test time:

1. every register item validates against its schema;
2. `LICENSE`, `pyproject.toml` and the register agree on the
   licence and its holder;
3. every declared dependency is noticed in the register with the
   same version constraint, under a dependency kind, unvendored;
4. every third-party module imported in `src/` or `tests/` is a
   declared dependency;
5. every path a register entry names exists;
6. **vendored is false for every entry — nothing third-party is
   copied into this tree** — and a future vendored entry would
   have to name an existing location, while a `vendor/`,
   `vendors/`, `third_party/` or `_vendor/` directory appearing in
   the tree fails the check outright.

The rule going forward is the Y-016 discipline applied to
provenance: **a new dependency, founding document, referenced
standard or vendored piece lands only with its register entry, in
the same slice that introduces it.**

Honesty: these checks prove the register agrees with the repo as
it stands. They are not a legal opinion, and no hardware is
involved in this slice at all.
