# 12 — Deep analysis: where the codebase needs more effort

**Repo:** `/home/user/Chemclaw3` · **Branch:** `claude/config-extensibility-plan-0ittcz` · **Date:** 2026-07-25
**Scope:** seven dimensions the 2026-07-22 forensic audit (`00-SUMMARY.md`) under-covered or left as
unsigned proposals — performance, test *effectiveness*, complexity, doc↔code drift, configurability
to *run*, missing features, live-edge risk. **Method:** read-only measurement first; every finding
carries a file:line, a measured number, or a reproducible command. Findings that need a human
decision are marked and left unexecuted, per the prior audit's convention.

**Baseline at analysis time:** ~14.8k source lines / 12.3k test lines across 16 first-party packages;
`make lint type test` green — 624 passed, 41 skipped (offline Postgres/Temporal only), coverage floor 80%.

---

## Executive summary

1. **One High-severity defect, and it was in the front door of the developer experience:** the
   documented quickstart `cp .env.example .env` (README.md:12) made **every entry point crash at
   import**. `Settings` sets `extra="forbid"` and `.env.example` documented two keys that are not
   fields. Nobody had hit it because the defaults work without a `.env` — so the file that exists
   *only* to be copied was the one thing never tested. **Fixed**, with the parity now machine-enforced.
2. **The codebase is in genuinely good health otherwise.** Mutation testing on the five most
   safety-critical invariants — authorization, the GxP PR-gate, the note slug guard, the calculation
   cache, budget enforcement — found **all five caught** by the suite. The prior audit's fixes are
   real: DB statement timeouts are now uniform, `service_allow_insecure` is enforced and tested,
   9 cross-field config validators cover the coupled-config hazards.
3. **The real performance story is the interactive graph path**, and it was half-optimized. The
   KM-14 cache spared the note *parse* but every query still reassembled the whole NetworkX graph —
   and the agent's own documented flow (`find_notes` → `expand_note`) pays it **twice per turn**.
   Measured 162 ms of pure waste per warm query at 10k notes. **Fixed** (162 ms → 83 ms).
4. **One unbounded model-context surface remained** after the D-066 clamp pass: `find_notes`, the
   agent's primary entry into the graph, returned *every* match while every sibling retrieval
   surface was capped. **Fixed**, with the established truncation-warning convention.
5. **What looks like the worst complexity in the repo is a measurement artifact.** `create_app`
   scores cyclomatic 33 — but that is the FastAPI closure-factory idiom summing its nested route
   handlers, and the closure is exactly what makes the HTTP surface testable without a live model.
   **No action; refactoring it would trade a real property for a better number.**
6. **The residual performance ceiling is now the filesystem, not the code.** A warm query is
   ~75 ms of `stat()` at 10k notes on local disk — and the target deployment reads a networked
   OpenShift PVC, where that is far worse. This needs a **staleness-tolerance decision** (below).

---

## Findings

