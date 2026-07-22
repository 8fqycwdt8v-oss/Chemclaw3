# 00 — Forensic Audit Summary & Findings Report

**Repo:** `/home/user/Chemclaw3` · **Branch:** `claude/codebase-audit-hardening-69v233` · **Date:** 2026-07-22
**Status:** Phases 0–11 complete (discovery → report → sign-off → execution → handover), merged in
PR #9. All approved backlog items are implemented, tested, and committed; the gate was green (369
passed, 0 failed). See `REFACTOR_LOG.md` (finding → commit) and `09-handover.md` (what changed +
conventions). This file is the durable findings record (rows annotated where execution changed them).

> The eight interim per-phase working reports (`01`–`08`) were consolidated into this summary and
> removed once the audit completed and merged; their full per-finding detail remains in git history
> and in the commit trail referenced by `REFACTOR_LOG.md`.

---

## Phase 0 — What was discovered (sanity-check the setup)

- **Stack:** single Python 3.11 project, `uv`-managed. MAF (agent orchestration) + Temporal
  (durable jobs) + FastAPI/SSE front door + Postgres/pgvector + Git-backed Markdown knowledge
  graph + MCP capability servers. 196 `.py` files (~17k source + ~7k test lines), 16 first-party
  top-level packages.
- **Commands used** (discovered from `Makefile` + `.github/workflows/ci.yml`, both go through the
  same targets):
  - Lint: `uv run ruff check .` / `uv run ruff format --check .`
  - Types: `uv run mypy chemclaw agents bo calc eln evals kg mcp_servers memory report scripts workflows workers tests`
  - Tests: `uv run pytest`
  - Full gate: `make check` (= lint + type + test)
  - Also: `make db-migrate`, `make kg-validate`, `make eval`, `make eln-validate`, `make skill-validate`
- **CVE scan:** `pip-audit`. **Secrets scan:** `git log -p --all` grep over full history.
- If any command above is wrong for your workflow, correct it before Phase 10.

---

## Executive summary (plain language)

1. **This is a disciplined codebase, not a vibe-coded mess.** The automated gate is fully green:
   ruff clean (196 files), `mypy --strict` clean (186 files), **356 tests pass / 0 fail**, no known
   CVEs, and **no secrets anywhere in git history**. Every module has a purpose docstring.
2. **Provenance is legible, not chaotic.** It was built sequentially phase-by-phase (Jul 19–21) with
   an ADR log and repeated adversarial ("CHECKMATE") reviews. The "merged from many quick branches"
   framing mostly does *not* apply here — with two real exceptions below.
3. **No Critical or High security findings.** SQL is uniformly parameterized, git uses `exec` (no
   shell) with slug + path-containment guards, JWTs are RS256-pinned with audience/issuer/exp checks,
   deserialization is safe (`SafeLoader`, no pickle/eval). The security posture is strong.
4. **The real signal is design-level drift automation can't see.** The two genuine merge scars: an
   **agent-harness feature integrated twice**, and an **F7 "generic data-source seam" migration left
   half-done** — the ELN sync reads sources from config while the memory jobs still read a hardcoded
   registry, so the two subsystems can disagree on which reactions exist.
5. **One client-facing information leak (Medium):** turn errors interpolate the raw exception string
   into the browser SSE stream, directly contradicting the code's own "never a leaked trace" comment.
6. **A few durable-path resilience bugs in shipping-but-dormant F5 code:** the Nextflow launcher
   mis-classifies a transient bad JSON body as permanent (kills the durable QM job) and is
   non-idempotent under Temporal's at-least-once retry.
7. **The whole auth stack hinges on one insecure-by-default boolean** (`entra_required=False`). It's
   documented as dev-only, but nothing in the *code* warns when it runs unauthenticated on a
   non-loopback interface.
8. **A latent packaging defect:** the wheel declares only the `chemclaw` package, but the console
   entry point is `agents.cli:main` and 12 other first-party packages exist — a non-editable
   `pip install` would ship a broken command and no feature code. Works today only because every
   install path is editable.
9. **Two small hygiene items:** `httpx` is imported by 4 modules but undeclared in `pyproject.toml`
   (reproducibility hazard, corroborated by 3 independent agents); and the durable GxP audit sink
   (`agents/audit_store.py`) has no direct test.
10. **Bottom line:** low bug density, high consistency. The backlog is a focused set of ~20 items,
    heavy on "finish the half-done F7 migration" and "close the deployment footguns," light on
    rewrites. Several are one-line fixes.

