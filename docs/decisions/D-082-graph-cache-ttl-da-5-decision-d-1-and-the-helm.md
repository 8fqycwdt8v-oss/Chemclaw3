# D-082 — Graph-cache TTL (DA-5 / decision D-1) and the Helm render gate (DA-10 / decision D-2)

**Context.** `docs/audit/12-deep-analysis.md` left two findings explicitly unresolved because each
needed a judgement call rather than an engineering one. Both were signed off; this records what was
built and, more importantly, what was traded.

### D-1 — Graph freshness vs. interactive latency (DA-5)

**The problem.** The note cache is keyed by a stat fingerprint of the note tree. The fingerprint is
cheap *per file* but O(notes) in total, and it is computed on **every** query — including a pure
cache hit. After DA-3 removed the reassembly cost, that scan *is* the floor on interactive latency:
~75 ms at 10k notes on local disk in the audit, and materially worse on the networked OpenShift PVC
production actually reads.

**Decision.** Add `graph_cache_ttl_seconds` (default **5.0**): within the window the last scan is
trusted and skipped entirely. Measured effect on a warm query at 10k notes: **164 ms → 0.52 ms**.

**What this costs, stated plainly.** A note changed by something *outside* this process — another
pod, an out-of-band `git pull` — can remain invisible for up to the window. That is a real change to
freshness semantics, and it takes effect on upgrade. Two things bound it:

- **Local writes never wait.** `kg.graph.invalidate_cache()` is the explicit bust hook, and the
  PR-gate submitter calls it after writing a note. The authoring loop — the one place a human
  *expects* their own change to appear at once — is unaffected. (It is also required for
  correctness there: the submitter's `checkout -B`/`reset --hard` rewrite the tree wholesale, so a
  cached graph could otherwise describe a tree that no longer exists.)
- **`0` restores the old behavior exactly** — scan every query — for any deployment where no
  staleness is acceptable. This is the setting to choose if the GxP posture demands it.

**Why a TTL and not an invalidation signal.** A merge hook or `inotify` avoids staleness entirely,
but only catches changes through the paths it hooks; an out-of-band `git pull` still slips past, so
it buys complexity without closing the hole. The TTL bounds *every* path uniformly.

**Honest note on blast radius.** Two existing tests had to pin `graph_cache_ttl_seconds = 0`,
because they assert fingerprint-based busting and that needs the scan to run. That is the change
being visible where it should be — not test churn to be papered over.

### D-2 — Buying down live-edge risk offline (DA-10)

**Decision.** Do the cheapest, highest-probability item now and defer the rest, as recommended:

1. **`make helm-validate` in CI** — `helm template` piped to `kubeconform -strict` against the
   Kubernetes schemas (plus the CRD catalog, for the OpenShift `Route`). The chart is the one
   artifact no test exercises; a broken chart is discovered at `helm install`, in production, on
   the worst day. No cluster needed.
2. **`tests/test_helm_chart.py` — the gap a schema check cannot see.** kubeconform validates
   *Kubernetes* shape; it has no idea whether `CHEMCLAW_FOO` is a real setting. Two failure modes
   live in that gap and both were unguarded:
   - *A key that is not a field.* pydantic-settings **tolerates** an unknown prefixed environment
     variable — unlike an unknown key in a `.env` file, which is precisely what broke the
     quickstart in DA-1. So the operator gets no error and no effect: a setting they believe they
     enabled is silently ignored. In a GxP deployment that is worse than a crash.
   - *A malformed value on a real field.* This one does crash — at import, in every pod at once.

   Both are now caught offline against the same `Settings` the pods construct, and both were
   mutation-verified (inject each fault, watch the suite go red).

**Deferred, deliberately.** Entra/Nextflow contract tests against recorded responses wait for a
real tenant. Recorded-response tests written against a *guess* at the response shape mostly assert
one's own assumptions back; they would buy confidence, not correctness.

**Finding surfaced while doing this.** `CHEMCLAW_COMPONENT` is set on every Deployment but is not a
`Settings` field and nothing in the app reads it. It is harmless (unknown prefixed env vars are
ignored) and plausibly useful to an operator reading `kubectl describe`, so it is allow-listed by
name in the parity test rather than deleted or the check loosened — any *other* non-field key is a
real finding.