| ID | Track | Sev | Finding | Evidence | Status |
|---|---|---|---|---|---|
| **DA-1** | D/E · config | **High** | `cp .env.example .env` — the README quickstart — makes `Settings()` raise at import, killing every entry point (CLI, service, workers, all `make` targets). `.env.example` documented `CHEMCLAW_REPORT_EXCERPT_CHARS` (superseded by `note_excerpt_chars`) and `CHEMCLAW_ENTRA_CLIENT_ID` (never a field); `model_config` sets `extra="forbid"`. | Reproduced: `ValidationError: chemclaw_report_excerpt_chars — Extra inputs are not permitted`. README.md:12 prescribes the copy. | **Fixed** `a96932d` |
| **DA-2** | D · docs | Med | 19 real `Settings` fields were absent from `.env.example` — the entire `budget_*` block (6), `service_allow_insecure`, `service_turn_timeout_seconds`, the D-066 clamps, the ELN cursor-slack knobs. `docs/runbook.md:4` and `implementation-plan.md:82` both promise "every field mirrored in `.env.example`". Operators cannot tune an invisible knob. | Set-diff of `Settings.model_fields` against the file. | **Fixed** `a96932d` |
| **DA-3** | A · perf | Med | `build_graph` reassembled every node and edge on each call; the KM-14 cache covered only the parse. `find_notes`/`expand_note` each call it, and the `find_notes`-then-`expand_note` flow is what the tool docstring tells the model to do — so a normal turn paid it twice. | Measured warm build at 10k notes: **161.9 ms**, of which 75.8 ms stat scan + ~86 ms reassembly. | **Fixed** `a96932d` — 162 ms → **82.7 ms** |
| **DA-4** | A · context | Med | `find_notes` returned *all* matches, uncapped, straight into the model's context — the only unbounded retrieval surface left. Siblings are all bounded (`fingerprint_max_top_k`, `retrieval_top_k`, `substructure_scan_max_records`, `graph_max_hops`). A one-letter needle returned the corpus. | `agents/graph_tools.py` `find_notes` had no truncation before the fix. | **Fixed** `a96932d` — `graph_max_results` (50) + truncation warning |
| **DA-5** | A · perf | Med | **Needs a decision.** With DA-3 fixed, the warm-query floor is the `stat()` fingerprint scan: ~75 ms per query at 10k notes *on local disk*. Production reads a networked OpenShift PVC, where per-file `stat` latency is far higher — this is the scaling wall for interactive Q&A. | `_dir_fingerprint` at N=10k: 75.4 ms (benchmark below). | **Open** — see decision D-1 |
| **DA-6** | B · tests | — | **Positive.** Five mutations of the most safety-critical invariants (authz role check, PR-gate GxP guard, note slug traversal guard, calc-cache reuse, budget enforcement) were **all caught** by the suite. Coverage here is real, not nominal. | `agents/authz.py`, `kg/pr_gate.py:68`, `kg/note.py` `_SLUG`, `calc/store.py:131`, `service/budget.py` — each mutated, suite went red. | No action |
| **DA-7** | B · tests | Low | Test-to-module locality is weak: 3 of the 5 mutations survived the test file that *obviously* owns them and died only under the full suite. A narrow `pytest tests/test_x.py` during development gives false confidence. Not a correctness gap (CI runs everything) — a feedback-loop one. | e.g. the `kg/note.py` slug guard is proven by neither `test_knowledge.py` nor `test_graph.py`. | **Open** — [S] |
| **DA-8** | C · complexity | Info | `create_app` scores cyclomatic **33** (`service/app.py:135`), by far the repo's highest — but it is the FastAPI closure-factory idiom, and mccabe sums the nested route handlers. The closure is load-bearing: injecting `agent_factory`/`owner_store` is what lets tests drive the whole HTTP surface without a live model. Flattening it would push state into globals or a DI layer. | `--select C901`: only 5 functions exceed 8, four of them inside `create_app`. | **No action** (deliberate) |
| **DA-9** | E · config | — | **Positive.** 9 `model_validator`s already cover the coupled-config hazards (Temporal mTLS pairing, poll-below-heartbeat, HPC launch mode, LLM + embedding provider coherence, Entra enforcement completeness, relative `knowledge_dir`, distinct source names). This is a real preflight, not a wish. | `chemclaw/config.py` — 9 validators. | No action |
| **DA-10** | G · live edges | Med | The 41 skipped tests are **entirely** offline-infra (24 Postgres, 17 Temporal) and CI runs Postgres, so the skip surface is honest. The genuine untested surface is narrower than feared but real: Entra token validation, federation/OBO exchange, real Nextflow launch, Helm render. | `pytest -rs`; `BACKLOG.md` live-edge list. | **Open** — see decision D-2 |

### Fix quality note

DA-1 was fixed at the root, not the symptom. Correcting the two keys would have left the *class* of
defect alive — the file and the field list had no relationship anything checked. Three tests now
enforce it: no stale key, no undocumented field, and `.env.example` loads as a real `.env`. The
docs' "every field mirrored" promise is now machine-checked rather than aspirational.

DA-3's cached graph is **frozen** (`nx.freeze`) rather than copied. Copying a 10k-node graph would
return most of the saving; freezing makes the shared instance safe for the same reason `Note` is
frozen — a rationale already established in this codebase, so the fix extends an existing idea
rather than introducing a new one.

---

## Decisions needed (not self-resolved)

**D-1 — Graph freshness vs. interactive latency (DA-5).** The stat-scan floor is only removable by
accepting some staleness (a TTL on the fingerprint check: "re-scan at most every N seconds") or by
adding an invalidation signal (the PR-gate merge hook busting the cache; `inotify`). A TTL is ~10
lines and makes queries O(1); it also means a just-merged note may be invisible for up to N seconds.
**That is a GxP-adjacent product call — how fresh must a query be? — so it is not ours to make.**
Recommendation: TTL defaulting to a few seconds, config-gated, with the PR-gate merge path busting
the cache explicitly so the *authoring* loop stays instant.

