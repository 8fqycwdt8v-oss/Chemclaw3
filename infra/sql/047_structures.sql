-- Content-addressed 3D geometries, so a computed structure can be named instead of pasted
-- (D-2026-08-21-a-geometry-is-an-address-not-a-payload).
--
-- **What was wrong.** `structure_id` is a hash of a geometry's chemical content, derived
-- byte-identically on both sides of the wire, and until now it was *write-only* from the agent's
-- side: four result models reported one and no tool or job spec accepted one. So a conformer
-- search handed a chemist twenty geometries and the only way to use one in the next calculation
-- was to hand back the SMILES, which throws the search away. This table is the missing half — the
-- address resolves to the geometry it names.
--
-- **Keyed by `structure_id`, not by the SHA-256 of its bytes**, which is the one thing that makes
-- it different from `artifact_blobs` (019) and the reason it is not that table. A geometry's
-- identity deliberately excludes its `smiles` and its `origin` — two identical geometries are the
-- same structure whether one was embedded and the other optimized — so the address is *narrower*
-- than the bytes, and a byte-address would fork on provenance the identity ignores. That is
-- exactly the same argument `api/tool_results.py` makes for not sharing the artifact store: one of
-- the two keys would be pretending to be the other.
--
-- **Never pruned, and that is a decision rather than an omission.** A row is a few kilobytes (a
-- 40-atom drug molecule is ~1.7 kB of JSON) and is deduplicated by construction, so twenty
-- thousand of them is tens of megabytes. The geometries are *already* held, untruncated, inside
-- the `calculation_results` payloads that D-011 refuses to prune — so pruning here would reclaim
-- nothing that is not still on disk one table over, while breaking every handle a chemist wrote
-- down. `durable/retention.py`'s `_PRUNABLE` therefore does not name it, deliberately.
--
-- Applied by `make db-migrate` (idempotent).
CREATE TABLE IF NOT EXISTS structures (
    structure_id TEXT        PRIMARY KEY,   -- 'st_' + stable_hash(elements, positions, charge, multiplicity)
    -- The whole `Structure`, as `model_dump(mode="json")` writes it: the coordinates that were
    -- hashed, plus the `smiles` and `origin` the identity excludes but a reader wants.
    structure    JSONB       NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Which molecule a stored geometry is of, for "show me the conformers we have for this compound".
-- A plain expression index rather than a column: the value is inside the payload already, and a
-- second copy is a second thing that can disagree with it.
CREATE INDEX IF NOT EXISTS structures_smiles_idx
    ON structures ((structure ->> 'smiles'));
