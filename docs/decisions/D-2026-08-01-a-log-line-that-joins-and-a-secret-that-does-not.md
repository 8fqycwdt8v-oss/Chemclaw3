# D-2026-08-01-a-log-line-that-joins-and-a-secret-that-does-not — A log line that joins, and a secret that does not

**Status:** accepted · **Date:** 2026-08-01 · **Extends:** SEC-3 (the audit sink), D-141 (the
ambient identity ContextVars), D-2026-08-01-a-turn-you-can-follow-across-a-process

## Context

Two findings arrived as one backlog row, and separating them is most of the work.

**A log line had nothing to join on.** One `%`-format string, no JSON option, and no filter
injecting the correlation/actor/session `ContextVar`s — which already existed and were already
being read by audit, by authorization, and by the connector headers. So an ordinary WARNING sat in
a stream beside the audit trail and the traces and could be tied to neither: the correlation id
that keys `audit_events`, and the session `chemclaw explain` walks, were live in the process and
absent from the line.

**Nothing redacted a credential.** `core/db.py::_redact` strips a password out of a DSN before it
is echoed in an error — in exactly one place. That is the tell: the concern is real, someone met it
once, and nothing generalised it. A DSN, a bearer token or an API key reaches a log by entirely
ordinary routes — an exception message, an httpx error, the `repr` of a settings object.

## Decision

**Two filters on the handler, and a JSON formatter behind one flag.**

`ContextFilter` stamps `correlation_id`, `actor` and `session_id` onto every record.
`SecretRedactingFilter` replaces any configured secret's *value* with `***`. `JsonFormatter` emits
one object per line when `log_json` is set — off in code, on in the chart, the split
`budget_enabled` already uses.

Both filters go on the **handler**, not on a logger, and the distinction is load-bearing rather
than stylistic: every module logs through `getLogger(__name__)`, so almost every record reaches the
root handler by *propagation*, and a filter attached to a logger is not consulted for propagated
records. Installed on the logger, redaction would have applied to almost nothing while looking
correct.

## Why not the alternatives

**Redact the audit trail's arguments — which is what the row appears to ask for.** It would break
the requirement the trail exists to meet. `SECURITY.md` says in the same breath that the trail
records each tool call's arguments, that they are user free text and may contain PII or
confidential chemistry, and that this is **intentional**: GxP requires an attributable "who did
what to which inputs" record. A redacted argument answers "who did what to *something*". The
honest reading of "nothing implements redaction" is credentials, which have no such justification
and which nothing was protecting. The deployment's retention and PII policy over the trail stays a
policy question — which is exactly what `SECURITY.md` already says it is.

**Match token-shaped strings with a pattern.** `[A-Za-z0-9]{32,}` both misses a short key and
mangles a molecule id or a content hash. Matching the *actual value* this process holds catches the
secret no matter which route put it into the message, and cannot false-positive on anything else.

**Derive the secret list from a name pattern** (`*_token`, `*_secret`, `*_key`). It would silently
include `entra_token_endpoint` and `budget_max_tokens_per_user` — a URL and an integer, one of
which would then be redacted out of every line that mentions it — and silently *exclude* the next
secret whose name does not match. An explicit list is one line per addition and visible in review,
which is what a credential inventory should be.

**Widen the `%`-format string with the three ids instead of adding JSON.** Then there are two
formats to keep in step, and the one nobody runs locally goes stale. `log_json` supersedes the
string rather than duplicating it.

## Consequences

- A WARNING can be joined to the turn that caused it, to the audit row that recorded it, and — via
  the same correlation id — to the trace that spans it.
- A credential in a log line is `***`, wherever it came from, including one passed as a `%s`
  argument: the filter runs on the *rendered* message and clears `record.args`, because leaving the
  args would let a formatter re-render the original. That is how a naive redactor is escaped, and
  there is a test for exactly it.
- An empty or short secret is not matched. `llm_api_key` defaults to `""`, and a substring search
  for the empty string matches every line — the failure that would have made redaction worse than
  none.