**D-2 — How much live-edge risk to buy down offline (DA-10).** Contract tests against recorded
Entra/Nextflow responses and a `kubeconform` render in CI would de-risk the F4–F7 surface without a
tenant or cluster. This is real effort against code that is dormant by default. Recommendation:
`kubeconform`/`helm template` in CI first (cheapest, catches the most likely break); defer the
identity contract tests until a tenant exists.

---

## Track F — missing features: triage, not re-discovery

The two completeness analyses already enumerate 29 unsigned proposals (`08-agentic-engine-gaps.md`
AG-1…15, `09-knowledge-management-gaps.md` KM-1…14). Re-deriving them would be waste. Ranked against
the actual target — GxP pharma process R&D on OpenShift/HPC with one internal LLM — the load-bearing
few are:

1. **KM-13 retrieval evaluation** (already rated High there, and it is the right call). Everything
   else in the knowledge layer is unfalsifiable without it: DA-4's cap, KM-5's ranking, KM-4's query
   understanding are all changes to retrieval whose effect is currently unmeasurable. The corpus is
   still small — this is the cheapest it will ever be to build the gold set.
2. **AG-14 prompt/skill version provenance.** Directly a GxP reproducibility hit: an audit record
   cannot be tied to the prompt revision that produced it. Small, and the audit trail already exists.
3. **KM-7 freshness enforcement.** Partly live already (expiry is filtered at read — DA-4's tests
   confirm it), but fingerprint re-indexing on note mutation is still open; a superseded condition
   can be served as current fact through the *structural* path.

The rest are correctly ranked in the source documents. Two items merit **downgrading**: AG-12
(model routing/fallback) and KM-10 (near-duplicate detection) are solving problems a single-endpoint,
Git-curated, human-signed-off system does not have — adding them now is ceremony.

**Dimensions neither gap analysis covered**, and which a production GxP deployment will need:
data lifecycle (retention, backup/restore, and specifically **migration rollback** — `infra/sql`
migrations are forward-only), disaster recovery for the Git knowledge repo, and per-project
confidentiality scoping (KM-9 defers this, correctly, until two projects share one graph — but the
deferral should be revisited *before* the second project, not after).

---

## Reproducing the measurements

```bash
make lint type test                      # the gate: 631 passed, 41 skipped
uv run ruff check --select C901 --config "lint.mccabe.max-complexity=8" .   # DA-8
uv run pytest tests/test_config.py -k env_example                           # DA-1/DA-2 guards
```

Graph scaling (DA-3/DA-5) — synthetic tree, since `knowledge/` holds no notes yet:

| N notes | cold build | warm build (before) | warm build (after) | stat floor |
|---|---|---|---|---|
| 100 | 8.0 ms | 0.9 ms | 0.6 ms | 0.6 ms |
| 1 000 | 76.8 ms | 10.7 ms | 6.5 ms | 6.4 ms |
| 5 000 | 433.6 ms | 65.2 ms | 38.5 ms | 36.7 ms |
| 10 000 | 857.4 ms | 161.9 ms | **82.7 ms** | 75.4 ms |

Cold build is ~86 µs/note and unavoidable on first read; warm build is now the stat floor plus
noise. The mutation checks (DA-6/DA-7) were run by patching one line per invariant and asserting the
full suite goes red — the survivors under narrow test files were re-run against the whole suite
before being reported, and two "survivors" turned out to be mis-targeted patches (one had replaced a
docstring, not the guard). Only conclusions that survived that re-check are stated above.

---

## What this pass did not reach

Honest scope limits, so the next pass does not assume coverage it did not get:

- **Track A** covered the interactive graph path end to end; the DB read patterns, SSE fan-out
  behavior under concurrency, and Temporal activity granularity were **not** measured — they need a
  live Postgres/Temporal, which the offline sandbox does not have.
- **Track B** mutation-tested five invariants, not the whole suite. A systematic pass (e.g.
  `mutmut`/`cosmic-ray` over `agents/` and `kg/`) would be the real measurement; these five were
  chosen by blast radius.
- **Track C** ranked complexity mechanically and examined the top scorer. Coupling and layer-boundary
  analysis got only the import-graph sketch (`chemclaw` is the hub at 101 inbound imports, as the
  architecture intends) — no violation was found, but none was deeply hunted either.
