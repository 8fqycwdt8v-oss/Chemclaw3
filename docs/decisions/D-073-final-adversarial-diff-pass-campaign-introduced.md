# D-073 — Final adversarial diff pass: campaign-introduced defects caught and fixed

**Context.** After all fix waves landed green, one adversarial reviewer re-read the entire branch
diff (90 files) hunting only for defects the campaign itself introduced or left: regressions,
incomplete fixes, cross-agent seam inconsistencies, and dishonest tests.

**Decision (findings → fixes).** Nine confirmed: (1) **S2** — the new submitter reset/clean plus
the `note_repo_dir="."` default could destroy a developer's own working tree; `submit()` now
refuses the process's CWD/checkout root before any destructive git command. (2) deny-mode authz
inversion — the built-in write gate now only narrows `allow`, never widens `deny`. (3) ELN
overlap×chunk amplification — merged-note short-circuit, first-chunk-only overlap, DEBUG replay
logging (sync trail INFO line now also reports `skipped_existing=K`). (4) `CampaignSpec` read
live config inside a Temporal-crossing validator — ceiling moved to the creation entry point so
replays cannot fail on config drift. (5) drift-eval memo keyed on paths — corpus stat signature
added. (6) `except Exception` counter cleanup — BaseException-safe try/finally for turn and
stream slots. Plus docs: workflow-versioning policy recorded in BACKLOG (no live histories yet),
memory-id one-time migration note, stale `entra_client_id` references removed. SQL surface,
artifact-token scoping, flock semantics, budget LRU, charge-validation coverage, and test honesty
were probed and confirmed sound.

**Consequence.** The campaign's own changes went through the same skeptic gauntlet as the code it
reviewed; the one severe self-introduced risk (dev-tree destruction) was caught before merge.

**Result.** Final gate: lint + mypy strict clean; 625 passed / 17 Temporal-only skips; coverage
89.64% (baseline 88.43%). Branch `claude/code-review-refactor-plan-wm34wc`.
