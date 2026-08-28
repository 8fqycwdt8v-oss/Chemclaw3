# Working the curated backlog — 2026-08-27

Follows the review merged as #255, which deleted six resolved rows and corrected twelve. This is
the implementation pass over what that review left standing.

## Triage: what can actually be built offline

A row is **buildable** here only if its fix is specified, offline-verifiable, and does not need a
decision only the owner can take. Everything else keeps its row and its stated trigger.

### Buildable — worked in this pass

**Wave 1 — self-contained defects, disjoint files**
- [x] W1-A A solvate collapses onto whichever fragment is larger (`core/chem.py`)
- [x] W1-B Surface `invalid_tool_calls` (agent middleware chain)
- [x] W1-C A timed-out attachment parse still runs to completion — cheap half (`ingest/documents/sync.py`)
- [x] W1-D `observations_status_idx` does not cover its query (`memory/observations.py` + migration)
- [x] W1-E A jobs-only bundle has no reachability signal (`connectors/health.py`)
- [x] W1-F No `framing_envelope_secret` warning on a durable deployment (`core/config`)
- [x] W1-G No session pagination and no per-session delete (`session_store` + `api/routes/sessions.py`)
- [x] W1-H `connector_job_timeout_seconds` bounds every bundle identically (`JobSpec.timeout_seconds`)

**Wave 2 — cross-module, each wants an ADR**
- [x] W2-A `reaction_fingerprints` keys on a bare reaction id (composite key + migration)
- [x] W2-B The digest is written to a mailbox with no reader (`GET /digests`)
- [x] W2-C A retracted ELN entry stays current evidence (tombstone half)
- [x] W2-D The sixteen periodic workflows can still hang instead of failing
- [x] W2-E `session_owners` grows without any age-based disposal
- [x] W2-F Split-conformal uncertainty is unwired
- [x] W2-G A Hessian is cached and never published
- [x] W2-H Make an ingest rejection answerable instead of only logged

**Wave 3 — the ones that are mostly a decision plus a small diff**
- [x] W3-A The stored-message conversion is a destructive in-place pre-upgrade rewrite
- [x] W3-B No connector or MCP tool result is framed
- [x] W3-C Every structured tool result reaches the model as pydantic repr
- [x] W3-D `fetch_artifact` is a tool that can only refuse
- [x] W3-E The background worker is a hard singleton (audit the other activities)
- [x] W3-F One merge added eighteen tools and 32% to what every turn costs
- [x] W3-G The live lane and the four-repo lane fight over `chem` and `safety`
- [x] W3-H A pinned template's arguments go unchecked once its bundle stops being ours

### Not buildable here — row and trigger stay

Each names the input that is missing, not an effort estimate.

- **The unauthenticated `X-Chemclaw-Actor` header becomes durable attribution** — full closure needs
  an actor assertion bound to the call (OBO or a signed memo). Re-introducing OBO is a new decision
  (D-2026-08-15 deleted it as a control with no caller); building it against no tenant would rebuild
  exactly that shape.
- **The results store has no live target** · **Nothing has measured how many rows a real corpus
  produces** · **Postgres and Temporal are neither deployed nor owned** · **Two of the four
  deployables have no chart** — each needs infrastructure this environment does not have. Writing a
  chart against an imagined Service/Route is inventing somebody's deployment.
- **No external benchmark has ever been run** — ChemRAG-Bench is an external download and D-089
  forbids external sources at runtime; vendoring it is a licence review, not a code change.
- **`deep-research` has no index behind it** — wants `litsearch` built in `Chemclaw3-mcp`, which is
  its own multi-corpus build, not a change here.
- **Memory records; it does not change what the next turn does** · **`turn_cost_ratio` scores a
  fixture** — both blocked on a deployment with real session history. `make trajectory-census`
  already answers the first the day one exists.
- **A tool schema is 38% developer rationale** · **Half the probe corpus tests one tool** — both
  gated on a live-lane run proving every probe still reaches its tool; trimming a prompt without
  that gate is a regression with a good-looking metric.
