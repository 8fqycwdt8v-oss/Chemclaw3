# Triage — 109 verified survivors, clustered for shipping

Every finding below survived two independent refuters (neither refuted it) and carries its agreed
severity: the **lower** of the two lenses, so one sceptic could de-escalate and neither could
escalate. `survivors.md` is the generated roll-up; this file is the plan built from it.

**What "verified" does and does not mean here.** 124 verdict files produced 109 survivors and
**1 outright refutation**. A filter that kills 1 in 110 is not separating true from false — what the
refuters actually did was *sharpen*: they corrected three wrong supporting claims on the cross-tenant
finding, established that a "real-looking tenant URL" was the literal string `TENANT`, and found the
`removeBondStereo` half of the stereo defect that no reporter had tested. Read the survivor list as
"mechanism confirmed, severity re-graded", not as "adversarially proven".

## Shipped (7)

Each has a test that fails on the pre-fix tree, `ruff` + `mypy --strict` clean, on
`claude/codebase-review-refactor-ar3csf`.

| # | Finding | Severity | Regression proof |
|---|---|---|---|
| 1 | `standardize` collapses enantiomers to one compound id | critical | 8 failed → 13 passed |
| 2 | "Full factorial" is not the cross product, labelled exhaustive | critical | 6 tests fail pre-fix |
| 3 | `−78 °C` ingested as `+78 °C` (7 of 8 dashes drop the sign) | high | 8 tests fail pre-fix |
| 4 | Misspelled `harness_autonomy` silently removes the plan gate | high | 8 failed → 12 passed |
| 5 | Embedding cache raced from several threads | high | 6/6 trials → 0/6 |
| 6 | An outage reported to the model as "nothing on file" | high | fails pre-fix |
| 7 | A report section discarding healthy sources' evidence | high | fails pre-fix |

## Cluster A — the connector tool surface (ship first, one PR)

Two findings that **compose into one attack**, both CONFIRMED by both lenses:

- An endpoint with no `tools:` yields `allowed_tools=None`, read as *everything the server offers* —
  measured: four tools including `propose_knowledge_note` and `exec_shell` bound from a manifest
  declaring none.
- `ToolNode` is last-wins over `[*core_tools, *connector_tools]`, so a connector-advertised name
  **replaces** core's implementation — measured: `find_notes`, `record_confirmed_answer`,
  `get_durable_job_status`, `read_attachment` all resolved to a rogue tool.

`job_tools()` already refuses exactly this for job names and says why. The fix is to apply the same
refusal to endpoint tool names, and to make an absent `tools:` mean *none* rather than *all*.
Blast radius: `connectors/transport.py`, `connectors/registry.py`, `agent/langgraph_agent.py:221`.

## Cluster B — ELN sync loses entries (one PR)

Four findings, one subsystem, all data-loss:

- A truncated chunk advances the cursor past entries it never fetched (whenever any entry carries
  `modified_at`).
- A transient git outage is filed as per-entry bad data, and the cursor advances past what was lost.
- Rows tied on the watermark beyond `fetch_limit` are stranded forever, and nothing reports it.
- The cursor advances on `max(created, modified)` while the fetch pages on `COALESCE(modified, created)`.

These interact — fixing one without the others leaves the same class open. Ship as one change with a
property test over cursor advancement.

## Cluster C — Postgres connection budget (one PR)

- A process opens **three** pools; every bound and every gauge counts one.
- The rendered chart computes 15×8=120 against a real **216** while declaring 136, so
  `ChemclawFleetAboveItsConnectionCeiling` **can never fire**.
- `tests/test_deploy_chart.py:979` encodes the same one-pool-per-process error.
- `grants/app_privileges.sql` aborts in full once any table in `public` is owned by the app role.
- A split-principal deployment cannot take a single turn (no `CREATE` on `public`).

The test and the alert encode the same wrong model as the code, so all four move together.

