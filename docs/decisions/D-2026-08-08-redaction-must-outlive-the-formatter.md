# D-2026-08-08-redaction-must-outlive-the-formatter — the leak lived in the path no test took

**Status:** accepted

## Context

A fifteen-agent review of `src/chemclaw` turned up four ways a credential reaches the log stream.
Three of them share a shape worth naming: **the control was real, and something downstream of it
was not covered by the same test.**

**1. `JsonFormatter` re-rendered the traceback the filter had just scrubbed.**
`SecretRedactingFilter` renders `exc_info` itself, into `record.exc_text`, precisely so redaction
cannot be switched off by a formatting choice — D-2026-08-06-a-redactor-that-only-reads-the-message
says so in as many words: *"`logging.Formatter.format` reuses a populated `exc_text` instead of
re-rendering, so ours is what gets emitted — including under a deployment's own formatter."*
`JsonFormatter.format` did not reuse it. It called `self.formatException(record.exc_info)`, reaching
past the scrubbed copy into the original exception. Measured, with the same handler and both
formatters:

```
log_json=False -> RuntimeError: auth failed for *** against ***     leaked: False
log_json=True  -> RuntimeError: auth failed for sk-internal-SUPERSECRET-0123456789
                  against postgresql://app:Pa55w0rd-live@db.corp:5432/chemclaw
                                                                    leaked: True
```

`deploy/helm/chemclaw/values.yaml` sets `CHEMCLAW_LOG_JSON: "true"`. So **every charted deployment
took the leaking path and every test took the other one** — `tests/test_logging.py` proved redaction
against the plain formatter, and its two JSON tests exercised `exc_info` with no secret present. The
two test families were disjoint on exactly the axis that mattered. `stack_info` was worse: scrubbed
by the filter and then dropped by the JSON formatter entirely.

**2. The front door's uvicorn loggers were outside the boundary altogether.**
`configure_logging()` attaches the filters to the *root* handlers, which is complete only while
every record propagates to the root. `deploy/entrypoint.sh` starts the API as
`exec uvicorn ... --factory` with no `--log-config`, so uvicorn installs its own dictConfig first and
gives `uvicorn` a handler with `propagate: false`. `uvicorn.error` logs every unhandled ASGI
exception with `exc_info` — the records most likely to carry a DSN or an auth header — and reached a
stream this module had never touched:

```
filters on those handlers: [[]]      propagate: False
emitted: RuntimeError: upstream auth failed: key=sk-internal-SUPERSECRET-0123456789
SECRET LEAKED via uvicorn.error: True      root handler saw anything: ''
```

`core/worker_http.py` and `connectors/server_entry.py` both pass `log_config=None` for this exact
reason. The one process that holds user traffic, the LLM key and every DSN was the one that did not.

**3. A bad connector manifest silently emptied the inventory, at WARNING.**
`SecretRedactingFilter.__init__` resolves the per-connector bearer-token variable names and degrades
to `()` if that raises, for the life of the process. The degradation is defensible; its *severity*
was not, because the trigger is correlated with the consequence — a malformed `connector.yaml` is
precisely what produces the connector failures whose tracebacks then carry the token.

**4. `redact_secrets` could only redact what this process configured.**
It is a value inventory plus one URL-userinfo pattern. Everything that merely *passes through* was
outside it. Measured, eleven realistic shapes reached the stream verbatim: `ghp_`/`github_pat_`
tokens, a JWT (the inbound Entra token's shape), a libpq `password=` connection string, `sk-ant-`,
`sk-proj-`, `?access_token=`, an `Authorization` header repr. Separately, `framing_envelope_secret`
was in neither the inventory nor the chart's `secrets.keys` — so its only home was `.Values.config`,
which renders into a **ConfigMap**, readable by the OpenShift `view` role.

## Decision

**Redaction is a property of the record, and every path to an output stream must honour it.**

- `JsonFormatter` reads `record.exc_text` and never re-renders from `exc_info`; it emits the scrubbed
  `stack_info` too. Its fallback, for a handler carrying no filter, is
  `redact_secrets(self.formatException(...))` — weaker than the filter, because it cannot see the
  per-connector tokens, but never the *reason* a secret is written.
- `configure_logging()` sweeps the root's handlers **and every non-propagating logger's own**. This
  covers the entrypoint's `exec uvicorn` without the entrypoint knowing this module exists, and it
  covers the next library that configures its own logger.
- The manifest-resolution failure logs at **ERROR** with the stable marker
  `redaction_inventory_incomplete` and increments `chemclaw_redaction_inventory_failures_total`. A
  single WARNING in container startup output is the line nobody reads; the counter is the alertable
  surface, and any non-zero value is permanent for that pod.
- `redact_secrets` gains a small set of **structural** rules alongside the value inventory, and
  `framing_envelope_secret` joins `_SECRET_SETTINGS` and the chart's `secrets.keys`.

**On pattern matching, which this codebase rejected once and was right to.** A false positive
corrupts a log line, and a rule that ate a SMILES, an InChIKey, a note slug or an ADR id would be
worse than the leak it closed. So every rule is anchored on a vendor-assigned prefix (`ghp_`,
`github_pat_`, `sk-ant-`, `eyJ`) or an explicit key name (`password=`, `access_token=`, `Bearer`),
and each requires a long opaque tail. None can match this system's own identifiers, and
`test_the_structural_rules_never_touch_ordinary_content` pins that with six strings it logs
routinely. The rules are compiled at module scope and substituted with a *callable*, not a template,
because a template compiles lazily and that compilation imports — on the logging path, which
`test_filtering_a_record_never_imports_anything` forbids after an import from inside a filter
re-entered the filter under Temporal's sandbox and wedged the worker.

`framing_envelope_secret` is the only one of the seven chart secrets added because it was in the
*wrong place* rather than missing. It is not a credential to an external system, which is how it came
to have no slot — and it is the HMAC key `ENVELOPE_TAG` derives from, so anyone who reads it and can
place text into any retrieval source closes the envelope from inside and has their text read as
instructions instead of as data.

## Consequences

Redaction now holds under the formatter the chart actually ships, on the loggers the front door
actually uses, for credentials this process does not hold, and for the one secret that had no
Secret. Four new guards keep it there:

- `test_a_credential_inside_an_exception_never_reaches_either_formatter` is **parametrized over both
  formatters** rather than added as a third JSON case. The assertion set is identical and the
  formatter is the only axis, so a future formatter cannot arrive with its own private blind spot.
  That disjointness, not the missing line, is what let this ship.
- `test_configure_logging_reaches_a_non_propagating_logger` asserts on the installed filters rather
  than on captured output, because the property that generalises is "any logger that opts out of
  propagation is still swept".
- `test_no_secret_is_carried_in_the_plaintext_config_map` checks `.Values.config` against
  `_SECRET_SETTINGS` rather than a hand-kept list. Those are the same question asked twice: a value
  this codebase has declared too sensitive for a log line must not sit in a ConfigMap, which is more
  durable than a log line. A name-shaped heuristic would have missed `framing_envelope_secret`; this
  catches it.
- All eleven measured pass-through shapes are pinned, alongside the six innocent strings.

The structural rules are a redaction *floor*, not a replacement for the inventory: they cannot see a
site-specific credential with no recognisable shape, and the value inventory remains the mechanism.
What they remove is the class of credential that was never covered by anything.

Adding a plain secret to the chart is still an architecture change (D-047), still argued case by case
in `test_chart_declares_only_the_documented_secrets`, and the count still lives in that assertion
rather than in prose (D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose).
