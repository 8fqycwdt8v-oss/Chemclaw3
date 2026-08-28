# D-2026-08-28-a-check-is-only-as-wide-as-what-it-reads — six findings in the deployment tier

## Status

Accepted, 2026-08-28. An adversarial pass over `deploy/`, `.github/workflows/`, `infra/` and the
three chart/CI test files, taken in a container with no `helm`, no `kubeconform` and no `promtool`
— so every claim below is either measured against a running Postgres, run through `make`, or read
off the template source and labelled as such.

## Context

Six findings, and each one is the same sentence: **a control was documented for a surface wider
than the one it actually reads.** None was a wrong answer; each was an answer to a narrower
question than the one its own docstring, comment or values file asks.

| # | The control | What it reads | What it is documented to cover |
|---|---|---|---|
| 1 | `test_every_migration_is_re_runnable` | `CREATE …` statements | "each file", i.e. the whole migration |
| 2 | the chart's egress rule | nine named keys off `networkPolicy.egressPorts` | the map, which `values.yaml` presents as a knob |
| 3 | `make deps-audit`'s classification | `pip-audit`'s *output*, only when it exits non-zero | whether the locked closure is clean |
| 4 | `bootstrap.sh status` | the `temporal` CLI | the lane, on either backend |
| 5 | nine rendered-chart tests | nothing — they skip in every environment | "the CI half" of the chart's assertions |
| 6 | the Jenkins delivery target's pre-flight | the egress posture | the postures the chart refuses to render without |

### 1 · A migration that a replay would reject, invisible to the re-runnability check

`046_review_hardening_indexes.sql` ends with
`ALTER TABLE session_messages ADD CONSTRAINT session_messages_shape_known … NOT VALID;` with no
`DROP CONSTRAINT` in front of it. Postgres has no `ADD CONSTRAINT IF NOT EXISTS`, so a replay
raises `42710`. Measured on a scratch schema:
`ERROR: constraint "session_messages_shape_known" for relation "session_messages" already exists`.

`core/migrate.py` sends every file inside **one** transaction, so that is not one file skipped —
it is the whole run rolled back, `make db-migrate` exiting having applied nothing, and the recovery
being exactly what the re-runnability check's own docstring says `IF NOT EXISTS` exists to prevent:
work out by hand which statements already ran. 041, 056 and 063 all drop the object first and are
replayable; 058 drops without `IF EXISTS` and is still replayable, because 027 always creates what
it drops.

**It is grandfathered rather than repaired, and the ledger is why.** The runner keys on a checksum
of a file's *statements*, so inserting the missing drop is the destructive edit
`test_no_merged_migration_had_its_statements_changed` refuses — it would make `make db-migrate`
refuse on every database that has already applied 046. A later migration cannot help either: the
failure happens while 046 itself is replayed. The exemption therefore carries the operator's
recovery (`DROP CONSTRAINT IF EXISTS` before re-running, or record the ledger row by hand), names
the exact statement rather than the filename, and has its own staleness test — the same shape
`_REVIEWED_ROLLBACK_BREAKS` already uses two checks up.

The full-history half of that suite was run for this pass: the clone was unshallowed and
`tests/test_migrations_are_additive.py` went from **3 skipped** to 173 passing, so the immutability
guard was actually asked its question. It found nothing beyond the two grandfathered edits.

### 2 · An egress port an operator adds is not permitted

`networkpolicy.yaml` read `.Values.networkPolicy.egressPorts.postgres`, `.temporal`, `.https`,
`.llm`, `.otel`, `.chem`, `.safety`, `.calc` and `.rxnlabel` — nine keys, by name. A tenth key in
an operator's values file rendered nothing at all, which is
`D-2026-08-26-a-knob-that-renders-nothing-is-not-a-knob` one object over, and it is not
hypothetical: this chart's own `secrets.optionalKeys` ships `llmFallbackApiKey` and
`vectorStoreApiKey`, whose endpoints (a failover LLM, Qdrant on 6333) are on ports the map does not
name. Enabling either from this chart produced a credential, a URL, and a connection the release's
own NetworkPolicy silently drops — a dropped SYN is a timeout, not a refusal.

The rule now ranges over the map (Helm iterates a map in sorted key order, so the render stays
deterministic); `connectorPort` stays outside it, read from the value the connector containers
listen on. The rendered set for the shipped values is unchanged.

The test that enforced this named `rxnlabel` alone, while its own docstring said the trap applied
to `chem`/`safety`/`calc` too — the "lesson written too narrowly" shape
`D-2026-08-28-a-lane-primitive-must-verify-the-act-it-was-asked-for` describes. It is replaced by a
check derived from the addresses the chart states, so a sixth endpoint is covered on the day it is
added.

### 3 · `deps-audit` classifies an output it never bounds

The classification itself is sound and stays: `Found N known vulnerabilit…` is checked first and
never excused, so an advisory whose text mentions a connection error cannot buy an exemption
(asserted, with the noisy case, in `tests/test_deploy_chart.py`). What nothing asked is what the
audit *read*. Every branch of that logic is reached only when `pip-audit` exits non-zero — and
measured with the real tool, `uvx pip-audit --no-deps -r <a file of comments>` prints
`No known vulnerabilities found` and exits **0**.

So an export that succeeded and named nothing produced a green supply-chain gate over zero
packages, and the test that exists to be the clean control stubbed `uv` as `echo '# stub export'`
— asserting exactly that state was a pass. This repository has closed the same shape twice
elsewhere (`check_corpus_is_assembled`, `test_there_are_migrations_to_check`) and not here.

