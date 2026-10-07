# CLAUDE.md

Guidance for Claude Code in this repository: **current rules only**, changed in a PR like code.
Why a rule exists lives in `docs/decisions/` (`CURRENT.md` indexes what is in force);
`ARCHITECTURE.md` maps every directory.

## What the system is

An agent for pharmaceutical and chemical process R&D, in four layers whose concerns never merge:

1. **LangGraph** — conversation orchestration: one graph compiled per turn by `create_deep_agent`
   (`agent/langgraph_agent.py`), with a Postgres checkpointer (`agent/checkpointer.py`) holding
   turn state only.
2. **Temporal** — durable execution of long or expensive work (semiempirical calculations, BoFire
   BO). Queues: `background-jobs`, one derived `connector-<name>` per bundle owning durable work,
   and `connector-<name>-interactive` where a manifest queues heavy tool calls. A persisted result
   is never recomputed (D-011). Durability lives **only** here.
3. **Agent Skills** (`SKILL.md`) — judgment, loaded on demand. No agent path writes a skill.
4. **Markdown knowledge graph** — what we know. `kg/record.py` is the one write path (moving to
   Postgres under `D-2026-10-07-the-architecture-programme`).

Key mechanisms, each in one place: the tool chain is `@wrap_tool_call` middlewares
(`tool_call_middleware` in `agent/langgraph_agent.py`); the plan gate is `agent/plan_gate.py`;
the runaway cap is a first-party `before_model` counter over `ChemclawState.model_calls`
(`agent/loop_cap.py`) — never upstream's `ModelCallLimitMiddleware`; compaction is
`agent/compaction.py`; budgets derive from `tests/test_context_floor.py`'s `PREFIX_BOUND` in
`core/config/agent.py`; per-turn spend is `agent/spend_cap.py`. A helper (`task`) gets *its
caller's surface ∩ its profile − side-effecting tools*; a peer handoff (off by default) gets *the
root's surface ∩ its profile*. An agent not compiled by `build_langgraph_agent` runs without
audit, authorization or plan gate, so every agent is compiled there.

