# Task: whole-codebase security review, hardening, refactor and simplification (2026-08-06)

Branch family: `claude/codebase-review-hardening-ytzm8b-*`. Six merged PRs (#135–#140), six ADRs.

**The brief was open — "a huge code review, security review, hardening, refactoring, improvement
and simplification session, heavily relying on subagents and agent teams" — and the first decision
was what *not* to do.** This is the **second** whole-codebase sweep: `refactor-hardening-plan.md`
ran nine parallel agents on 2026-08-02 and executed R0–R6, five further deep reviews landed after
it, and `BACKLOG.md` already carried 132 open items. A naive re-sweep would mostly have re-reported
known work — the failure `tasks/lessons.md` records under parallel-session hazards. So it was
scoped as a *successor* sweep: security (never given a dedicated pass), plus the within-package
duplication and complexity the first sweep's "13 packages is right" verdict deliberately left alone.

## The fleet

Eleven disjoint finder lanes — six security (`S-A` front door, `S-B` identity/RBAC, `S-C`
connectors/outbound, `S-D` data plane/injection, `S-E` prompt injection/PR-gate, `S-F`
secrets/deploy/supply chain) and five quality (`Q-A` store duplication, `Q-B` connector
boilerplate, `Q-C` complexity, `Q-D` error handling, `Q-E` test honesty) — **owned by file, not by
feature**, which is what `lessons.md` says made six concurrent agents work last time.

Two rules did the actual work:

- **Every finder deduped against `BACKLOG.md`, `DEFERRED.md`'s declined list, and this file's
  "Measured, and not defects" section before reporting.** Only 3 of 48 came back already-tracked,
  so the briefing held.
- **Every finding went to a second agent required to *execute* a repro**, defaulting to REFUTED
  when the probe did not demonstrate the failure. Result: **43 confirmed, 2 refuted, 3 already
  tracked, 0 unverifiable**, and eight severity corrections — six down, two up.

## What shipped

- [x] **#135 — the token-validation path.** `PyJWKClientError` is not an `InvalidTokenError`, so it
      escaped both handlers and surfaced as a 500; the same path was an amplifier, because PyJWT
      re-fetches the JWKS on every `kid` miss and the `kid` is chosen by an unauthenticated caller.
      **50 anonymous tokens → 50 outbound fetches to the tenant IdP; 1 with the cooldown.** Plus
      `/openapi.json`, unauthenticated, which the auth-coverage test skipped *while documenting
      that it did*.
- [x] **#136 — the redactor.** It rewrote the message and never the traceback, so every credential
      in the inventory was readable in exactly the lines a failure produces. Plus the PAT-in-URL
      form, the migration DSN, and the shipped default that made redaction replace the product's
      own name with `***`. Connector servers turned out to have **no entrypoint at all**.
- [x] **#137 — the safety screens.** Pair rules are a cross-product, so **13 KiB of SMILES produced
      251,000 flags and blocked the connector's event loop for 2.48 s**; the request cap cannot see
      it because the amplification is in the response. Bounded to 1,088 flags / 26.3 ms, off the
      loop.
- [x] **#138 — three tools that said the data was kept.** `report_measurement` said "the
      measurement is kept" on **every call in every unconfigured deployment**, because
      `calibration_enabled` is False by default and `record_observation` returned `0` for both
      "stored, nothing matched" and "did nothing".
- [x] **#139 — the injection envelope.** The nonce was per-process; a durable session outlives a
      process, and the instructions say *only* the exact tag marks retrieved data. Plus a defang
      that caught every visible spelling of the tag and none of the four invisible ones.
- [x] **#140 — a gate that names nothing.** `authorize_trigger("request_development_report")` was
      inert on the shipped chart, and no other gate covered it.

## Review

**What was measured, not argued.**

| | before | after |
|---|---|---|
| anonymous unknown-`kid` requests → JWKS fetches | 50 → 50 | 50 → 1 |
| legitimate warm-cache request under a 40-request flood | 9.46 s | not reachable (401, no fetch) |
| credentials in a `logger.exception` line | API key + DSN password, verbatim | redacted |
| 13 KiB safety screen | 251,000 flags / 2.48 s blocked | 1,088 flags / 26.3 ms, off-loop |
| `report_measurement` on the default config | "the measurement is kept" | "NOT recorded" |
| envelope tag across two processes | two tags | one (when configured) |
| invisible-character tag spellings defanged | 0 of 4 | 4 of 4 |
| `request_development_report` gated on the shipped chart | no | yes |

**Three fixes were generalised past what was reported, and that is where the value was.**
`screen_genotoxic_alerts` had the identical cross-product nobody reported (640 components →
102,400 alerts), so the bound is one function both screens call. The `report_measurement` finding
was about a swallowed write; running it showed the *default* configuration lies on every call. And
`authorize_trigger` got a test that AST-walks every call site, because there had already been two
occurrences of "a gate that names nothing" and an instance fix would not stop the third.

**Where I was wrong, twice, and the full suite is what caught both.**

1. `configure_logging()` in `connector_app` looked obviously right — the single point all seven
   bundles pass through. It is `basicConfig(force=True)`, which removes every root handler, and
   that function runs at import time in modules tests import freely. It tore out pytest's capture
   handler and failed two GxP audit tests unrelated to logging. Every *targeted* test passed. The
   fix that emerged — giving connector servers the process entrypoint they never had — is better
   than what I would have shipped, and picked up a missing `configure_telemetry()` too.
2. The "shipped defaults are not credentials" rule compared whole values only. `conftest.py`
   repoints `postgres_dsn` at an isolated schema, so under CI the DSN is not the default and is
   redacted — while the password inside it still is. Locally there is no Postgres, nothing
   repoints, and it passed. **CI is the arbiter; a local green is not the gate**, and this is what
   that sentence means concretely.

**Measurement discipline, stated because two of three probes measured nothing.** Sizing the safety
blowup took three attempts: the first repeated one SMILES (`dict.fromkeys` deduplicated it to two
molecules), the second grew molecule *length* with the index (so the O(n²) measured was my own
input). Only distinct strings at constant molecule size measure the code. The first two would each
have produced a confident, wrong write-up.

**What was deliberately not done, and why.** Twenty confirmed findings are in `BACKLOG.md` with
file:line, severity and analysis rather than half-fixed. The largest deferral is the framing lane's
four *coverage* findings: `frame_untrusted` wraps a prose string and each unframed source returns a
**structured model**, so covering them is a decision about which fields to wrap without corrupting
what the model reads. That is a design question, and rushing it into a mechanism PR would have
produced a worse answer than deferring it with the reasoning attached.

**The Q-A lane's honest negative result.** The ten `Protocol + InMemory + Postgres` triads are
*not* one abstraction waiting to be extracted — the audit chain's advisory lock and hash chain, the
retention semantics and the key shapes genuinely differ. What is shared is the connect/execute
plumbing (hand-rolled 14 times, five docstrings byte-identical), and the real prize is the
divergences the duplication hides: an `InMemoryStore.find` that raises on a timezone-aware row
where Postgres does not, and one of three jsonb writers rejecting non-finite floats. Recorded that
way rather than as "collapse the ten".

## Measured, and not defects

Recorded so they are not re-litigated:

- **The BO acquisition test's timeout headroom.** `test_the_suggestion_wires_the_assay_noise...`
  fails under load and passes alone in 27.8 s against its 60 s budget. Its own docstring predicts
  this and the marker exists to make a spike name itself. 2.2x headroom is thin but the test is
  behaving as designed; it is adjacent to the already-tracked "two slowest pKa tests fail on a
  loaded box" row, not a new finding.
- **The suite must not run concurrently with the agent fleet on a 4-core box.** It manufactures
  timeout failures indistinguishable from regressions. Every verification run in this session was
  serialized against agent work; one that was not had to be discarded and re-run.