---

## Findings table

Severity: **Crit / High / Med / Low / Info**. Effort: **S** (<1h), **M** (a few hours), **L** (day+).
"⚠ needs decision" = do not self-resolve; see Open Questions.

| ID | Area | Sev | Description | Recommendation | Eff |
|----|------|-----|-------------|----------------|-----|
| SEC-1 | Security / error-leak | Med | `service/runner.py:83` streams raw `{exc}` into the client `ErrorEvent`, contradicting its own "never a leaked trace" comment. Leaks DB host, SMILES, workflow ids. | Emit a generic message + correlation id; log detail server-side. | S |
| SEC-2 | Security / authz default | Med | Entire auth stack is a no-op when `entra_required=False` (the default). No code-level warning when unauthenticated + bound to non-loopback (`service_host` defaults `0.0.0.0`). | Loud startup warning (or fail-closed) when `entra_required` false and host non-loopback. ⚠ needs decision on fail-closed vs warn. | S–M |
| SEC-3 | Security / audit | Low–Med | `agents/audit.py` swallows durable audit-sink failures (WARN-only). GxP trail can silently drop records. | Add a metric/alert on sink failure. ⚠ needs decision (design). | S |
| SEC-4 | Security / input | Low | No `max_length` on `MessageIn.message`; `expand_note(hops)` unbounded; MCP `top_k`/`threshold` unclamped. | Add `Field(max_length=…)` + clamp `hops`. | S |
| SEC-5 | Security / headers | Low | No HSTS/CSP/X-Frame-Options/X-Content-Type-Options; app serves an HTML chat UI. | Add a security-headers middleware (CSP at minimum). | S |
| SEC-6 | Security / logs | Low | Upstream `response.text` echoed into exceptions in `nextflow.py`, `identity/workload.py`, `identity/obo.py`. Server-side only. | Truncate/omit body; keep status + reason. | S |
| SEC-7 | Security / logs | Low | `service/auth.py` returns JWT validation failure reason in the 401 `detail`. | Generic 401 in production. | S |
| ~~COR-1~~ | Correctness / resilience | ~~Med~~ **FALSE POSITIVE** | Claimed `nextflow.py` `response.json()` `JSONDecodeError` is non-retryable and kills the durable QM job. **Verified wrong during execution:** Temporal matches `non_retryable_error_types` by exact class-name string (`retry_logic.rs`), and the failure type is `exception.__class__.__name__` = `"JSONDecodeError"` ≠ `"ValueError"`, so it is already **retryable**. No change. | Dropped (A4). | — |
| COR-2 | Correctness / idempotency | Med | `nextflow.launch_run` POSTs with no idempotency key; Temporal at-least-once retry double-submits an expensive HPC run. (Dormant: `hpc_launch_interface` defaults `mock`.) | Send an idempotency key derived from the QM cache key. ⚠ may defer to live-edge. | M |
| COR-3 | Correctness / resource | Med | `service/app.py` `sessions`/`session_owners` dicts never evicted → unbounded per-pod memory growth. | Bound with an LRU/TTL, or move lookup to the session store. | M |
| COR-4 | Correctness / concurrency | Med | `agents/session_events.py` push-back mailbox does `SELECT unconsumed` then `UPDATE consumed` without `FOR UPDATE SKIP LOCKED`; concurrent tailers double-deliver. (Mitigated: single-tailer + session-to-pod pinning.) | Add `FOR UPDATE SKIP LOCKED` (or claim-then-read). | M |
| COR-5 / CON-2 | Correctness+Consistency / DB | Low | `mcp_servers/fpstore.py:187` is the only store calling `db.connect(dsn)` without `statement_timeout_seconds` — the HNSW similarity scans (most likely to run long) have no per-statement bound. | Pass `settings.pg_statement_timeout_seconds`. | S |
| DUP-1 | Duplication / behavior | Med | Two live registries: `sources/registry.py` (config-driven, feeds ELN sync) and `eln/registry.py` (hardcoded json+ord, feeds memory jobs). Memory synthesis ignores `data_sources`; the two corpora can disagree, and `CHEMCLAW_DATA_SOURCES` silently has no effect on memory. F7 migration half-done. | Consolidate onto `sources/registry`; repoint `memory_jobs`. ⚠ needs decision on intended memory-source semantics. | M |
| CON-1 | Consistency / identity | Med | `config.py` claims `service_actor_id` replaced the magic `"unknown"` literal, but `"unknown"` is still the live default in `workflows/models.py:35`, `agents/chemclaw_agent.py:89`, `agents/audit.py:166`. Half-finished F4 migration; unattributed events tagged inconsistently. | Standardize on `settings.service_actor_id`. | S |
| CON-3 | Consistency / DRY | Low–Med | Identical ISO-timestamp parse duplicated in `eln/json_adapter.py:266` and `eln/ord_adapter.py:332` (the ORD docstring even points at the other). | Extract one shared helper. | S |
| CON-4 | Consistency / naming | Low–Med | Two `_list()` helpers, same name, opposite contracts: `json_adapter.py:212` raises on missing; `ord_adapter.py:371` returns `[]`. | Rename to intent (`_require_list`/`_optional_list`). | S |
| CON-5 | Consistency / cosmetic | Low | `agents/identity/hpc_bridge.py:15` hardcodes the logger name instead of `getLogger(__name__)`; two DB cursor idioms coexist. | Normalize logger; pick one cursor idiom. | S |
| ARC-1 | Architecture / packaging | Med (latent) | Wheel declares `packages=["chemclaw"]` but entry point is `agents.cli:main` and 12 other first-party packages exist. Non-editable `pip install` ships a broken `chemclaw` command + no feature code. No ADR covers the flat multi-package layout. | Declare all packages (or ADR the layout + fix wheel config). ⚠ changes the build/distribution contract. | S |
| INV-1 | Deps / reproducibility | Med | `httpx` imported by `agents/identity/obo.py`, `identity/workload.py`, `llm_provider.py`, `workflows/hpc/nextflow.py` but **not declared** in `pyproject.toml` (relied on transitively). Corroborated by 3 agents. | Add `httpx` to `dependencies`. | S |
| DUP-2 | Dead/stale config | Low | `settings.eln_sync_adapter` is now only a cursor *label* (no longer selects an adapter); its docstring is wrong. | Fix docstring or remove; fold into the DUP-1 consolidation. | S |
| DUP-3 | Dead code | Low | `eln/registry.make_eln_adapter()` has no production caller (tests only). | Remove with DUP-1, or keep as the registry's public API. | S |
| INV-3 | Test coverage | Low | `agents/audit_store.py` (durable GxP audit sink) has no direct test; `evals/` has thin coverage for a quality-gate module. | Add a Postgres-backed audit-sink test (CI has Postgres). | M |
| INV-2 | Migrations / cosmetic | Info | `infra/sql/` skips `005` (004→006). **Confirmed never existed** (renumber artifact); discovery is glob-by-filename, so harmless. | None; optionally note in a comment. | — |
| SEC-8 | Security / logging | Info | Audit trail logs truncated user free-text args + Entra `oid` — **intentional, documented** GxP requirement, config-bounded. | None; ensure log-retention/PII policy accounts for it. | — |
| SEC-9 | Security / secrets | Info | Dev-default DSN `chemclaw:chemclaw@localhost` in `config.py`/`.env.example`; all real secret fields default empty. No live secrets in tree. | None (documented dev default). | — |
| DUP-4 | Dead code | Info | `exchange_obo()` has no non-test caller — **intentionally dormant** (gated by `entra_obo_enabled=False`, documented, tested). | None. | — |