Knowledge is written directly (`created_by: agent`) and corrected, not pre-approved; a skill
changes only by blast radius (`skills/` by reviewed commit, the organisation tier by
`POST /skills/org`, a chemist's own by `POST /skills/mine`). An ELN transcription is data.

**Not in this system** (each a decision; re-adding is a new ADR): HPC, DFT, any cluster tier, a
local shell or exec path (`pyexec` is a sandboxed MCP server), web search/fetch (no egress),
LangSmith, a specialist router. Every calculation is GFN2-xTB or CREST in a `Chemclaw3-mcp` pod;
when a decision turns on a difference inside xTB's error bar, say so and propose an experiment.

## Where code goes

- **`src/` is all the code**; everything beside it is data, configuration or documents.
- **Capability code lives in a connector bundle or in `science/`**, nowhere else (reviewed, not
  tested). Scientific primitives belong in `Chemclaw3-mcp` as servers; orchestration and the cache
  stay here; a composite whose key would name its output is decomposed, not shipped.
- **`data/` holds every runtime corpus**, except `knowledge/` and `skills/` (layers 4 and 3) and
  `science/bo/benchmarks/data/`.
- A new top-level directory or subpackage gets a row in `ARCHITECTURE.md` and a `README.md`
  (`tests/test_repo_map.py`).
- Import direction: `tests/test_layering.py`, `tests/test_third_party_layering.py`; borrowed
  upstream shapes: `tests/test_upstream_surface.py`.

## Seams

- **Connector** — a directory with a `connector.yaml` (D-118); it may be off by default, because
  declaring a capability and binding its schemas into every prompt are separate decisions.
- **Data source** — `ingest/sources/<name>/datasource.yaml` plus its name in
  `CHEMCLAW_DATA_SOURCES`; a mounted share is a source too.
- **Result sink** — `publish/`, schema in `schema/result-store/`, off until
  `CHEMCLAW_RESULT_SINKS` names one. A connector produces, a source supplies, a sink consumes.
- **Delivery channel** — `deliver/`, off until `CHEMCLAW_DELIVERY_CHANNELS` names one.

## Commands

`make help` lists every target; use them — CI runs the same ones.

- The gate: `make lint` · `make type` (`mypy --strict`) · `make test` · `make check` (all three)
  · `make cov` (with the coverage floor) · `make ci` (everything CI runs, incl. the validators).
  A step is done only when its acceptance check passes and `make lint type test` is green.
- `make test` is serial; `PYTEST_WORKERS=4` is a faster local opt-in, and a failure under it is
  re-run serially before it is believed.
- Single test: `uv run pytest path/to/test_file.py::test_name` or `-k "substring"`.
- Running things: `make up` (Postgres/pgvector + Temporal) · `make db-migrate` · `make connectors`
  · `make chat`.

**Start the infrastructure before claiming anything about the suite.** Docker is available but not
started at session start: `sudo -n dockerd &`, then `make up && make db-migrate`. Without it every
Postgres-backed test skips and the run still prints green, so never report a local run as green
without saying what it skipped (`tests/conftest.py`'s epilogue counts it).

## Workflow

- **Plan non-trivial work** (3+ steps or an architectural choice) in `tasks/todo.md` as checkable
  items before touching code; mark them off as you go and close with a short review. If an
  approach fails, stop and re-plan rather than pushing on.
- **Verify before done**: run the tests, read the logs, measure. When two explanations compete,
  run it and report the number.
- **Act autonomously by default**: fix bugs from a report or a red CI to the root cause, then
  open the PR, merge it to `main` when CI is green, and delete the branch — here and in the
  companion repos, one PR per repo.
- **Ask first only when** the change is destructive, the request is ambiguous, or the work is
  outside what was asked. Skip the auto-merge in those cases, when CI is red, or when the user
  asked to review.

## Code quality

- Root cause, not band-aid; minimal, focused changes; no new bugs.
- KISS and DRY: no abstraction without a second real caller; no dead parameters, empty
  interfaces or "for later" stubs.
- Small, single-responsibility, clearly named functions; every public function fully typed.
- Config, never magic numbers: every URL, path, threshold, timeout and model name comes from the
  one `pydantic-settings` config, ENV-overridable.
- After every change run the existing tests; add tests that prove behaviour, not mocks.

**Docstrings** state *what*, *why* and the *invariants*, in about ten lines at most. No history,
no dates, no measurement narratives, no corrections of earlier prose — those belong in the commit
message, the PR description or an ADR. At most one ADR id per module docstring and none in inline
comments. **Tool docstrings are prompts**: write them for a chemist — units, what the tool is not,
and what it refuses.

## Persistent knowledge

- `docs/planning/BACKLOG.md` — the open queue, including deferred items (what / why not now /
  trigger). A row is deleted in the commit that closes it, never struck through. Claim a row by
  opening a GitHub Issue and marking the row `(issue #NNN)`.
- `docs/decisions/` — one ADR per file; `CURRENT.md` is what is in force, `README.md` the ledger.
- `tasks/lessons.md` — short rules; review at session start, add one after any user correction.

### Writing an ADR

- Write one **only for a choice between options or a decline**. A defect fix is a commit and a
  test, not an ADR.
- Name it `D-YYYY-MM-DD-<slug>.md` (today's date; the whole stem is the id) and add its row to
  `docs/decisions/README.md` in record order. Start from `docs/decisions/TEMPLATE.md`: it needs an
  `## Options` section, and a decline needs a `Revisit when:` line naming an executable trigger.
- Update `docs/decisions/CURRENT.md` when the decision changes what is in force.
- Never edit a merged ADR; the one permitted edit is adding `**Superseded-by:** <id>` under its
  status line. The frozen `D-NNN` files are never renumbered.

## Related repositories

Work only within this family (`chemclaw` and `chemclaw2*` are out of scope):

- [`Chemclaw3-mcp`](https://github.com/8fqycwdt8v-oss/Chemclaw3-mcp) — the MCP tool fleet: one
  capability per server, each with a `connector.yaml`; no outbound call at request time.
- [`Chemclaw3_ui`](https://github.com/8fqycwdt8v-oss/Chemclaw3_ui) — the frontend.
- [`Chemclaw3_mock`](https://github.com/8fqycwdt8v-oss/Chemclaw3_mock) — stand-in tools and
  data sources for end-to-end tests.

A fix that belongs in a companion repo is made there (`add_repo`, then a PR under its rules).

## Live lane credentials

Some environments carry an Anthropic credential in a variable literally named `API-KEY` (read it
with `printenv 'API-KEY'`; never print or commit it). Map it onto `CHEMCLAW_LLM_API_KEY` only
beside a gateway named by `CHEMCLAW_LLM_BASE_URL` and `CHEMCLAW_LLM_MODEL` —
`infra/live/e2e-full-stack/up.sh` does exactly that. With no gateway, the lane runs against
`chemclaw.cli.mock_llm` on loopback and needs no key.
