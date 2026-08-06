# D-2026-08-06-a-secret-that-masks-itself-when-you-forget — A secret that masks itself when you forget

**Status:** accepted · **Date:** 2026-08-06

## Context

Three open rows of the v1.0 readiness analysis, all about credentials the deployment holds:

- **[M] Secrets are plain `str`, never rotated.** No `SecretStr` anywhere; `llm_api_key`,
  `hpc_api_token`, `temporal_api_key` and the DSN are one `logger.debug("%s", settings)` from a log.
  The "three-secret model" is four in `values.yaml`, and `hpc_artifact_store_token` has no chart key
  at all — so a cross-origin artifact store is fetched unauthenticated.
- **[M] Workload identity federation is dead code the docs lean on.** `identity/workload.py` has no
  production caller while `values.yaml` enables it and `deploy/README.md` presents it as *the reason*
  only three plain secrets are needed. Also `deployment-connectors.yaml` is the one pod spec missing
  the `azure.workload.identity/use` label, on the `qm` worker that talks to HPC.
- **[S] Egress is still port-scoped by default.** `networkPolicy.egressDestinations` renders `to: []`
  — any destination on those ports.

## Decision

### The leak is real, and it is the half the redactor cannot reach

The row's stated hazard — a settings repr reaching a log — has been covered since
`D-2026-08-01-a-log-line-that-joins-and-a-secret-that-does-not`: `core.logging` matches the actual
secret *values* this process holds and scrubs them from every record, including tracebacks. So the
question was whether `SecretStr` adds anything or duplicates that. Measured:

```
repr leaks:        True
str leaks:         True
model_dump leaks:  True
json dump leaks:   True
```

None of those is a log record. A response body, an exception rendered by a framework, a debugger, a
`model_dump()` into a diagnostic payload — the redactor sees none of them. So the two layers cover
different paths and both are wanted: `SecretStr` for everything that renders an object, the redactor
for everything that has already become a string by the time it is emitted.

### The conversion's own failure mode is why most of the diff is a guard

A `SecretStr` renders as `**********`. A reader that is *not* converted therefore does not crash and
does not fail `mypy --strict` — it sends the mask. Three of this repo's sinks hide that in three
different ways, and `mypy` found none of the last two:

| Sink | Why the type checker is blind |
|---|---|
| `f"Bearer {settings.hpc_api_token}"` | any object formats; the credential becomes ten asterisks |
| `options["api_key"] = settings.temporal_api_key` | the dict is `dict[str, Any]`; the object is stored whole |
| `_openai_client(..., settings.llm_api_key, ...)` | `lru_cache` erases the wrapped signature to `Hashable` in typeshed, so a `SecretStr` passed where `str` is annotated type-checks cleanly |

`mypy` reported exactly three errors and the fourth site — the embeddings client — was found by
reading. Worse, the artifact-store path would have failed **silently**: an unauthenticated fetch is a
supported configuration there, so `Bearer **********` is indistinguishable from a store that simply
rejects us.

So `tests/test_secret_settings.py` guards the shape rather than trusting the type: an AST walk
refusing any direct interpolation of a credential setting into an f-string, plus a value-level
assertion at each real sink (the launcher header, the artifact header, the Temporal client, the
webhook MAC, the anchor signature). Each was verified by mutation — reverting a call site fails a
named test.

### The DSNs stay `str`, and that is scheduling rather than reluctance

`postgres_dsn`, `postgres_migration_dsn` and `session_store_dsn` are read **directly in 26 modules**.
That is the same duplication `BACKLOG.md` records as *"the Postgres connect helper is hand-rolled 14
times"*. Converting them now means writing 26 `.get_secret_value()` calls in order to delete 25 of
them when the shared helper lands. They keep the redactor's coverage (which also matches the password
*inside* a DSN) meanwhile, and the conversion becomes a one-line change behind the helper.

### Federation: the documents were wrong, and correcting them is the fix

`WorkloadTokenProvider` has no production caller. Its only importer, `identity/obo.py`, has none
either. Both intended consumers — the connector `entra_workload`/`entra_obo` auth modes, and per-user
reads from the warehouse ELN — wait on the same real tenant. Nothing offline can legitimately call
it, so "wire it" was never available and the honest half of the row's own "either/or" is the
documents.

`deploy/README.md` said *"everything that can federate does"*, offered as the reason so few plain
secrets are needed. That has the argument backwards, which is the correction worth writing down: the
plain secrets that exist are exactly the ones federation **cannot** supply — a credential for a
system that does not speak Entra (the LLM endpoint, the git host, the HPC launcher and its artifact
store), a shared secret a git host signs with, a key deliberately held out of reach of the database
it protects, and the credential our own pods present to each other. Federation removes none of them,
today or once it is wired.

The `azure.workload.identity/use` label is added to the connector pods — every one, not just `qm`,
because which bundle needs a federated credential is a property of what it talks to and not of that
template. A test now asserts the label on every pod spec that names a ServiceAccount, discovered from
the templates: four templates had it and one did not, and nothing in the suite could see the
difference.

### Egress: the permissive state becomes a written one, and the gap stays open

`to: []` means *anywhere*. Narrowing it needs the deployment's own CIDRs, and a chart that invented a
Postgres subnet would be worse than one that admits it does not know — the row says as much. What is
available offline is removing the *inheritance*: an empty `egressDestinations` now requires
`allowEgressAnywhere`, or the chart refuses to render, naming both ways out. The same shape as the
front door's `service_allow_insecure` and the connector channel's opt-out.

This does not close the row and the row is narrowed rather than ticked. What it buys is that
unrestricted egress is a value somebody wrote, visible in review and in a diff, instead of the
default reading of an empty list.

## Consequences

- An eighth plain secret key, `hpcArtifactStoreToken`, for a behaviour the code already had and the
  chart had no way to express. Absent still means an unauthenticated cross-origin fetch, so it shares
  the webhook secret's polarity rather than the four-key one.
- `SecretStr("")` is falsy and `len()`-able, so every `if settings.x:` check survives the conversion
  unchanged — asserted, not assumed.
- Two chart-hygiene tests caught the first version of the egress guard: it swallowed the line after
  its comment, and it sat inside the slice that pins "narrowing destinations must not take DNS with
  it". The guard moved above the DNS rule, which is where it belonged anyway — it is about the whole
  policy, not one rule.
- The connector NetworkPolicy's comment claimed to be "the boundary that makes the advisory
  `X-Chemclaw-*` headers safe". It is now the second lock it always described itself as, and says so.

## Alternatives rejected

- **Relying on the redactor alone.** Measured above: it covers log records and nothing else, and
  four of the five leak shapes are not log records.
- **Converting the DSNs in the same change.** 26 call sites written to delete 25.
- **Deriving egress destinations from the chart's own addresses** (the Temporal and LLM Service
  DNS names carry their namespaces). Tempting and wrong: the Postgres host lives in a *secret*, so
  the chart cannot see it, and a derived policy would silently black-hole database traffic in a way
  first discovered in someone's cluster.
- **Wiring `WorkloadTokenProvider` to something.** There is nothing offline for it to authenticate
  to; a caller invented to justify the code would be a worse artifact than the honest note.
