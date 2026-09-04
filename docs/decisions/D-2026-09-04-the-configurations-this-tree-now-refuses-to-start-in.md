# D-2026-09-04-the-configurations-this-tree-now-refuses-to-start-in — the TLS guard reaches the transcripts, and four other startup refusals

**Status:** accepted · **Date:** 2026-09-04 · Operator-facing. Every item here changes whether a
process boots.

## Context

This repository prefers a loud misconfiguration to a silent one, and `Settings()` already refuses to
start for several. The hardening pass found that the most important of those guards did not cover
the most important DSN.

**`require_pg_tls` never checked `session_store_dsn`** — the one DSN that literally carries the
transcripts its own refusal message names. And it re-parsed every DSN by hand in a way that
disagrees with libpq, which gave two working bypasses: `hostaddr=` (which libpq honours and the
guard did not read) and a repeated `sslmode` (libpq takes the last, the guard took the first).

Beside it, four smaller admissions: an empty `*_env` in a connection block silently dropped the
credential, so a mounted manifest with a blank key connected anonymously or raised a bare retryable
`TypeError`; `retrieval_source_weights` accepted `NaN`, because `nan <= 0` is False, turning every
fused retrieval score into `NaN` and collapsing hybrid ranking to alphabetical note order;
`log_level` was the one enum-shaped setting with no validator, so a typo failed inside
`configure_logging()` rather than at construction; and `template_run_timeout_seconds` was checked
against a step budget that bounds nothing on the path that overruns it.

## Decision

The guards are widened, and the resulting refusals are stated here rather than discovered in a
cluster. **Under `entra_required=true`, `Settings()` now refuses to start for:**

| Configuration | Why |
| --- | --- |
| a non-loopback `session_store_dsn` without `sslmode=require\|verify-ca\|verify-full` | it carries the transcripts |
| a DSN using `hostaddr=` | libpq honours it; the hand parser did not read it |
| a DSN repeating `sslmode` | libpq takes the last one; the hand parser took the first |
| a DSN libpq cannot parse | fails closed rather than waving it through |
| a `CHEMCLAW_LOG_LEVEL` `logging` will not accept | it failed later, inside `configure_logging()` |
| a non-finite `retrieval_source_weights` entry | `NaN` collapses hybrid ranking silently |
| a mounted manifest with a blank `*_env` | a blank key is not a credential |
| `template_run_timeout_seconds` below one `job` step's real bound | see below |

The DSN parsing is now one function reading `conninfo_to_dict`, honouring `hostaddr or host` and
libpq's last-wins `sslmode`. Fixing the parse at its root rather than patching each bypass is the
whole of it: a hand parser that disagrees with the library the connection actually uses will always
have a third bypass nobody has found yet.

**`template_run_timeout_seconds` rises to 25,320 s.** A template `job` step is bounded by
`wrapper_execution_timeout()` = `connector_job_timeout_seconds + activity_timeout_seconds * 4` =
18,120 s, inside a run that was capped at 7,200 s — so a long job step ended the whole run as a
silent `TIMED_OUT`, with no push-back and no record, and the validator that exists to catch this
checked a setting that bounds nothing on that path. `connector_job_timeout_seconds` cannot come down
to meet it (its own floor is 15,030), so the run ceiling is the number that moves: one job step at
its real bound plus the entire eight-ordinary-step allowance that sized the old default.

`core` may not import `durable`, so `_WRAPPER_FINISH_STEPS = 4` is restated in `core/config/` — and
**pinned**: a test asserts the whole identity `wrapper_execution_timeout() == connector_job_timeout_seconds
+ activity_timeout_seconds * _WRAPPER_FINISH_STEPS` and goes red against a hypothetical fifth
post-child step. Without that, the fix would have moved the duplication rather than removed it.

## Consequences

A site that split its session store and omitted `sslmode` will not boot after this release. That is
the point: it was running with the transcripts in plaintext and the guard whose message names them
said nothing.

**Raising `connector_job_timeout_seconds` now requires raising `template_run_timeout_seconds` too**,
refused at startup with the number named.

A Unix-socket DSN in `host=/var/run/postgresql` form is still refused under `entra_required` and
told to add `sslmode`. That is a pre-existing wart, unchanged here, and a `BACKLOG.md` row.

`require_pg_tls` raises `ValueError`, which is **not** in `durable/publish._BAD_DATA_TYPES`, so a
refusal on the publish sink path is retried forever. Pre-existing, found by this pass, and filed
rather than fixed inside a config change.
