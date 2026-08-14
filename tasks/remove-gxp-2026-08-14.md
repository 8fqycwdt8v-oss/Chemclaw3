# Removing the GxP aspects

Prompted by: "remove any gxp aspects from codebase — traceability logging etc is still important,
but it doesn't have to fulfil the demands and requirements of full GxP."

Decided in
[`D-2026-08-14-the-record-is-kept-because-it-is-useful-not-because-a-regulator-asks`](../docs/decisions/D-2026-08-14-the-record-is-kept-because-it-is-useful-not-because-a-regulator-asks.md).

**184 references across ~110 files, in two categories that needed opposite treatment.** The
vocabulary ("in a GxP system, X is worse than Y") was justifying ordinary engineering and gets
restated on its own terms. One real control — a tamper-evident hash chain over `audit_events` plus
signed anchors, a verifier, a Schedule, a CLI and an HMAC secret — was built for a regulated
deployment and is removed.

The scope was settled with the user before implementation: framing + tamper-evidence + the
retention/approval carve-outs, in plain traceability language. Two of the carve-outs were then
kept as mechanisms after measuring what they actually do (below).

## Plan

### 1 — Remove the tamper-evidence machinery
- [x] Delete `agent/audit_anchor.py`, `durable/audit_chain.py`, `durable/audit_verify.py`,
      `cli/verify_audit_chain.py` and their three test modules.
- [x] Strip the chain from `agent/audit_store.py`: `chain_hash`, the frozen field tuples, the
      advisory lock, the tip read, three columns from the INSERT. **Keep the file** — deleting it
      downgrades every Postgres deployment to `NullAuditSink`, the exact incident
      `default_audit_sink()` exists to prevent.
- [x] Salvage the concurrency coverage into `tests/test_audit_store.py` as a plain
      "24 concurrent sinks lose no rows" test, counted by `correlation_id` so it needs no TRUNCATE.
- [x] Unwire: schedules, `background_worker`, `cli/live_jobs`, five settings, `.env.example`,
      the `audit-verify` Make target, the chart's two values, the pytest selection list.
- [x] **Keep `"audit-verify"` in `OWNED_SCHEDULE_IDS`, marked retired** — that frozenset is the
      prune namespace, and dropping the name strands a live Schedule nothing can delete.
- [x] Rewrite `durable/retention.py`'s refusal to prune `audit_events` on records grounds; keep
      `tests/test_retention.py`'s guard, renamed.

### 2 — Rewrite the vocabulary
- [x] `src/` (125 hits), package READMEs, `infra/sql/` headers, root docs, `docs/guides/`,
      `docs/reference/` (German), `deploy/`, `tests/` (68 hits), `skills/`, `data/profiles/`.
- [x] The six operator-visible strings, which change behaviour and not just prose: the PR body,
      the metric help text, the erase-actor report header, a live-probe description, the agent
      `_INSTRUCTIONS`, the Prometheus alert summary.
- [x] Delete runbook §(xiii)'s anchor/restore procedure and §(xvi)'s `make audit-verify` paragraph.
- [x] Rename the `AgentProfile(name="gxp")` fixture.

### 3 — Eval probes
- [x] kn-23 rewritten around what remains — the audit row, the INSERT-only grant, the calculation
      store — **and the limit stated**: no cryptographic proof a row was never edited.
- [x] kn-27 and kn-29 rewritten; kn-29's and rp-13's `forbids_claims` **kept deliberately**, since
      forbidding a claim of GxP/21 CFR compliance is more correct after this change, not less.

### 4 — Record
- [x] ADR + ledger row; deleted the three BACKLOG rows and the DEFERRED row this closes.
- [x] Left `docs/decisions/`, `docs/archive/`, the dated review and closed `[x]` rows as history.

## Review

**Two things in the agreed scope were kept as mechanisms, and the reason is measurement, not
caution.**

- **Bi-temporal note validity.** `valid_from`/`valid_to`/`is_current` filter five read paths across
  eleven modules so a superseded note is not served as current evidence. That is retrieval
  correctness *described* in GxP terms; removing it would have been a regression. Confirmed with
  the user before proceeding.
- **The erasure retention tier.** Kept, per the user's choice. Its `audit_events` reason had to be
  rewritten regardless, because it cited the hash chain.

**The removal was cheaper than expected and the schema did not move.** Every chain column already
had a default (`prev_hash`/`row_hash` → `''`, `chain_version` → `1`), so the writer just stops
supplying them: **zero migrations**, which matters because
`tests/test_migrations_are_additive.py` refuses destructive statements with no exemption. Migrations
011 and 032 keep their statements and get `RETIRED` headers.

**What the system lost, stated plainly:** it can no longer detect that an audit row was modified.
What it never lost is the thing that *prevents* it — `grants/app_privileges.sql` gives the
application `INSERT` and neither `UPDATE` nor `DELETE`. The chain was the weaker half of that pair.

**Two incidental wins.** The audit write path loses a global `pg_advisory_xact_lock` taken on every
tool call, and `audit_events` becomes prunable in principle (still refused, now for the right
reason).

## Verification

- `make lint` · `make type` · `make test` — green.
- `make prose-validate`, `kg-validate`, `skill-validate`, `connector-validate`,
  `datasource-validate`, `template-validate`, `eln-validate`, `safety-validate` — all pass.
- `make helm-validate` **could not run here** — `helm` and `kubeconform` are not installed in this
  environment. The chart's values are still covered by `tests/test_helm_chart.py`, and CI's separate
  `chart` job runs the real render.
- The Postgres-backed audit tests skip offline; CI provides a real database.
- `grep -rniE "gxp|21 ?cfr|part 11|alcoa|gamp"` over the tree returns only the two deliberate
  `forbids_claims` guardrails, `docs/decisions/`, `docs/archive/`, the dated 2026-08-13 review and
  closed `[x]` BACKLOG rows.