## Cluster D — MCP fleet resource exhaustion (`Chemclaw3-mcp`, one PR)

- An atomic number of 0 terminates the server process — exit code 0, no error response.
- One tool call can burn unbounded CPU: no size cap, no atom cap, no timeout, no concurrency limit.
- `MAX_COMPONENTS` bounds the component count, not the response — 6 KB in, 29.6 MB out.
- Eight `render_structure` calls with a 4 KB SMILES take the pod out of service; **a single
  2,500-character SMILES (~1,250 tokens, well within what a model emits) left `/healthz` dead >30 s**.
- `rxnpredict`'s synchronous tools run RDKit on the event loop.

## Cluster E — chemistry correctness in the MCP fleet (`Chemclaw3-mcp`, one PR)

**Highest consequence remaining.** A chemist is shown these answers directly:

- A multi-fragment SMILES silently defeats every pair rule and over-fires every counted rule in the
  hazard screen.
- `ich_impurity_limit("CO")` returns **cobalt's** PDE — `CO` is methanol, and the fleet's own props
  server emits exactly that string.
- `props` and `safety` disagree on triethylamine's ICH Q3C class and limit (5000 vs 640 ppm).

## Cluster F — UI/BFF availability (`Chemclaw3_ui`, one PR)

- A lost upstream connection leaves the browser's response open forever.
- 128 concurrent SSE streams wedge every other request through the BFF, forever.
- Unauthenticated slow-body requests exhaust the same 128-socket pool.
- Agent-authored markdown can forge the "this figure came from a tool" provenance mark.
- The banner's Retry control does nothing after a failed turn.

## Cluster G — authorization (one PR)

- Any authenticated principal can read every other principal's durable job. Three of the reporter's
  supporting claims were **wrong** (reports write no `job_records` row; no tool returns
  `payload`/`requested_by`/`session_id`; it is cross-*principal*, not cross-tenant) — but
  `QMJobResult.molecule_smiles` and `XtbResult.smiles` ride inside `result`, so structures leak by a
  different route than the one filed.
- A one-shot plan approval is not spent on two reachable turn endings.
- `entra_expensive_actions` is inert (medium).
- The warehouse ELN retrieve half has no authorization gate, and its binding cannot declare one.

## Parked — needs a human, not a fix

- **`deps-audit` says clean; GitHub says 3 vulnerabilities.** Every hypothesis I can test from here
  came back clean (dev deps included, 246 packages, both PyPI and OSV feeds, lockfile identical to
  `main`). I cannot read the Dependabot alerts from this session. Three explanations remain open and
  **two of them need no code change.** See `findings/round1/supply-chain-gate-vs-github.md`.
- **F0-3, the migration-immutability guard**, is vacuous on a shallow clone and its sibling's failure
  message instructs you to delete a live control. Fix is known; it touches a test that gates CI, so
  it wants a deliberate decision rather than being folded into another PR.

## Merge order

Contract changes land **mock → mcp → backend → ui**. Within this repo: behaviour-changing fixes
first, behaviour-preserving refactors last, so the refactors do not obscure the fixes in a diff.
Nothing merges on a red gate.

## Process failures of my own, recorded rather than smoothed over

1. **Verification was launched against a moving snapshot — twice.** Every slice that reported after a
   wave launched landed unverified, creating a silent gap that reached 55 findings. Caught only
   because the synthesis script counts unverified crit/high explicitly instead of reporting a
   survivor total that looks complete.
2. **A tree-wide `git stash` destroyed a reviewer's live mutation experiment** for ~3 minutes.
   Any `runner_trace` finding from that window should be re-derived rather than trusted.
3. **Two self-inflicted vacuous passes.** A `PYTHONPATH` mistake made a before/after comparison
   identical (I nearly reported the stereo fix as ineffective), and the synthesis script reported
   `0 survivors, 0 of everything` when run from the wrong directory — a green-looking answer from a
   check that read nothing. Both are the exact shape this audit keeps finding in the codebase.