The floor is **one** package rather than a number matched to today's 212: a larger literal goes
stale in the direction that quietly stops asking, and the only thing in doubt is whether anything
at all was read.

### 4 · `status` branched on nothing

`bootstrap.sh status` shelled straight to `temporal operator cluster health`. That binary exists
only where the *native* path built it, so on a compose lane — the one CLAUDE.md tells you to bring
up, and the only one this container has — the line resolved to `temporal: command not found`, the
`|| true` beside it swallowed the 127, and the verb exited 0 having reported on one of the two
services it names. `up`/`down` branch on Docker and `start_temporal`/`stop_temporal` branch on the
compose container through `compose_service_id`; `status` was left out of both sweeps.
`temporal_port_open` needs no binary and is the signal `start_temporal`'s compose branch already
waits on, so `status` and `start` now agree about what "up" means.

Beside it, the actor's half of that ADR's rule reaches the verb the ADR is named after:
`restart-postgres` still ended by logging "postgres up" without asking whether anything restarted.
`D-2026-08-28-a-restart-that-restarts-nothing-passes-the-check-it-was-written-for` gave the
verification to the *observer* (`_chaos_postgres_bounce`) and the compose branch to the actor; the
actor still could not tell a bounce from a no-op, and the no-op is still reachable on any lane
where `stop_postgres` finds no `postmaster.pid` because something else started the server. It now
reads `pg_postmaster_start_time()` either side and dies when it has not moved — distinguishing, in
the message, "did not restart" from "could not ask".

### 5 · Nine rendered-chart tests that execute nowhere

`tests/test_deploy_chart.py` ends with nine tests marked
`skipif(shutil.which("helm") is None)` that shell out to `helm template`: they are the only things
that can see what a *missing* value renders to, which is where this chart's derivations fail
silently. The job that runs pytest is `check`, which installs uv and Postgres and no Helm; the job
with Helm is `chart`, which runs `make helm-validate` and no pytest. So all nine skipped in CI
exactly as they skip on a laptop. A test that executes in no environment is a claim, not a gate.

`tests/test_helm_chart.py`'s docstring pointed at a `docs/planning/BACKLOG.md` row ("LIVE — assert
on rendered chart YAML") to track closing this. That row does not exist and no rule could see it:
the prose contract resolves backticked *paths*, ADR ids, config keys and metric names, and a row
title is none of those.

### 6 · The delivery path pre-flights one of the two postures

`D-2026-08-26-a-knob-that-renders-nothing-is-not-a-knob` put a `fail` in `networkpolicy.yaml` *and*
one in `config.yaml`, and every other caller treats them as the pair they are: the Makefile's three
renders, `docs/guides/runbook.md` and `deploy/README.md` all pass both flags.
`deploy/jenkins/targets/openshift.sh` grew `egress_flags` for the first and nothing for the second
— `grep -rn retention deploy/jenkins/` returned nothing — and neither pipeline had a parameter that
could state it.

The failure is loud rather than silent; what is wrong is *where* it lands. The egress twin is
caught in a pre-flight naming both remedies; the retention one comes out of `helm upgrade` after
the image has been built and pushed, and `Jenkinsfile`'s own `Render the chart` stage could not be
made to pass at all for a release whose values file had not written a window. One `posture_flags`
replaces `egress_flags`, because a second copy is what let the first one's lesson go unapplied to
its twin.

## Decision

**A check is only as wide as what it reads, so the reach is asserted rather than described.** Each
fix widens the reader, or moves the test to where it can run; where widening is impossible the
exemption is exact and carries the operator's recovery.

- The re-runnability check gains an `ALTER TABLE … ADD CONSTRAINT` half, guarded on the table
  rather than the constraint name (`ADD PRIMARY KEY` names none), with 046 exempted by statement.
- The egress rule ranges over `egressPorts`; the URL-to-port check is derived from every address
  the chart states, and a second test refuses the return of a named-key read.
- `deps-audit` refuses an export that names no package, and the stubbed clean control now exports
  one.
- `bootstrap.sh status` asks the port; `restart-postgres` verifies the act it was asked for.
- The `chart` job runs `tests/test_deploy_chart.py` and `tests/test_helm_chart.py` under the Helm
  it already installs.
- `egress_flags` becomes `posture_flags` and asks both questions, with `ACCEPT_UNBOUNDED_GROWTH`
  beside `ALLOW_ANY_EGRESS_DESTINATION` in both pipelines — same default (`false`), same opt-in
  shape, same assertions in `tests/test_jenkins_delivery.py`.

## Consequences

- **The last item is not verified here and must be read as unverified.** `helm` is absent from this
  container by the terms of the pass, so the nine tests could not be executed; what was checked is
  that their arithmetic holds against the shipped values (615 = 600 + 15, 150 = 120 + 30,
  3610 = max(900, 3600) + 10, and 3610 clears both client bounds) and that every `required` and
  `fail` they depend on is present in the template. If the step goes red on its first run, the
  answer is in those nine tests, not in this change.
- A deployment enabling the failover LLM or a vector store now has somewhere to put its port. The
  shipped render is byte-identical.
- `make db-migrate` against a database whose ledger is older than its tables still fails on 046.
  What changed is that the failure is now written down where an operator looks, and the next
  unguarded constraint fails at review instead.
