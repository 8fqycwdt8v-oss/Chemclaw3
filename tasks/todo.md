# Task: extensibility audit — what does it actually cost to add a thing?

Branch: `claude/codebase-extensibility-review-va7jmq`.
Deliverable: **a report only** — no code changes, no backlog edits. Scope: **this repo**.
Method: **measured**, not read. Prose about a seam is evidence about its author's belief.

## The question, made falsifiable

For every part of the system that changes on a regular cadence — a new tool, a new agent,
a new data source, a new skill, a new note type, a new eval, a config value, a user/role, a
routine operation — the claim under test is *"adding one is a bounded, local, low-effort act."*

That claim is only meaningful if it has a number. So each surface gets three:

1. **Footprint** — files changed, and how many of them are *outside* the new unit's own
   directory, taken from a real past addition in `git log`. A seam that works has a footprint
   that is almost entirely inside the new folder.
2. **Core-edit count** — how many of those files are in `core/`, `agent/`, `api/`, `durable/`,
   or a shared registry. This is the number D-120 claims is zero for data sources; every
   surface gets the same test.
3. **Leak count** — occurrences of a *concrete instance name* (`xtb`, `bofire`, `eln-json`,
   `sharedrive`, a profile name, a note type) in code that should only know the abstraction.
   Every leak is a place the next addition must be edited into.

Where a surface has no past addition to measure, the substitute is a **dry-run**: enumerate
the exact edit list from the registry/validator code and count it. Stated as such, not as
a measurement.

## Plan

### A. Inventory the change surfaces
- [ ] Enumerate every "add one of these" axis the system has, from the tree, the Makefile
      validators, the config, and the Helm chart — not from the docs' list of them.
- [ ] For each: name the discovery mechanism (registry? manifest? directory scan? hardcoded
      list?), the declaration file, the validator that guards it, and the doc that explains it.

### B. Measure the footprint of each past addition
- [ ] `git log` archaeology: find the commit(s) that added the most recent connector, data
      source, ingest document share, skill, note type, step template, eval metric, API route,
      Temporal queue/workflow, component role, and profile.
- [ ] Per commit, compute files-changed / inside-unit / outside-unit / core-edits, scripted so
      the numbers are reproducible rather than eyeballed.
- [ ] Where several additions of the same kind exist, check the trend: is the footprint
      shrinking (the seam is being paid off) or flat (it is not)?

### C. The leak sweep
- [ ] For every concrete instance name in the system, grep the whole of `src/` and report every
      hit outside its own bundle. Classify: legitimate (test fixture, default value, docs) vs
      leak (a branch, an enum, a literal list, an `if name ==`).
- [ ] Look specifically for the shapes that make a seam fake: string-literal registries,
      `Literal[...]` unions of instance names, `match`/`if` chains on a kind, per-instance
      config fields, per-instance Helm values, per-instance entrypoint cases.

### D. Configuration
- [ ] 358 `CHEMCLAW_*` settings and a 56 KB `.env.example`: measure the real shape — how many
      settings, how many are required vs defaulted, how many an operator must set to stand the
      system up, how many are per-instance (i.e. grow with the number of connectors/sources).
- [ ] Check the three-way consistency: config model ↔ `.env.example` ↔ Helm `values.yaml`.
      Is it enforced by a test, or by hand? What happens when someone adds a setting and forgets
      one of the three?
- [ ] Judge whether the config is *navigable*: can an operator find the setting they need, and
      is the growth rate per new connector/source bounded?

### E. Identity, users and roles
- [ ] Trace the whole path: Entra group → role → entitlement → gate. Where is the mapping
      declared, and is adding a role/entitlement/group a config act or a code act?
- [ ] Adding a user, revoking one, granting a new capability to an existing role, onboarding a
      new team, an emergency lockout — for each, the concrete steps and where they are written down.
- [ ] The DB side: `core/grants.py`, `make db-grants` — what a new deployment or a new table costs.

### F. Routine operations (day two)
- [ ] Read `docs/guides/runbook.md` against the Makefile and the code: is every routine operation
      an operator would need (migrate, reindex, re-embed, sync a share, rotate a secret, apply
      schedules, roll a release, verify the audit chain, cost a share, explain a session) actually
      a single documented command, and does it exist?
- [ ] Find the operations that have code but no runbook entry, and the runbook entries whose
      command no longer exists. Both are real failure modes on a live system.
- [ ] Workflow versioning / migrations: what does changing a running durable workflow cost.

### G. The enforcement layer itself
- [ ] The validators (`connector-validate`, `datasource-validate`, `skill-validate`,
      `template-validate`, `prose-validate`, `helm-validate`, …) are the mechanism that makes a
      declaration safe to add. Test each one's *discrimination*: break a declaration deliberately
      in a scratch copy and confirm the validator fails. A validator that passes everything is
      the most expensive kind of false comfort, because additions are then unguarded while
      appearing guarded.
- [ ] Same for the structural tests (`test_repo_map`, `test_layering`, `test_packaging`,
      `test_helm_chart`, `test_decision_log`, `test_deferred_register`).

### H. Synthesis
- [ ] One table: surface → discovery → declaration → validator → doc → measured footprint →
      verdict (trivial / bounded / expensive / undefined).
