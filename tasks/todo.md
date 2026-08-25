# Reaction labelling, a precedent index, and Pistachio as an evidence corpus

Plan: `/root/.claude/plans/investigate-deeply-following-user-playful-spindle.md`

## Phase 1 — the vocabulary and the two-phase index
- [x] `science/labels/vocabulary.py` — `SpeciesRole`, `LabelGroup`, `species_role_from`, `VOCABULARY_VERSION`
- [x] `science/labels/records.py` — `ReactionLabel`, `SpeciesLabel`
- [x] `science/labels/policy.py` — `LabelPolicy`
- [x] `science/labels/store.py` — `LabelIndex` Protocol, in-memory + Postgres backends
- [x] `infra/sql/050_reaction_labels.sql` + README rows + grants
- [x] `DataSourceManifest.labels`
- [x] `ingest_reaction` writes the record phase; `sync_entries` / `eln_sync` thread `source`
- [x] tests

## Phase 2 — the `rxnlabel` server and its client
- [ ] `Chemclaw3-mcp:servers/rxnlabel/` (separate PR)
- [x] `ingest/labels/labeller.py` (MCP client), `core/config/labels.py`, `core/mcp_session.py`

## Phase 3 — the enrichment background service
- [x] `ingest/labels/{enrich,merge}.py`, `durable/label_sync.py`, schedule, ADRs

## Phase 4 — Pistachio as an evidence corpus (warehouse `corpus:` binding) — DONE
## Phase 5 — the six questions as tools — DONE
## Phase 6 — pattern-fingerprint substructure screen (optional)

## Phase 2b — the `rxnlabel` MCP server (companion repo)
- [x] `servers/rxnlabel/` written, 1003 tests green, `lint`/`type`/`test` all pass
- [x] committed locally on `claude/rxnlabel-reaction-representation-and-naming`
- [ ] **BLOCKED: cannot push.** The git proxy refuses `8fqycwdt8v-oss/chemclaw3-mcp` — it is not in
      this session's authorized repository set, and `add_repo(access="push")` needs an approval this
      session cannot obtain. The branch is ready at
      `/home/user/8fqycwdt8v-oss/chemclaw3-mcp` and pushes as soon as the repo is attached.

## Review

**What shipped in Chemclaw3** (4 commits, branch pushed):

1. `science/labels` — the derived reaction-label index: the `SpeciesRole` vocabulary, the two-phase
   row, the `labels:` manifest policy, both index backends, the facet query, the five searches, the
   pattern-fingerprint substructure screen, and `CorpusCoverage`.
2. `ingest/labels` — the record-phase builder, the MCP client for the labelling server, the merge
   rule, the enrichment drain, the corpus drain and the precedent retriever.
3. `core/mcp_session.py` — the shared outbound MCP client, extracted because this was the second
   caller and the four hazards in it were separately measured.
4. `durable/{label_sync,corpus_sync}.py` + two Schedules, both conditional on a manifest declaring
   something rather than on a new `*_enabled` flag.
5. Five agent tools on the `rxnfp` bundle, the `reaction-search` skill rewritten, six eval probes.
6. `infra/sql/050`, `051`; three ADRs; `DEFERRED.md` and `BACKLOG.md` rows.

**Two real bugs the work found**, both in code paths that would have failed silently:

- `as_text` is `str()` for everything, so a NULL `NAMERXN_NAME` was being stored as the string
  `"None"` — and would then have been counted in frequency tables beside real named reactions.
- (in the server) a multi-component species — a salt, a metal complex — was matched against the
  reaction's dot-separated tokens as a whole string and matched neither, so every ferrocenyl
  phosphine and every alkali-metal salt came back `unknown`.

**Also fixed, pre-existing:** `make type` was red on this branch's base (three test-file errors).

**Gate:** `make lint type test` green with Postgres up, plus all seven validators, except
`tests/test_prompt_caching.py`'s two live-API cases, which 401 against this environment's
credential on the base commit too.
