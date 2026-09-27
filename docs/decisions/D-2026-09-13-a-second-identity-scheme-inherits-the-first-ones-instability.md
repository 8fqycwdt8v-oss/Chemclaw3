# D-2026-09-13-a-second-identity-scheme-inherits-the-first-ones-instability — no InChI, no InChIKey, and the argument that would have added one is measurably false

`051_reaction_labels.sql` states the omission as a decision: "no InChIKey, formula or molecular
weight either — nothing asks, and this tree deletes dead columns". A queue row proposed reopening
it, for a reason that sounds right and is not: **a cross-system join needs an identifier that
survives a `STANDARDIZATION_VERSION` bump, which a `standard_smiles` string by construction does
not.** The first half is a real need. The second half is not an argument for an InChIKey, and the
measurement below is why.

## What was measured

Structure identity today is `core.chem.compound_id` — `compound-<12 hex of the standardized
SMILES>` — and nothing else exists: the whole family declares no `inchi`, `inchikey`, `cas`,
`formula`, `molecular_weight` or registry column in `infra/sql/` or `schema/`. The only place an
external notation is read at all is `ingest/eln/ord_adapter.py`, which converts an ORD submission's
`INCHI` identifier to a structure at the boundary and stores the SMILES. That is the rule working.

Then the stability claim, run against the shipped pipeline (`std7`, RDKit's own `MolToInchiKey`):

| input | standardized | `compound_id` | InChIKey of the standardized form |
|---|---|---|---|
| `CCN.C1CCOC1` | `C1CCOC1.CCN` | `compound-3b5ebb43b6d5` | `XBIMXRVVZSNNGI-UHFFFAOYSA-N` |
| `C1CCOC1` | `C1CCOC1` | `compound-c59bfa659357` | `WYURNTSHIVDZCO-UHFFFAOYSA-N` |
| `CCN` | `CCN` | `compound-bb572bdd9031` | `QUSNBJAOOMFDIB-UHFFFAOYSA-N` |
| `CCN.Cl` | `CCN` | `compound-bb572bdd9031` | `QUSNBJAOOMFDIB-UHFFFAOYSA-N` |
| `CCN.[Cl-]` | `CCN` | `compound-bb572bdd9031` | `QUSNBJAOOMFDIB-UHFFFAOYSA-N` |

Row 1 is a real re-standardization: before
`D-2026-08-27-a-solvate-is-not-its-solvent`, `standard_smiles("CCN.C1CCOC1")` returned **THF** — row
2's structure, and row 2's InChIKey. So the change that moved `compound_id` moved the InChIKey with
it, by exactly the same amount and for exactly the same reason. An identifier derived from the
standardized structure inherits every instability of the standardization.

And taking it from the *raw* input instead is worse, which rows 3–5 show: the free base, the
hydrochloride and the chloride salt are one substance and one `compound_id`, and an InChIKey minted
before standardization gives them three different keys — fragmenting the join `standard_smiles`
exists to make.

## The decision

**Structure identity stays the standardized SMILES and nothing else**, in both databases.

An InChIKey is a *notation*, and a notation is a function of a structure. What a re-standardization
changes is **which structures are the same substance** — a solvate, a counterion, a stereocentre —
so no notation of the structure can be stable across it. The identifier that would be stable across
it is one **a site assigns and this system does not derive**: a corporate compound number. That is
not a schema decision here at all, because it already has a home — `Component.attributes` is a
free-form bag the warehouse binding fills from any column a site names
(`attributes: ["LOT_NUMBER"]` is the shipped example), so a site's own registry number rides into
the record today with zero core edits, under D-120's promise.

What is genuinely open is therefore narrower than the row said, and it is **not** an identity
question: whether a *published result* should carry a site-supplied identifier alongside
`compound_id`, so another system can join against it. That needs a reader, and the reader would be
a result sink — of which there is none, because `CHEMCLAW_RESULT_SINKS` names nothing in any
shipped deployment. It goes to `DEFERRED.md` with that trigger rather than being guessed at now: an
InChIKey nothing queries is precisely the dead column `051` refuses.

The queue row's own ordering constraint — "any identifier minted before [the solvate fix] inherits
the collapse" — is satisfied: that fix shipped in `D-2026-08-27-a-solvate-is-not-its-solvent`, and
the row it pointed at is closed and gone.

## What keeps it true

- `tests/test_schema_inventory.py::test_no_schema_mints_a_second_structure_identity` — a column-name
  check over comment-stripped SQL across `infra/sql/` and `schema/`. The stripping is load-bearing:
  the three notations it forbids are named in `051`'s own prose, so a text scan would pass on a
  migration adding the column directly beneath that sentence.
- `tests/test_schema_inventory.py::test_the_schemas_declare_columns_at_all` — the positive control,
  because a scan that parses nothing passes the guard for ever.