Harness watch item (was Open Question #5): the inventory flagged the "harness" as possibly integrated
twice. **Resolved post-execution:** there is exactly one *agent*-harness path (`build_agent` → a single
`_build_harness_agent`); `report/harness.py` (D-020) is an unrelated *report*-synthesis harness. Two
different features share the name — no duplicate/dead path, no deletion needed.

---

## Top 10 to fix first (rationale)

Ordered by value-over-effort and blast-radius safety. The first block is one-line/low-risk wins;
the ⚠ items are gated on Open Questions and should not start until answered.

1. **INV-1 — declare `httpx`** (S). Trivial, removes a real reproducibility hazard affecting 4 modules.
2. **SEC-1 — stop leaking `{exc}` to the browser** (S). Medium security, one-function fix, code already
   intends this behavior.
3. **COR-5/CON-2 — fpstore statement timeout** (S). One-arg fix; corroborated by two agents; brings the
   one outlier store in line.
4. ~~**COR-1 — Nextflow JSON decode retryability**~~ — **dropped during execution: verified false
   positive** (JSONDecodeError is already retryable under Temporal's exact-name matching).
5. **CON-1 — unify the `"unknown"` actor sentinel on `service_actor_id`** (S). Removes an identity-
   attribution inconsistency the config already claims is gone.
6. **ARC-1 — fix wheel packaging** (S, ⚠ contract). Latent but ships-broken on the first non-editable
   install; cheap to fix, but changes the build contract — confirm first.
7. **SEC-2 — warn/fail on unauthenticated non-loopback bind** (S–M, ⚠ decision). Highest real-world
   security value; needs a warn-vs-fail-closed decision.
8. **DUP-1 — consolidate the two registries** (M, ⚠ decision). Removes the F7 half-migration and a real
   behavioral divergence; the biggest structural cleanup. Blocked on intended memory-source semantics.
9. **COR-3 — bound the front-door session maps** (M). Prevents unbounded per-pod memory growth in a
   long-lived service.
10. **CON-3/CON-4 — dedupe the ISO-timestamp parse and disambiguate the two `_list()` helpers** (S).
    Cheap DRY/clarity wins in the ELN adapters.

(COR-2 idempotency, COR-4 mailbox atomicity, SEC-3–7, INV-3, DUP-2/3 follow — see the execution plan
once sign-off is given.)

---

## Open questions — DO NOT self-resolve (need a human decision)

1. **DUP-1 — memory-source semantics.** Is memory synthesis *meant* to reason over **all** ELN sources
   regardless of `data_sources`, or should it honor the config like the sync does? Both are currently
   documented as intentional in different docstrings. The fix differs:
   - "all sources" → consolidate registries but give memory an explicit `all_ingest_sources()` and fix
     the misleading `eln_sync_adapter` docstring;
   - "honor config" → point memory at `active_ingest_sources()` (behavior change: default drops ORD).
2. **SEC-2 — warn vs fail-closed.** When `entra_required=False` and bound to a non-loopback interface,
   should the service **hard-fail** at startup or only **log a loud warning**? Fail-closed changes the
   dev/first-run experience.
3. **COR-2 — Nextflow idempotency now or at the live edge.** The double-submit risk is real but the path
   is dormant (`hpc_launch_interface` defaults `mock`) and the live Seqera contract isn't wired yet.
   Fix now (defensive) or defer with the rest of the HPC live edge (per `DEFERRED.md`)?
4. **SEC-3 — audit-sink failure handling.** Swallow-and-warn is an availability choice; a GxP posture may
   want fail-closed or a hard alert. Which?
5. **Agent-harness double-integration** — is a second, older harness code path still present anywhere and
   safe to remove, or was it fully superseded by F1? Needs confirmation before any deletion.

---

## Changes that would touch a public API / contract (flag before doing)

Per the guardrails, these are high-risk-by-default even where they look safe:

- **ARC-1** — editing `[tool.hatch.build.targets.wheel]` / adding packages changes the **build &
  distribution contract** (what a `pip install` ships).
- **INV-1** — adding `httpx` to `dependencies` changes the **dependency manifest** (additive, low risk,
  but a manifest change).
- **DUP-1** — consolidating registries touches the **config contract**: the semantics of
  `CHEMCLAW_DATA_SOURCES` and `CHEMCLAW_ELN_SYNC_ADAPTER` (the latter may become vestigial/removed).
- **SEC-2 (fail-closed variant)** — changes **runtime startup behavior** (a previously-starting config
  would now refuse to boot).
- **SEC-4 (`max_length` on `message`)** — changes the **HTTP request contract** (previously-accepted
  large bodies would 422).
- No **database schema** change is required by any finding. (COR-4's fix is a query change, not a schema
  change.)

---

## Guardrail note for execution (Phase 10)

- The offline sandbox **skips 25 Postgres/Temporal tests** (they run in CI). Any change touching
  `calc/postgres_store`, `agents/session_store`, `agents/session_events`, `agents/audit_store`,
  `mcp_servers/fpstore`, or the workflows must be validated in CI, not just locally. Characterization
  tests will be added before touching those paths.
- Every change will be a **small atomic commit** referencing its finding ID, with `make lint type test`
  re-run and logged in `REFACTOR_LOG.md`.

**→ Signed off and executed.** All items were implemented in waves (see `REFACTOR_LOG.md`), the two
gated decisions resolved as recorded (DUP-1 → honor `data_sources`, D-053; SEC-2 → warn-only), and
the whole set merged in PR #9. A follow-up pass (2026-07-22) added the ELN multi-ingest cursor guard,
the CI coverage gate, and closed the informational items — see `09-handover.md`.