- [ ] Ranked gaps, each with the evidence that found it and the smallest fix that would close it.
- [ ] Explicitly name what I could **not** measure and why, so the report's limits are on its face.

## Verify

- [ ] Every number in the report is produced by a command recorded in the report, re-runnable.
- [ ] Every "this is easy" claim is backed by a footprint, not by an ADR asserting it.
- [ ] Every validator claimed to guard something has been shown to *fail* on a deliberate break.
- [ ] No repo state left behind: scratch copies under the scratchpad, `git status` clean.

## Review

Report: `tasks/extensibility-audit.md`. All eight phases run; every number in it comes from a
command that was executed, and the two headline findings were reproduced rather than inferred.

**What the measurement changed about the answer.** The static read of this codebase says the seams
are excellent, and the measurement agrees — a connector and a data source declared *outside the
repository* both reach the live surface with zero repo edits, and seven connector names produce six
hits across 64,200 lines of `src/`, all of them comments. That is a genuinely rare result and it is
now evidence rather than a claim.

The measurement also found what reading could not. Three docstrings state that the data-source
registry passes a source's name to its retrieve half; `registry.py:147` passes only
`**manifest.config`. Reading the ADR, the migration comment and the retriever docstring together
produces a confident, wrong picture of a working two-share deployment. Running it produces:
`share_sources()` collapsing two shares to one key, the second share's binding indexed under the
first's name, and a sweep that deletes the first share's documents. `infra/sql/037`'s comment
describes that exact failure as the thing its composite key prevents — the key is right, the value
fed into it is not.

**A validator that passes everything is the expensive kind of false comfort**, so each was broken
deliberately. Four of five breaks failed correctly. The fifth — two enabled shares reporting one
name — reports `data source validation passed.` That is the invariant the whole `(source, path)`
partition rests on, and nothing checks it.

**No repo state left behind**: every experiment ran from `PATH`-style overrides pointing at the
scratchpad. `git status` shows only these two documents.

---

## Part two: fixing all five

- [x] **Finding 1** — `_build_retrieve_half` passes `name=manifest.name` to every retrieve half.
      Passed rather than stamped after construction, because stamping accepts a half that refuses
      to be named, which is exactly how the defect survived. Passed to *every* half rather than
      only the parameterised ones, because a conditional pass is a rule the next half can fall
      outside of. Required (no default) on `ShareDocumentRetriever` and `WarehouseVectorRetriever`,
      whose old defaults were right for the first instance and wrong for every other.
- [x] **Finding 2** — runbook §(xv): the two ways a tenant expresses membership, the five gates that
      read a role, onboard/entitle/revoke, and offboarding via `chemclaw.agent.leaver` +
      `make user-erase`. The conversation is erasable; the GxP record is retained, counted and
      explained. No kill switch, recorded as a decision rather than left as a silence.
- [x] **Finding 3** — `note_types:`/`relations:` in `connector.yaml`; `known_note_types()` unions
      core's closed set with the *enabled* bundles'. `job-result` and `bo-candidate` moved out of
      core into the `qm` and `bo` manifests. Lazy `kg -> connectors` edge declared in the layering
      policy beside the `core.logging` precedent it copies.
- [x] **Finding 4** — runbook §(xvi) for `audit-verify`, `share-sync`, `safety-validate`, and why
      `schedules-apply` is deliberately not an operator step.
- [x] **Finding 5** — the escape was `job_tools()` under `except ConnectorError`, a sibling of
      `ConnectorJobError`, not the `_job_problems` arm the report named. Widened to the shared base
      and de-duplicated against what was already reported.
- [x] Three ADRs, ledger rows in sorted position.

## Verify (part two)

- [x] **The original repro re-run**: two shares now index under two names, and the sweep of one
      removes nothing from the other.
- [x] **Counterfactual measured**: reverting the one-line pass makes all three new seam tests fail;
      restoring it makes them pass. They discriminate.
- [x] **Postgres stood up locally** (with pgvector 0.8 built from source, since 0.6 cannot create
      one migration's `bit_jaccard_ops` index) so the eight leaver tests *run* instead of skipping.
      Five of them had been skipping silently — an offboarding command proven by nothing.
- [x] **`tests/test_database_privileges.py` caught a real gap in this change**: the new DELETE on
      `session_owners` had no matching grant. Fixed in `app_privileges.sql`, on its own line because
      insert-and-delete-but-not-update matches no other group there.
- [x] `make lint type test` green — 3602 passed, 36 skipped (xtb/crest binaries only). All eight
      validators green.

## Review

The audit's value was concentrated in the two places where running the code disagreed with reading
it. Three docstrings, an ADR and a migration comment all described a working two-share deployment;
the registry never passed the name, so the second share overwrote and then swept the first. And the
fix's own verification repeated the lesson one level down: five leaver tests reported success by
skipping, which is what "no database reachable" looks like from a green suite. Standing Postgres up
turned three of them red — and then the repo's existing privileges test found a missing grant that
no amount of reading the new module would have surfaced.

The one thing I would flag for a reviewer: `kg -> connectors` is the second declared lazy edge in
the layering policy. Two is still legible. A third would be the signal that the vocabulary wants to
live somewhere neither package owns.
