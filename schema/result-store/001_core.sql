-- The canonical result store: every computed value ChemClaw3 produces, as a queryable record.
--
-- **This schema is shipped by ChemClaw3 and created by the site.** Nothing in this repository ever
-- connects to it with DDL privileges; `make sink-schema` prints these statements for a DBA to run.
-- That split is deliberate and mirrors the one this repository already makes for its own database
-- (`postgres_migration_dsn` vs `postgres_dsn`): the credential that writes rows every day is not
-- the credential that can drop a table.
--
-- **Why this exists at all, given `calculation_results` already stores every result.** That table
-- is a cache: `key TEXT PRIMARY KEY` onto an opaque `result JSONB`, and its own query model refuses
-- any predicate on the payload — "a `total_energy_hartree > x` predicate would put one calculator's
-- schema inside the thing that persists all of them". Right for a store whose job is exact-key
-- lookup, and exactly why it cannot also be the scientific record. This is the other half: the same
-- science, shaped for `WHERE`.
--
-- **Portability.** Written to load on PostgreSQL, Snowflake and Oracle with only the type-name
-- substitutions the dialect module performs. Three Postgres habits are given up to get that:
--
--   * **no arrays** - lineage is an edge table (`calculation_input`), warnings are flag rows.
--     Neither Snowflake nor Oracle has `TEXT[]`, and an array column cannot be indexed in the
--     reverse direction that staleness propagation walks.
--   * **no sequences** - every primary key is a content hash. That also makes the publish
--     idempotent and the whole database re-buildable from `calculation_payload`.
--   * **no partial or expression indexes** - anything worth indexing is a real column. An
--     expression index over a JSON field is the pattern that would not port.
--
-- Applied by `python -m chemclaw.cli.sink_schema --apply`, tracked by checksum in
-- `results_schema_migrations`. Forward-only and additive: a new column arrives nullable with a
-- default, so the previous image keeps writing unchanged.

-- ============================================================================================
-- Registry: the extension point. A new calculator INSERTs here; the DDL does not move.
-- ============================================================================================

-- Every quantity that may be stored, with the unit it is kept in.
--
-- **This table is what keeps the fact layer from being EAV.** `property_value.property` is a
-- foreign key into it, so a value cannot be written under a name nobody defined - which is the
-- failure that makes an attribute-value store return confident partial answers: `pka`, `pka_acid`
-- and `pKa` all present, every query matching one of them.
CREATE TABLE IF NOT EXISTS property_definition (
    property        VARCHAR(128) PRIMARY KEY,
    -- What kind of quantity it is. Properties sharing a dimension must be convertible to one
    -- another's canonical unit; that is a check, not a comment.
    dimension       VARCHAR(64)  NOT NULL,
    -- The unit `property_value.value_canonical` is expressed in. Empty for a dimensionless
    -- quantity. Canonical *per property*, not one global unit: absolute energies stay in hartree
    -- because they exist to be differenced, while every difference is kcal/mol, because that is
    -- the unit every threshold a chemist states is in.
    canonical_unit  VARCHAR(32)  NOT NULL DEFAULT '',
    -- Which of `property_value`'s three value columns is correct for this name. A projection that
    -- writes `converged` as the number 1.0 is a registry violation rather than a plausible number.
    value_kind      VARCHAR(16)  NOT NULL DEFAULT 'number',
    -- **Which table this quantity's values live in**, so a per-atom value written as a
    -- calculation-scope scalar is a detectable error rather than a stored one. 'calculation' names
    -- `property_value` and covers *both* of that table's row scopes -- a reaction's delta-G is a
    -- fact about the run and a species' absolute Gibbs energy is a fact about one member, and
    -- `property_value.scope_kind` below is what tells those apart. The other values name the tables
    -- that are not `property_value`: 'site', 'point', 'conformer', 'candidate'.
    scope_kind      VARCHAR(16)  NOT NULL DEFAULT 'calculation',
    definition      VARCHAR(2000) NOT NULL,
    -- A property that turned out to be wrong is deprecated, never deleted: rows already carry it,
    -- and removing the definition would orphan them.
    deprecated_at   TIMESTAMP WITH TIME ZONE,
    superseded_by   VARCHAR(128)
);

