# D-2026-09-20-a-tier-every-prefix-pays-is-still-not-a-ceiling — turning durable memory on, and what the ratchet could not see

**Status:** accepted · **Date:** 2026-09-20 · **Builds on:**
`D-2026-09-20-a-behaviour-change-is-gated-by-its-blast-radius`,
`D-2026-09-16-a-setting-that-ships-off-is-a-feature-nobody-has` ·
**Supersedes** `D-2026-08-15-a-turn-needs-somewhere-to-put-intermediate-work`'s default for
`agent_memory_enabled`. Everything else that ADR decides — the three routes, the withheld verbs, the
actor-keyed namespace — is untouched.

## Why the default moves

`D-2026-08-15` shipped `agent_memory_enabled` off and argued it well:

> Off by default, and the default is about *data* rather than about the code being unproven:
> enabling it creates the `store`/`store_vectors` tables and starts writing files a turn authored to
> a place that outlives the session. A deployment should decide that, not inherit it.

That was right while the only thing behind the flag was a scratchpad that outlives a session. It is
no longer. `local_skills.personal_skills_available()` reads the same flag, so with it off:

- `POST /skills/mine`, `POST /skills/org` and the proposal-acceptance route all answer **503**;
- `propose_skill` is not bound at all, so the agent cannot even suggest a skill;
- `make distill --propose` refuses;
- and the human gate on agent-proposed behaviour — the entire subject of
  `D-2026-09-20-a-behaviour-change-is-gated-by-its-blast-radius` — is a gate nobody can reach.

`D-2026-09-16-a-setting-that-ships-off-is-a-feature-nobody-has` is the general form of that, and
`D-2026-09-18-a-skill-a-chemist-keeps…` already leaned on it when it declined to add a *second* flag
beside this one. The coupling it chose is right; what was wrong is which way the one flag pointed.

**What turning it on actually starts is larger than the skills tiers, and is stated rather than
buried**: `/memories/` is mounted for every authenticated turn, so `write_file`/`edit_file` under
that root become durable and agent-authored, bounded by `agent_memory_max_files` and evicted by
`BoundedStoreBackend`. A deployment that does not want that sets the flag false in
`deploy/helm/chemclaw/values.yaml`, where the chart now *states* the posture rather than inheriting
it, and loses the behaviour gate with it.

## The ratchet was measuring a system nobody runs

Flipping it exposed a defect one predicate wide. `tests/conftest.py` sets no `session_store`, so the
suite runs at the code default of `"memory"`, while the shipped chart pins
`CHEMCLAW_SESSION_STORE: "postgres"`. `personal_skills_available()` is
`agent_memory_enabled and session_store == "postgres"` — so `build_langgraph_agent` stripped
`propose_skill` from every graph `tests/test_context_floor.py` compiled, and the fleet paid for it.

That is
`D-2026-09-05-a-ratchet-that-binds-no-connectors-measures-a-smaller-system` exactly, one predicate
over instead of one bundle. `_as_a_deployment_runs` is the correction: `_observed_prefix`, `_floor`
and `sent_prefix` build under `postgres`, which is a pure predicate — no database is opened, because
the store only ever arrives as an argument.

**Measured on the corrected basis: 69,872 against the 70,600 ceiling, of which `propose_skill` is
462.** 728 of headroom, so `CEILINGS` does not move and neither do the two compaction defaults
derived from `PREFIX_BOUND`. The raise that was budgeted for was not needed; the measurement is
recorded because the next person to add a first-party tool has 728 tokens and not 1,188.

## The organisation's tier gets an allowance, not a ceiling

`ORG_SKILLS_ALLOWANCE = 3_450`, beside `LOCAL_SKILLS_ALLOWANCE = 5_700` and outside `CEILINGS`.