- **`pyexec` is merged in the fleet and unreachable** — needs an ADR on whether
  `CHEMCLAW_CONNECTORS_ENABLED` stops meaning "empty loads everything", which is a chart-wide
  behavioural change. Recorded as the decision it is rather than wired quietly.
- **Turn the image scan back on** / **The image vulnerability scan is not merged as a gate** — the
  hold is a stated measurement (phantom findings contradicting the build's own filesystem listing)
  and re-checking it needs a current trivy run against a built image in CI, not a workflow edit.
- **Settle `pytest-xdist` on a real runner** — the row's own closing condition is a comparison on a
  GitHub runner; a sandbox number says nothing about CI, which is the row's whole point.
- **Recover the flow-Suzuki screen** · **The PR-gate costs 1.81 s per proposed note** · **The
  turn-time comparison cannot diff what the ELN gives structured** · **`read_corpus` re-reads the
  entire ELN** — each changes what a `Component`/`Protocol`/submission *is*, and each wants its own
  measurement on a real corpus before the shape is chosen.
- **This environment's `API-KEY` comes and goes** — operational, no code.

## Review

Twenty-two rows worked across three waves. **Six of them ended as an argued
decline or a narrowing rather than code**, which is the part worth reading:

- **Split-conformal uncertainty** and **JSON tool payloads** were declined on
  measurement and moved to `DEFERRED.md`'s declined table, so neither is
  re-proposed as an oversight. In both cases the number pointed the opposite way
  to the row's expectation — conformal intervals are noise below n=59, and JSON
  is *shorter* than the repr, not longer.
- **`invalid_tool_calls`** was already fixed by a merge that landed while this
  branch ran; only the missing compiled-graph coverage was added, and no second
  mechanism was written.
- **The retraction row's stated foundation was gone** — the ELN sync writes no
  notes any more, so `Note.valid_to` was not the receiving end. `prune_share`
  could not port whole either: a crawl enumerates, a delta sync does not, so
  mark-and-sweep would have retired the whole corpus on run one.
- **The workflow-failure row's argument was false**: `ScheduleHealth` carries no
  run outcome, so a scheduled job failing every fire reads *healthier* than a
  parked one.
- **The singleton row's two suspects were both safe**, and the real blocker was
  something no row named.

**Four defects were found outside any row**, each by an agent doing adjacent
work: a dead truncation signal in every deployment (`DatedIngest` swallowed a
structural Protocol), `/readyz` and `/metrics` holding two definitions of "down",
published gradients 1.89x too large under a correct-looking unit string, and a
note-reindex that retires live notes the day anyone scales the worker past one.

Three prose counts were removed rather than corrected — "seven middlewares",
"79 call sites", "three probe states" — because incrementing a number in prose
only moves the date it goes stale.

## Merge-time tasks (cross-agent couplings)

Each is a one-line edit that no single agent could make, because the file
belonged to a sibling working concurrently.

- [x] `ingest_rejections` (migration 065) needs a `_NOT_PRUNED` entry in
      `durable/retention.py`. `tests/test_retention.py::test_every_table_in_the_schema_has_a_disposal_decision`
      is red until it lands. The table is self-bounding per its own README row.
- [x] Delete every `BACKLOG.md` row this branch closes, in the merging commit.
- [ ] Re-run the whole gate after the last agent lands: the per-agent runs each
      saw a tree the others were still editing.
- [x] `tests/test_upstream_surface.py` needs a row for the third upstream-internal
      read added in `connectors/server.py` (`Tool.fn` / `list_tools`). That file
      exists to hold exactly this count, so a new coupling that is not listed is
      the defect it guards against.
- [x] `tests/test_publish_projection.py`'s docstring measurement ("all 79 `_fact`
      call sites pass an already-canonical unit; one conversion observed") is
      stale — two sites convert on a live path now.
