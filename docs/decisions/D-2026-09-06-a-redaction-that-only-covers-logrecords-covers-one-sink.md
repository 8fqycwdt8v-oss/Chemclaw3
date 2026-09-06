# D-2026-09-06-a-redaction-that-only-covers-logrecords-covers-one-sink — three sinks the secret inventory never reached

## Status

Accepted, 2026-09-06. Revisits nothing; it adds the readers
`D-2026-08-01-a-log-line-that-joins-and-a-secret-that-does-not`,
`D-2026-08-06-a-redactor-that-only-reads-the-message` and
`D-2026-08-08-redaction-must-outlive-the-formatter` built an inventory for and did not have.
Records a limit that `D-2026-09-05-a-proxy-moves-the-destination-out-of-the-address` conceded in
general and that had no row.

## The finding

`core/logging` holds a genuinely strong secret redaction. Driven against twelve hostile shapes with
markers set by the reviewer — `repr(settings)`, a full DSN, a bare password with no surrounding
structure, a libpq `password=` keyword string, a bare LLM key, `Authorization: Bearer`, a bearer
quoted in upstream prose, the framing HMAC, the Temporal key, a git PAT in a remote URL, a bare git
PAT, and a *caller's* JWT this process never configured — twelve of twelve came out `***`.

It is a `logging.Filter`. Three sinks in this process are not `logging`, and the same values reached
all three verbatim.

**1. An OpenTelemetry span.** `agent/audit.py` calls `SpanHandle.failed(bounded_repr(exc))`, and
OpenTelemetry's `use_span` additionally records an `exception` event carrying `exception.message`
and the **full `exception.stacktrace`**, then overwrites the status with `f"{type}: {exc}"`. None of
it is a `LogRecord`, and none of it was gated on `otel_include_sensitive_data`. Measured with a real
`TracerProvider` and an in-memory exporter, the flag at its shipped default of off:

```
status desc: ValueError: upstream said: CONTENT-MARKER-IN-EXCEPTION-patient-secret
span events: [('exception', {'exception.message': 'upstream said: CONTENT-MARKER-…',
               'exception.stacktrace': 'Traceback … /home/user/Chemclaw3/src/chemclaw/agent/audit.py …'})]
secret 'MARKERLLMKEY9a4x' present on exported span: True
status: RuntimeError: 401 from backend: Authorization: Bearer MARKERLLMKEY9a4x
```

A 401 body echoed by an upstream is the single most likely way a bearer reaches an exception
message, and it went out whole. Meanwhile `core/logging._warn_about_sensitive_data` tells the
operator that with the flag off "no first-party span carries turn content", and
`core/tracing.start_span`'s own docstring states the rule — "identifiers and counts, never a
question, an argument or an answer" — which held for the attributes this repository sets explicitly
and for nothing underneath them.

**2. A knowledge note.** Since `D-2026-09-05-the-gate-follows-behaviour-not-knowledge` a note is
written the moment it is learned and the writer commits and pushes it. `kg/render.render_note`
serialises `note.body` verbatim and nothing on that path consulted the inventory. Measured, a body
holding an LLM key and a warehouse DSN password rendered both into the committed file, while the
identical strings in a log line from the same process came out `***`.

**3. `repr(settings)` itself, outside `logging`.** Five credentials are `SecretStr` and masked
everywhere. The three DSNs — `postgres_dsn`, `postgres_migration_dsn`, `session_store_dsn` — were
plain `str`, so `repr`, `str`, `model_dump()` and `model_dump_json()` each disclosed all three
userinfo passwords. The log path is fully defended (the same `repr` through the configured handler
comes out clean), which is exactly why this survived: the one place anybody looks was already right.

## Decision

**The exception channel is filtered in `core/tracing`, not at its call sites.** `exportable_detail`
is the one function, and `SpanHandle.failed` routes through it, because a flag read at one call site
and not another is the defect shape this repository keeps finding — a caller cannot forget a rule it
is not asked to apply. Two rules, and they are not the same rule:

- **A credential is never exported**, on either setting of the flag. `otel_include_sensitive_data`
  is a decision about *turn content*; nobody enables it in order to ship this process's own bearer
  to a collector. `redact_secrets` therefore runs regardless.
- **A message is content**, so with the flag off only the failure's *class* survives:
  `ValueError: detail withheld (CHEMCLAW_OTEL_INCLUDE_SENSITIVE_DATA is off)`.