The tempting argument for folding it in is that this tier is *not* like the personal one: it is the
same prefix for every person, which is what `CEILINGS` says it bounds. The argument fails on the
second half of that sentence — `CEILINGS` bounds *"the same prefix for every deployment and every
person"*, and these bytes are written by one deployment's administrators. Folding a worst case in
costs `BUDGET_THREAD_ALLOWANCE` the same amount on **every deployment on earth**, including every
one whose org tier is empty, because `agent_context_token_budget` cannot rise. That is
`LOCAL_SKILLS_ALLOWANCE`'s own refusal and it is not weaker here.

What the "every user pays it" difference *does* change is the cap, not the accounting:
`agent_org_skills_max = 12` rather than the personal tier's 20, derived from the fan-out multiplier.
The tier is on the turn's backend and a helper is compiled through the same builder over the same
backend, so a four-helper fan-out sends it five times — 12 rows is ~16,700 tokens across one turn's
graphs where 20 would be ~27,900.

**The allowance was derived and then measured, and the two disagreed.** The derivation said 3,345
(12 rows at the ~278 tokens `LOCAL_SKILLS_ALLOWANCE` measures per maximal row, plus the empty
mount). Measured on this tier's own `/org/` mount: **3,330**. The difference is small and the
correction is the point — a number carried across from a neighbouring tier is a claim about the
wrong commit (`D-2026-09-03-a-number-in-prose-is-a-claim-about-a-commit`), and the mount path is
part of what the listing costs.

The runtime charges the real thing regardless: `agent/context_budget.prefix_tokens` reads the prefix
of the call in flight, so a deployment that fills its tier is compacted against what it actually
sends. What these allowances hold is the other question — how large can that get — and each fails
loudly when a cap, a description limit or upstream's listing format moves.

## The grants ordering the flip made live

`store` and `store_migrations` are created at *runtime* by `AsyncPostgresStore.setup()` rather than
by a numbered migration — deliberately, since transcribing upstream's DDL into `infra/sql/` is a
second definition a bump walks away from. `infra/sql/grants/app_privileges.sql` therefore grants on
them only `IF to_regclass(...) IS NOT NULL`, and `deploy/entrypoint.sh`'s `migrate` role is a
`pre-install` hook that runs before any app pod exists.

So on a fresh install the tables did not exist when the grants ran: the runtime role got no
`INSERT`/`UPDATE`/`DELETE` on `store`, and every `/memories/`, `/mine/` and `/org/` write failed
until the *next* release's grants pass. That grants file already describes the symptom — "the one
group whose grant lands on the second run — the same run that first needs it" — and it was harmless
only because nothing ever wrote to `store`.

`chemclaw.agent.store_setup` is the middle term of that sequence now, and it runs **as the
migrator**. Reusing `memory_store()` was the obvious implementation and deadlocks the install on its
own chicken-and-egg: that function builds over the checkpointer's pool, which is the runtime
credential, and the runtime role has no `CREATE` on the schema until the step *after* this one
grants it. `migration_dsn()` is the one resolution `core/migrate.py` and `core/grants.py` already
share, so the three steps of one Job cannot disagree about who owns the schema.

## Consequences

- Every deployment that takes this release gets durable agent-authored `/memories/` writes and a
  reachable behaviour gate. A deployment that wants neither sets one value in the chart.
- 728 tokens of prefix headroom remain, and the next first-party tool is measured against that.
- The two stored tiers are bounded by their own allowances and by nothing in `CEILINGS`, so a
  deployment's own bytes never charge another deployment's thread budget.
- A fresh install's first turn can write. The ordering is held by
  `tests/test_helm_chart.py::test_the_pre_upgrade_hook_migrates_then_reconciles_grants` on the exact
  step list and by `tests/test_database_privileges.py` with the reason.

**Revisit when:** a first-party tool cannot be added because the 728 tokens are spent, or
`SERVED_ELSEWHERE_ALLOWANCE` moves. Either says the ceiling is the binding constraint again rather
than a tripwire, and the raise then has to be taken against `BUDGET_THREAD_ALLOWANCE` deliberately
rather than absorbed.
