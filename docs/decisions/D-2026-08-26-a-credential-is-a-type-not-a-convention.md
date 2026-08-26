# D-2026-08-26-a-credential-is-a-type-not-a-convention — every non-DSN secret on `Settings` is a `SecretStr`

## Status

Accepted. Defence in depth beside `core/logging.py`'s redacting filter, which stays and is still the
control.

## Context

`BACKLOG.md` §4 carried this as *"Three credentials are plain `str` on the settings object"*, already
corrected once: the hazard it originally stated is closed. `SecretRedactingFilter._redact` redacts
all nine `_SECRET_SETTINGS` values by exact match across `msg`, `args`, `exc_text` and `stack_info`,
and the module docstring names "a `repr` of a config object" as a covered route. `logger.debug("%s",
settings)` is safe today.

What the filter cannot cover is everything that is not a log record: a pydantic `ValidationError`
quoting a field value, a `model_dump()` written to a file or an event, a third-party library that
prints its kwargs, a debugger session, a traceback rendered by something other than `logging`. Exact-
match redaction is a good net under one path. A type is the guarantee on all of them.

Two things were found while working the row, and neither was in it:

- **`llm_fallback_api_key` was in no redaction list at all.** Nine settings were listed; this one is
  a tenth credential, and it is a separate field precisely so a second endpoint can hold a
  *different* key — so matching the primary's value would never have caught it. The one credential
  in `Settings` that nothing redacted.
- **`isinstance(value, str)` in `_secret_values` and `_published_values` would have silently
  disabled the filter for exactly the fields being hardened.** A `SecretStr` is not a `str`, so both
  readers would have skipped every converted credential and gone on reporting success — and
  `str(SecretStr("k"))` is `"**********"`, so even a loosened check would have matched asterisks
  against log lines. Two guarantees that look like one, where the stronger-looking one turns the
  other off. This is the whole reason the change is an ADR rather than a rename.

## Decision

**Every credential-bearing `Settings` field is a `SecretStr`**, read with `.get_secret_value()` at
each of the seven production sites:

| field | reader |
| --- | --- |
| `llm_api_key` | `agent/llm_provider.py`, `core/embeddings.py` |
| `llm_fallback_api_key` | `agent/llm_provider.py` |
| `hpc_api_token` | `connectors/qm/hpc/nextflow.py`, and `core/config/hpc.py`'s own validator |
| `hpc_artifact_store_token` | `connectors/qm/hpc/nextflow.py` |
| `temporal_api_key` | `core/temporal_client.py` |
| `note_webhook_secret` | `api/routes/proposals.py` |
| `framing_envelope_secret` | `agent/framing.py` |

**All seven, not the three the row named.** A settings object where some secrets hide in a `repr`
and others do not teaches a reader the wrong rule, and the next credential added would be typed by
whichever neighbour it was pasted beside. `framing_envelope_secret` is on the list although it is
not a credential to any external system: it is the HMAC key the envelope tag is derived from, and
anyone who learns it can close the prompt-injection envelope from inside.

**The three DSNs are explicitly out of scope**, unchanged from the row: 34 lines across 27 modules
read one, all feeding psycopg conninfo, which needs the plain string straight back. Wrapping them
would buy an unwrap at every site and the same value in the same place.

**`_secret_text` unwraps either form** in `core/logging.py`, and accepts a plain `str` deliberately
— a test that monkeypatches a bare string onto the settings object must still be redacted, because
a filter that only covers correctly typed values covers less than the one it replaced.

## The failure mode this introduces, named because it is not obvious

`f"Bearer {settings.hpc_api_token}"` compiles, runs, and sends `Bearer **********`. The request
fails as a 401 rather than leaking, which is the right direction and is still a failure — so every
site that formats a credential now unwraps first, and the two `nextflow.py` headers carry a comment
saying why. `tests/test_credentials.py` asserts each consumer transmits the real value, because
"the header is present" and "the header is correct" are different assertions and only the second
one would have caught this.

## Consequences

A credential cannot reach a log line, a dump or an error message through a route nobody taught the
filter about. Adding a secret is now one typed field rather than a field plus a remembered entry in
a list — the list is still there, still exact-match, and `tests/test_credentials.py` asserts the two
agree, so a `SecretStr` added without a `_SECRET_SETTINGS` row fails rather than quietly halving its
own protection.

## What was measured rather than assumed

- `repr(settings.llm_api_key)` → `SecretStr('')`, and `str(SecretStr("k"))` → `'**********'` — the
  second is what makes the f-string hazard above real rather than theoretical.
- `grep` for every read site, enumerated in the table: seven in `src/`, each converted and tested.
- The suite green with the conversion, including `tests/test_logging.py`'s redaction cases, which
  are what the `isinstance` finding was checked against.
