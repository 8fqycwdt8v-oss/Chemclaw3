# D-021 — Production-readiness review: one bad-data contract, hardened PR-gate

The whole-repo review (post-5b) fixed systemic issues rather than adding features. (a) All
bad-input errors (`FingerprintError`, `ElnMappingError`, `IngestError`, `MetricError`,
`PlaybookError`, `NoteError`) now derive from one `chemclaw.errors.ChemclawError(ValueError)`:
reject-and-continue boundaries catch the base instead of enumerating types — forgetting one had
turned a single degenerate ELN entry into a batch-aborting poison pill. It stays a `ValueError`
so Temporal's fail-fast-on-bad-data retry policy keeps applying; the shared policy and the
note-publish discipline live once in `workflows/publish.py`. (b) The git PR-gate submitter is
hardened: submissions serialize through a lock (checkout -B switches the whole tree), the
checkout is `note_repo_dir` config (a dedicated clone in production), note ids/types are
slug-constrained at the model (ELN-derived ids reach file paths and git refs), and the note
branch is fetched before `--force-with-lease` so re-proposals from fresh clones push. (c) ELN
mass balance is downgraded to element-set subsumption: without stoichiometric coefficients a
per-molecule count comparison falsely rejects dimerizations, so the sound necessary condition is
"no product element absent from the inputs". (d) Store factories (`default_molecule_store`/
`default_reaction_store`) pair table name and bit width once, and the pKa cache key now embeds
the tblite version like xTB's (an engine upgrade is a cache miss, not a stale hit, D-011).