**The SDK's own exception handling is switched off and replaced.** `start_as_current_span` is
called with `record_exception=False, set_status_on_exception=False`, and `start_span` sets the ERROR
status itself from its own `except Exception` clause. Replacing rather than merely disabling is
mandatory, not tidy: `agent/audit.py` deliberately does **not** mark a *refusal* itself, on the
recorded argument that "it raises, so OpenTelemetry marks it anyway". Dropping
`set_status_on_exception` without the clause would have silently un-marked every refusal — the
control regressing while every test stayed green. `Exception`, not `BaseException`, because that is
the width `use_span` catches; a `CancelledError` is still marked by `agent/audit.py` itself.

**A knowledge note is redacted on the way to the file, not on the way to the body.**
`kg/record._note_file` applies `redact_secrets` to the *rendered* bytes, so a secret in a
frontmatter field is covered by the same call as one in the body, and `render_note` stays a pure
serializer. Redaction rather than refusal: a note that names its credential `***` is still the
record, and refusing would fail a turn's knowledge write deep in the writer for a defect the
chemist did not cause. **This one matters more than the log leak it mirrors**, and the asymmetry is
the reason it is here rather than in a row: git is the one store that leaves the pod
(`deploy/knowledge-sync.sh` pushes to a remote) and it is append-only in practice, so a secret in a
merged commit survives every later correction — which means the contradiction and supersession
controls that ADR names as the safety argument for writing knowledge directly cannot reach it.

**A DSN is a type.** `core/config/dsn.DatabaseDsn` is `Annotated[str, Field(repr=False),
PlainSerializer(mask_dsn)]`. Two mechanisms because they close different sinks and neither closes
both: pydantic renders `repr`/`str` from field *values* and dumps from *serializers*. Not
`SecretStr`, because there is no `SecretStr` DSN that keeps `psycopg` happy without an
`.get_secret_value()` at every dial site, and the defect is what a *rendering* says rather than what
the value is — attribute access is untouched and every caller that dials is unchanged. The cost is
stated: `repr(settings)` no longer shows *which* server the DSN names; `model_dump()` does, masked.

**Two recorded limits, with rows rather than silent code.** The guard is blind to gRPC and Temporal
— measured with an empty allowlist and no proxies, `grpc.insecure_channel`, the OTLP gRPC exporter
and `temporalio.Client.connect` all reached an external listener with `_refused` at 0 — and
`derive_allowed` adds `otel_endpoint` and `temporal_address` all the same, which reads as a bound.
Both docstrings now say it is not one, and `BACKLOG.md` carries the row that did not exist. The
second is the ambient-proxy hole: it is open on this repository's defaults and closed under the
chart, and two backlog rows each bounded their severity on the sentence "which is every shipped
one". Measured, that is true of Helm and false of `Settings()`, so both sentences are corrected in
place rather than the code being changed — the alternative (charging every proxy variable
unconditionally) was already measured and rejected by
`D-2026-09-05-a-proxy-moves-the-destination-out-of-the-address` because it refuses a stock checkout
behind a corporate proxy.

**What replaces the docstring claim that stood in for a control.**
`tests/test_netguard.py::test_every_served_http_client_refuses_the_ambient_proxy` walks every
`httpx.Client`/`AsyncClient` construction in `src/` and requires `trust_env=False` or
`**gateway_client_kwargs(...)`. The sentence it replaces — "every first-party HTTP client here
passes `trust_env=False`" — was the whole correctness argument for `_env_reading_destinations`
charging two destinations instead of twelve, and was measured false for six clients, all in the
live/eval lane, one of them carrying a bearer. `httpx` defaults `trust_env` to True, so this decays
by *omission*, which is the one kind of decay a docstring cannot hold. The test's own first draft
had the same defect it was written against: it accepted the `gateway_client_kwargs` unpacking on the
strength of the function's *name*, so deleting `"trust_env": False` from that mapping left it green.
It now asserts the mapping against the live function before trusting the scan.

## Consequences

An operator filtering a collector by `status=ERROR` sees exactly what they saw before — including
for refusals — and sees which exception class, and nothing else, until they turn the flag on. What
is lost with the flag on is the SDK's stacktrace event, deliberately: it cannot be filtered by
anything first-party, it disclosed absolute paths and the dependency tree, and the redacted status
description carries the same message.

`_resolved_ips` no longer records what a *loopback* name resolved to. It was seeded from a branch
`_check` passes by name without resolving anything, so `localhost`'s second A record — a hosts file,
a split-horizon resolver — would have become a permanent, port-independent allowlist entry that
`_check` never re-derives. Nothing is lost: a loopback address is exempt by address anyway.

The concession paragraph in `core/netguard.py` gains `_socket.socket`, the C base class
`socket.socket` subclasses. Its `connect` is not assignable, so this is a statement rather than a
fix, and it is LOW because anything that can `import _socket` can run `subprocess` — which the same
paragraph already concedes.