-- Canonical solvent identity, and every spelling that resolves to it.
--
-- **This table exists because of a measured defect.** The calculation layer accepts `thf` AND
-- `tetrahydrofuran`; `hexane`, `n-hexane`, `nhexane`, `n-hexan` AND `nhexan`; `ch2cl2`,
-- `dichloromethane`, `dichlormethane` AND `methylenechloride` - 42 names for 25 solvents - and the
-- name reaches the calculation key verbatim. Storing the name as given would make
-- "every reaction we ran in THF" return a confident subset, with nothing raising anywhere.
CREATE TABLE IF NOT EXISTS solvent (
    solvent_id   VARCHAR(64) PRIMARY KEY,
    display_name VARCHAR(128) NOT NULL,
    -- Deliberately no dielectric constant. It would be useful to query on, and this system has not
    -- measured one; inventing numbers to fill a column is worse than leaving it out. A deployment
    -- holding real values can add the column and populate it.
    smiles       VARCHAR(512) NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS solvent_alias (
    alias      VARCHAR(64) PRIMARY KEY,
    solvent_id VARCHAR(64) NOT NULL REFERENCES solvent (solvent_id)
);

-- How a calculation was run, as distinct from what state it was run at. Separate from
-- `condition_set` because "at GFN2" and "in THF" are independently varied in every screen.
CREATE TABLE IF NOT EXISTS theory_level (
    level_id   VARCHAR(64)  PRIMARY KEY,   -- content hash of the tuple below
    method     VARCHAR(128) NOT NULL,      -- 'GFN2-xTB' | 'B3LYP' | 'esol-delaney@2004'
    family     VARCHAR(32)  NOT NULL DEFAULT '',  -- semiempirical | dft | ff | ml | empirical
    basis_set  VARCHAR(64)  NOT NULL DEFAULT '',
    engine     VARCHAR(64)  NOT NULL DEFAULT '',  -- tblite | xtb | crest | rdkit
    treatment  VARCHAR(64)  NOT NULL DEFAULT ''   -- effort tier, or how conformers were handled
);

-- The state a calculation was run at.
--
-- **A continuum solvent lives here rather than in the subject**, because an implicit model is a
-- parameter of the Hamiltonian and not a species present in the flask. An *explicit* solvent
-- molecule is a subject member instead, and the two stay distinguishable.
--
-- `solvent_id IS NULL` means gas phase, which is a real state. There is a row whose every column
-- is null, reached by every calculator that has no conditions at all (descriptors, fingerprints),
-- so that "ran in the gas phase" and "this calculator has no conditions" are one queryable value
-- rather than a LEFT JOIN in every consumer.
CREATE TABLE IF NOT EXISTS condition_set (
    condition_id  VARCHAR(64) PRIMARY KEY,
    solvent_id    VARCHAR(64) REFERENCES solvent (solvent_id),
    solvent_model VARCHAR(32) NOT NULL DEFAULT '',    -- 'alpb' | 'cpcm' | ''
    temperature_k DOUBLE PRECISION,
    pressure_pa   DOUBLE PRECISION,
    ph            DOUBLE PRECISION,
    charge        INTEGER,
    multiplicity  INTEGER
);

-- ============================================================================================
-- Chemical identity and the subject
-- ============================================================================================

-- One molecule. Keyed by `compound_id` - `compound-<hash>` over the standardized SMILES, which is
-- the id this system already uses to join the knowledge graph, the fingerprint search and the QM
-- notes. Reused rather than minting an InChIKey here, so a published result and the note about the
-- same compound name it identically with no second scheme.
CREATE TABLE IF NOT EXISTS compound (
    compound_id      VARCHAR(64)  PRIMARY KEY,
    canonical_smiles VARCHAR(4000) NOT NULL,
    first_seen_at    TIMESTAMP WITH TIME ZONE NOT NULL
);

-- One 3-D geometry, content-addressed. `structure_id` is derived byte-identically on both sides of
-- the calculation wire, so an address written here resolves in the calculation store too.
--
-- Coordinates stay in the JSON column: no chemistry question is "where is atom 7", while
-- `atom_count` and `formula` are asked and so are real columns.
CREATE TABLE IF NOT EXISTS structure (
    structure_id    VARCHAR(64) PRIMARY KEY,
    compound_id     VARCHAR(64) REFERENCES compound (compound_id),
    atom_count      INTEGER     NOT NULL DEFAULT 0,
    -- **Nullable, because 0 and 1 are answers.** A geometry reaches the writer either whole (its
    -- charge and multiplicity stated) or as a bare content address (they are not), and a NOT NULL
    -- default made the second case indistinguishable from a neutral closed-shell singlet -- so
    -- "every anionic geometry we have optimised" answered over a column that said 0 for every ion
    -- this system had ever published. NULL is "not recorded"; the writer preserves a stated value
    -- against an unstated one rather than letting the later write win.
    charge          INTEGER,
    multiplicity    INTEGER,
    -- The calculation that produced this geometry, when it is an output rather than an embedding.
    origin_calc_ref VARCHAR(512) NOT NULL DEFAULT '',
    geometry        JSONB       NOT NULL,
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX IF NOT EXISTS structure_compound_idx ON structure (compound_id);

-- What a calculation was about.
--
-- **`subject_id` deliberately excludes solvent, temperature and method.** That exclusion is what
-- makes "compare this reaction's free energy across every solvent we ran it in" a GROUP BY on one
-- column, rather than a fuzzy join over two text arrays that happen to be spelled the same way.
-- It is order-insensitive and stoichiometry-sensitive: `A+B->C` and `B+A->C` are one subject,
-- `2A->B` and `A->B` are two.
CREATE TABLE IF NOT EXISTS subject (
    subject_id   VARCHAR(64)  PRIMARY KEY,
    kind         VARCHAR(16)  NOT NULL,   -- molecule|geometry|ensemble|reaction|complex|system
    member_count INTEGER      NOT NULL,
    -- A reaction SMILES, or the compound's SMILES: so a row is legible without a join, which is
    -- the same argument the measurements table makes for keeping its subject string.
    label        VARCHAR(4000) NOT NULL DEFAULT ''
);

-- The participants. **One shape for all five subject cases**, rather than five special cases: a
-- molecule is one member; a reaction is N with reactant/product roles, one per stoichiometric
-- equivalent; a complex is three (monomer, monomer, complex); an ensemble is one naming the seed
-- geometry the search started from, because the conformers it found are outputs and live in
-- `conformer`.
CREATE TABLE IF NOT EXISTS subject_member (
    subject_id    VARCHAR(64) NOT NULL REFERENCES subject (subject_id),
    ordinal       INTEGER     NOT NULL,
    role          VARCHAR(16) NOT NULL,   -- subject|reactant|product|monomer|complex|solvent|catalyst
    compound_id   VARCHAR(64) REFERENCES compound (compound_id),
    structure_id  VARCHAR(64) REFERENCES structure (structure_id),
    smiles        VARCHAR(4000) NOT NULL DEFAULT '',
    stoichiometry DOUBLE PRECISION NOT NULL DEFAULT 1,
    charge        INTEGER,
    multiplicity  INTEGER,
    PRIMARY KEY (subject_id, ordinal)
);
-- "Everything we hold for this compound", which is half the questions asked of this database.
CREATE INDEX IF NOT EXISTS subject_member_compound_idx ON subject_member (compound_id, role);
CREATE INDEX IF NOT EXISTS subject_member_structure_idx ON subject_member (structure_id);

-- ============================================================================================
-- The spine
-- ============================================================================================

-- One row per distinct calculation. **The science, and only the science.**
--
-- `calc_ref` is the flat cache key - `calc_type@calc_version:input_hash:params_hash` - the same
-- string a knowledge note cites, so a published row and a note about it name the calculation
-- identically.
--
-- **The actor is deliberately absent.** A calculation's identity excludes who asked for it: the
-- result does not depend on the requester, so identical science shares one key across users. Two
-- chemists running the same calculation produce one row here and two in
-- `calculation_publication`. Putting the actor on this table would make them collide on the
-- primary key.
CREATE TABLE IF NOT EXISTS calculation (
    calc_ref         VARCHAR(512) PRIMARY KEY,
    calc_type        VARCHAR(128) NOT NULL,
    calc_version     VARCHAR(256) NOT NULL DEFAULT '',
    input_hash       VARCHAR(64)  NOT NULL DEFAULT '',
    params_hash      VARCHAR(64)  NOT NULL DEFAULT '',
    subject_id       VARCHAR(64)  NOT NULL REFERENCES subject (subject_id),
    condition_id     VARCHAR(64)  NOT NULL REFERENCES condition_set (condition_id),
    level_id         VARCHAR(64)  NOT NULL REFERENCES theory_level (level_id),
    -- The geometry the calculation ran ON, never the one it produced. That is the useful meaning:
    -- a chemist holding a conformer's address asks what has already been computed at it.
    -- Empty for a molecule-keyed calculator, which reads as "not recorded" rather than "none".
    structure_id     VARCHAR(64)  NOT NULL DEFAULT '',
    provenance       VARCHAR(16)  NOT NULL DEFAULT 'computed',  -- computed|measured|imported
    status           VARCHAR(16)  NOT NULL DEFAULT 'valid',     -- valid|superseded|invalidated
    compute_seconds  DOUBLE PRECISION,
    computed_at      TIMESTAMP WITH TIME ZONE,
    -- Which ChemClaw3 wrote the row, and which contract version it was built against. Without
    -- these, "why is in_domain null for everything before March" is unanswerable, and a consumer
    -- cannot tell an absent measurement from an absent column.
    --
    -- **contract_version 1 published a wrong `max_gradient`, and 2 is the correction.** The
    -- projector reported the value as `hartree/bohr` while it is Hartree/Angstrom, so the number
    -- passed through the unit conversion untouched: every `max_gradient` fact written at version 1
    -- is 1.890x too large, under a unit string that looks right and therefore lands silently
    -- inside or outside any range filter. No other field is affected.
    --
    -- Two ways to separate them, and neither needs this table changed: filter on
    -- `contract_version >= 2`, or read `property_value.reported_unit`, which records what the
    -- calculator actually said — `hartree/bohr` on a `max_gradient` row is the old population and
    -- `hartree/angstrom` the corrected one. Re-publishing a corrected document works from version
    -- 2 onward; at version 1 the writer's own outbox treated it as a duplicate and dropped it.
    writer_version   VARCHAR(64)  NOT NULL DEFAULT '',
    contract_version INTEGER      NOT NULL DEFAULT 0,
    ingested_at      TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX IF NOT EXISTS calculation_subject_idx ON calculation (subject_id, calc_type);
CREATE INDEX IF NOT EXISTS calculation_structure_idx ON calculation (structure_id);
CREATE INDEX IF NOT EXISTS calculation_type_idx ON calculation (calc_type, computed_at);

-- Who ran a calculation, under which tenant, and why. N rows per calculation - see above.
-- A site's grants and row-level security attach here rather than to `calculation`.
CREATE TABLE IF NOT EXISTS calculation_publication (
    calc_ref       VARCHAR(512) NOT NULL REFERENCES calculation (calc_ref),
    tenant_id      VARCHAR(128) NOT NULL,
    session_id     VARCHAR(128) NOT NULL DEFAULT '',
    job_id         VARCHAR(256) NOT NULL DEFAULT '',
    actor          VARCHAR(256) NOT NULL DEFAULT '',
    correlation_id VARCHAR(128) NOT NULL DEFAULT '',
    -- What question the run was meant to answer. The field the rest of the system had nowhere to
    -- put: a note records what a run produced and an audit row records that a tool was called, and
    -- neither says why - which is what a chemist needs months later to judge whether it still
    -- applies.
    rationale      VARCHAR(4000) NOT NULL DEFAULT '',
    published_at   TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (calc_ref, tenant_id, session_id, job_id)
);
CREATE INDEX IF NOT EXISTS calculation_publication_tenant_idx
    ON calculation_publication (tenant_id, published_at);
CREATE INDEX IF NOT EXISTS calculation_publication_actor_idx ON calculation_publication (actor);

-- What a calculation rested on. An edge table rather than an array column: staleness propagation
-- walks it in the reverse direction, and no array type indexes that way.
CREATE TABLE IF NOT EXISTS calculation_input (
    calc_ref            VARCHAR(512) NOT NULL REFERENCES calculation (calc_ref),
    depends_on_calc_ref VARCHAR(512) NOT NULL,
    role                VARCHAR(32)  NOT NULL DEFAULT '',
    PRIMARY KEY (calc_ref, depends_on_calc_ref, role)
);
-- The reverse direction: "everything that rests on this calculation", which is the question asked
-- when a calculation is found to be wrong.
CREATE INDEX IF NOT EXISTS calculation_input_reverse_idx
    ON calculation_input (depends_on_calc_ref);

-- The payload exactly as it was stored, never a predicate.
--
-- **This is what makes the projection safe to be wrong.** Every fact in the tables below is
-- derived; this is the source. A projector bug is then a re-projection rather than lost science,
-- and a future contract version can re-derive facts nobody thought to extract.
CREATE TABLE IF NOT EXISTS calculation_payload (
    calc_ref     VARCHAR(512) PRIMARY KEY REFERENCES calculation (calc_ref),
    payload_kind VARCHAR(128) NOT NULL DEFAULT '',
    payload      JSONB        NOT NULL
);

-- ============================================================================================
-- The fact layer. Four narrow tables, split by scope rather than by tool.
--
-- **Why four and not one: cardinality, not taste.** A 33-atom molecule produces one
-- calculation-scope energy, 99 vibrational modes, 33 atom charges and ~35 bond orders. In a single
-- table the per-atom and per-mode rows outnumber the ones anyone filters on by ~150x, so the index
-- that answers "pKa between 4 and 6" would be built over a table that is 99.3% wavenumbers.
-- ============================================================================================

-- Calculation- and member-scope scalars. **The hot table**: every top-level chemistry question
-- filters here.
CREATE TABLE IF NOT EXISTS property_value (
    -- A content hash of (calc_ref, scope, ordinal, property), not a sequence. Portable, and it
    -- makes re-publishing the same record a no-op rather than a duplicate.
    value_id         VARCHAR(64)  PRIMARY KEY,
    calc_ref         VARCHAR(512) NOT NULL REFERENCES calculation (calc_ref),
    property         VARCHAR(128) NOT NULL REFERENCES property_definition (property),
    scope_kind       VARCHAR(16)  NOT NULL DEFAULT 'calculation',
    -- Null at calculation scope. This is what carries a reaction's per-species breakdown: the
    -- reaction's delta-G is a fact about the whole run, while each species' absolute Gibbs energy
    -- is a fact about one member. One table answers both.
    member_ordinal   INTEGER,
    -- THE predicate column, always in the registry's canonical unit for this property. Every
    -- range filter reads this and nothing else.
    value_canonical  DOUBLE PRECISION,
    -- The other two value kinds. Exactly one of the three is filled; which one is correct for a
    -- given property is `property_definition.value_kind`.
    value_bool       BOOLEAN,
    value_text       VARCHAR(2000),
    -- What the calculator actually said, before canonicalization. Kept for audit and for the day a
    -- conversion is found wrong - at which point the canonical column can be rebuilt from this.
    reported_value   DOUBLE PRECISION,
    reported_unit    VARCHAR(32)  NOT NULL DEFAULT '',
    -- **On the same row as the value, deliberately.** Split across two rows, reading a value
    -- without its error bar becomes a self-join that silently succeeds when the second row is
    -- missing - and a semiempirical number quoted without its uncertainty is the failure every
    -- result model in this system warns about.
    uncertainty      DOUBLE PRECISION,
    uncertainty_kind VARCHAR(32)  NOT NULL DEFAULT '',  -- reported|propagated|none
    -- Whether the molecule was inside the model's applicability domain. NULL means no domain was
    -- declared, which is NOT the same as false and must never be read as yes: an out-of-domain
    -- prediction's error bar is not merely larger, it is meaningless.
    in_domain        BOOLEAN,
    -- Denormalized from the spine so the headline query is a single-table scan. A deliberate
    -- warehouse trade; `tests/test_publish_sql.py` reconciles them against their dimension rows.
    subject_id       VARCHAR(64)  NOT NULL,
    calc_type        VARCHAR(128) NOT NULL,
    method           VARCHAR(128) NOT NULL DEFAULT '',
    solvent_id       VARCHAR(64),
    temperature_k    DOUBLE PRECISION,
    computed_at      TIMESTAMP WITH TIME ZONE
);
-- The index the headline question plans against: "every reaction with delta-G below -10 kcal/mol
-- run in THF at GFN2".
CREATE INDEX IF NOT EXISTS property_value_query_idx
    ON property_value (property, method, solvent_id, value_canonical);
-- "Everything we know about this reaction or molecule."
CREATE INDEX IF NOT EXISTS property_value_subject_idx ON property_value (subject_id, property);
CREATE INDEX IF NOT EXISTS property_value_calc_idx ON property_value (calc_ref);

-- Per-atom and per-atom-pair values: Mulliken charges, Fukui indices, Wiberg bond orders.
-- `atom_j = -1` means a single-site value; a non-negative `atom_j` makes it a pair.
CREATE TABLE IF NOT EXISTS calculation_site_value (
    calc_ref VARCHAR(512) NOT NULL REFERENCES calculation (calc_ref),
    atom_i   INTEGER      NOT NULL,
    atom_j   INTEGER      NOT NULL DEFAULT -1,
    property VARCHAR(128) NOT NULL REFERENCES property_definition (property),
    element  VARCHAR(8)   NOT NULL DEFAULT '',
    value    DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (calc_ref, atom_i, atom_j, property)
);

-- Ordered series: scan profiles, vibrational modes, IR spectra. Ordered is why this is not EAV -
-- "the third mode" has to be an integer comparison and not a string sort.
--
-- A spectrum, a chromatogram and a titration curve are all this shape, which is worth checking
-- before any future result type earns a table of its own.
CREATE TABLE IF NOT EXISTS calculation_point_value (
    calc_ref     VARCHAR(512) NOT NULL REFERENCES calculation (calc_ref),
    series       VARCHAR(32)  NOT NULL,     -- 'scan' | 'modes' | 'spectrum'
    ordinal      INTEGER      NOT NULL,
    property     VARCHAR(128) NOT NULL REFERENCES property_definition (property),
    value        DOUBLE PRECISION NOT NULL,
    -- The abscissa the point sits at: a dihedral in degrees, a bond in Angstrom, a wavenumber.
    -- What makes a series plottable without knowing which tool produced it.
    x_value      DOUBLE PRECISION,
    x_unit       VARCHAR(32)  NOT NULL DEFAULT '',
    x_label      VARCHAR(128) NOT NULL DEFAULT '',
    structure_id VARCHAR(64),
    PRIMARY KEY (calc_ref, series, ordinal, property)
);

-- Conformer ensembles. Its own table rather than a point series, because a conformer is a geometry
-- with a degeneracy and not a value at an abscissa.
--
-- **`population` is meaningless without the temperature that produced it**, which is on the
-- calculation's `condition_set`. The search that found these members is temperature-independent
-- and is cached that way; the populations are arithmetic over it at a stated T, published as their
-- own calculation edged back to the search. So recomputing at 353 K is a new row, not an overwrite.
--
-- Both energy columns are nullable because the two upstream shapes carry different halves and
-- neither carries both: the cached search has an absolute energy and no population, while the
-- weighted ensemble has a relative energy and a population and no absolute.
CREATE TABLE IF NOT EXISTS conformer (
    calc_ref       VARCHAR(512) NOT NULL REFERENCES calculation (calc_ref),
    ordinal        INTEGER      NOT NULL,   -- 0 = lowest
    structure_id   VARCHAR(64)  NOT NULL,
    energy_hartree DOUBLE PRECISION,
    relative_kcal  DOUBLE PRECISION,
    population     DOUBLE PRECISION,
    degeneracy     INTEGER      NOT NULL DEFAULT 1,
    PRIMARY KEY (calc_ref, ordinal)
);
CREATE INDEX IF NOT EXISTS conformer_structure_idx ON conformer (structure_id);
-- "Ensembles with more than N conformers above a population threshold" scans this.
CREATE INDEX IF NOT EXISTS conformer_population_idx ON conformer (calc_ref, population);

-- Ranked outputs: predicted products, suggested conditions, similarity hits, optimizer candidates.
-- One table for four tools, because they are the same question - "what does this suggest, and how
-- strongly" - asked of different chemistry.
CREATE TABLE IF NOT EXISTS calculation_candidate (
    calc_ref       VARCHAR(512) NOT NULL REFERENCES calculation (calc_ref),
    ordinal        INTEGER      NOT NULL,
    candidate_kind VARCHAR(32)  NOT NULL,   -- compound|reaction|condition|point
    compound_id    VARCHAR(64) REFERENCES compound (compound_id),
    smiles         VARCHAR(4000) NOT NULL DEFAULT '',
    score          DOUBLE PRECISION,
    score_property VARCHAR(128) REFERENCES property_definition (property),
    detail         JSONB        NOT NULL,
    PRIMARY KEY (calc_ref, ordinal)
);

-- The 0..N assertions that are not measurements: warnings, hazard alerts, imaginary-frequency
-- notices.
--
-- **The line against `property_value.value_bool` is cardinality, not type.** A fixed 0..1 attribute
-- of every result of its kind (`converged`, `is_minimum`, `veber_pass`) is a property, because
-- every such calculation has exactly one. An open-ended emitted set is a flag, because a run may
-- raise none or six and nobody can enumerate them in advance.
CREATE TABLE IF NOT EXISTS calculation_flag (
    calc_ref VARCHAR(512) NOT NULL REFERENCES calculation (calc_ref),
    ordinal  INTEGER      NOT NULL,
    flag     VARCHAR(64)  NOT NULL,
    severity VARCHAR(16)  NOT NULL DEFAULT 'info',
    message  VARCHAR(2000) NOT NULL DEFAULT '',
    detail   JSONB        NOT NULL,
    PRIMARY KEY (calc_ref, ordinal)
);
CREATE INDEX IF NOT EXISTS calculation_flag_idx ON calculation_flag (flag, severity);

-- ============================================================================================
-- Schema metadata
-- ============================================================================================

-- Which of these files have been applied, tracked by checksum so an edited migration is caught
-- rather than silently skipped. The same discipline the application's own migration runner uses.
CREATE TABLE IF NOT EXISTS results_schema_migrations (
    filename   VARCHAR(256) PRIMARY KEY,
    checksum   VARCHAR(64)  NOT NULL,
    applied_at TIMESTAMP WITH TIME ZONE NOT NULL
);
