# D-2026-08-06-a-redactor-that-only-reads-the-message — A redactor that only reads the message

**Status:** accepted · **Date:** 2026-08-06

## Context

From the same whole-codebase security sweep as
`D-2026-08-06-the-caller-chooses-the-kid-not-the-workload`. Two lanes — secrets/deploy and
connectors — independently reported that credentials reach logs, by different routes. They are one
defect with two halves: the redactor covered less than it claimed, and one process family did not
run it at all.

Everything below was measured. Prose is evidence about what its author believed.

## Decision

### 1. The traceback was never redacted

`SecretRedactingFilter.filter` rewrote `record.getMessage()` and stopped. But
`logger.exception(...)` and `exc_info=True` do not put the exception in the message — the
*formatter* renders it, later, from `record.exc_info`. So every credential in `_SECRET_SETTINGS`
stayed readable in the log lines a failure produces.

That is the worst possible place to miss, because a failure is precisely when a DSN, an auth
header or a token ends up inside an error string. Measured before the fix, from one
`logger.exception` call: the API key **and** the DSN password, both verbatim, while the same
process correctly redacted them in ordinary messages.

`exc_info` is now rendered inside the filter, because redaction cannot be applied to a string that
does not exist yet. `logging.Formatter.format` reuses a populated `exc_text` instead of
re-rendering, so ours is what gets emitted — including under a deployment's own formatter. That is
the same reason this is a filter and not a formatter: redaction must not be something a formatting
choice can switch off. `stack_info` is covered for the same reason.

### 2. `scheme://token@host` — the common token form — was the unmatched one

`_URL_USERINFO` required a colon: it matched `scheme://user:password@host` and nothing else. But a
personal access token reaches a git remote as `https://ghp_...@github.com/org/repo.git`, where the
whole userinfo *is* the credential. The pattern that existed covered the DSN case and missed the
token case.

Both are matched now. The two-part form still keeps its user, because a redacted line has to say
which principal failed; the one-part form has no principal to keep, so all of it goes.

### 3. A value this repository commits is not a credential

The dev Postgres default is `postgresql://chemclaw:chemclaw@localhost:5432/chemclaw`. Its password
is the literal string `chemclaw` — eight characters, so it cleared `_MIN_REDACTABLE` and entered
the inventory. Redaction then replaced **the product's own name** with `***` in every dev and CI log
line that happened to contain it, including lines with nothing to do with the database.

The fix is not a longer default password, which would ripple through `docker-compose.yml`,
`ci.yml`, `.env.example` and `bootstrap.sh` to protect a value that is public anyway. It is the
general statement: a value committed to `core/config/` is readable by anyone and redacting it buys
nothing, so shipped defaults are skipped. That covers the next such collision as well as this one.

### 4. `postgres_migration_dsn` was not in the inventory

`postgres_dsn` was. The two are deliberately different roles (`infra/sql/grants`), and the
migration DSN is the *more* privileged — it owns the schema and is the one credential that can
rewrite `audit_events`.

### 5. The connector servers ran none of this

`configure_logging()` is called at every process role's entrypoint: the front door, the background
worker, each connector's Temporal worker, every CLI. Not the connector **servers** —
`deploy/entrypoint.sh` execs `uvicorn <bundle>.server.app:app` directly, so the module *is* the
entrypoint and there was nowhere the call had been put.

So the one process family that holds per-connector bearer tokens ran with no secret redaction, no
correlation id and no actor on any line — and it is the family whose whole job is talking to
things over HTTP with a credential. It also ran with **no meter provider**, which is not "telemetry
off" but the configuration `_install_noop_meter_provider` records as leaking: with none set, the
OpenTelemetry API proxies every instrument call and retains the proxy forever.

The fix is to give the role the entrypoint it never had. `chemclaw.connectors.server_entry` does
the process setup and then serves, exactly as `connectors/worker.py` does for the durable half,
and `deploy/entrypoint.sh` execs it instead of pointing uvicorn at the app object. The app is
handed to uvicorn as an import *string*, so it is built after logging is configured — importing it
first would put every bundle's import-time logging on an unconfigured, unredacted root logger.

**Putting the call in `connector_app` instead was tried first, and is recorded because it looked
obviously right.** It is the single point all seven bundles pass through, so it seemed like the
place a new bundle could not forget. But `configure_logging()` is `logging.basicConfig(force=True)`,
which *removes every existing root handler* — and `connector_app` runs at import time in modules
that tests, the dev composite and anything else import freely. It tore out pytest's capture handler
and failed two GxP audit-trail tests that have nothing to do with logging. A process-wide side
effect belongs at a process boundary, not in a composition helper, and the full suite is what said
so: the targeted tests for the change itself all passed.

## Consequences

- Log volume is unchanged; the traceback is still emitted in full, only with credentials replaced.
  A test asserts `RuntimeError` survives redaction, because a redactor that eats the diagnostic is
  one an operator turns off.
- Shipped-default secrets are no longer redacted. This is a deliberate *narrowing* of redaction:
  the values it stops covering are the ones printed in this repository.
- Connector server processes now emit the JSON/context format the rest of the fleet does. A log
  pipeline keyed on the old uvicorn-default shape for those pods would need updating.
- Both controls are mutation-proven: removing the `exc_info` render fails the leak test, removing
  the `configure_logging()` call fails the connector test.

## Alternatives rejected

- **A redacting `Formatter` instead of a filter.** A deployment may install its own formatter, and
  redaction would then be off with no error. The existing class docstring already states this; the
  fix keeps it true rather than working around it.
- **Regex-matching token-shaped strings** (`[A-Za-z0-9]{32,}`) in tracebacks. Misses a short key and
  mangles molecule ids and hashes, of which this codebase logs a great many. Exact-value matching
  against the inventory cannot false-positive.
- **Changing the dev Postgres password** so it stops colliding. Treats the symptom, touches five
  files that must stay in lockstep, and leaves the next repo-public default to collide again.
- **Calling `configure_logging()` in each bundle's `app.py`.** Seven call sites, and the eighth
  bundle is the one that forgets.
- **Calling it in `connector_app`.** See above — `basicConfig(force=True)` is a process-wide side
  effect, and that function is imported by things that are not processes.
- **Leaving `exec uvicorn <app>` and passing `--log-config`.** Would configure uvicorn's logging,
  not ours, and still leaves nobody calling `configure_telemetry()`.
