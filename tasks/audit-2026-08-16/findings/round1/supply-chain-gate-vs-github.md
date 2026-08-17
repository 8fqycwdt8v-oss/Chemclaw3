# The supply-chain gate reports clean while GitHub reports three vulnerabilities

## The repo's own gate and GitHub's advisory database disagree, and nothing would ever surface it

- **Severity**: medium — **and unresolved**: the part that would settle it is not readable from this
  session. See "What I could not settle" before acting on this.
- **Location**: `Makefile:deps-audit`, run by `.github/workflows/ci.yml` and `image.yml`

### Trigger

Push to the repository. GitHub responds on every push:

```
remote: GitHub found 3 vulnerabilities on 8fqycwdt8v-oss/Chemclaw3's default branch
remote: (1 high, 2 moderate).
```

Meanwhile the gate that exists to answer exactly this question says:

```
$ make deps-audit
Resolved 247 packages in 3ms
No known vulnerabilities found
```

### What I measured

Every hypothesis I could test from here, and what each returned:

| Hypothesis | Test | Result |
|---|---|---|
| The gate skips dev dependencies | `uv export` (246 pkgs) vs `--no-dev` (213) — audited the full set | **clean** |
| The gate audits fewer packages than the lockfile holds | 247 names in `uv.lock` vs 246 in the export | only `chemclaw` itself differs (the editable local package) |
| `pip-audit`'s default PyPI feed lags | re-ran the full set against **OSV** (`-s osv`) | **clean** |
| The alerts are about a non-Python manifest | searched for `package.json`, `go.mod`, `Gemfile` outside `.venv` | none tracked |
| My branch's lockfile differs from `main` | `git diff origin/main -- uv.lock pyproject.toml` | identical |
| A `dependabot.yml` narrows or widens the scan | `.github/dependabot.yml` | does not exist |

So the gate is not misconfigured in any way I can detect, and it covers the whole closure. Two
independent advisory databases — PyPI's and OSV's — both report nothing across all 246 packages.

### What I could not settle

**I cannot read the Dependabot alerts from this session.** There is no `gh` CLI here and no MCP tool
exposing Dependabot alerts, so I cannot see which three packages GitHub is flagging, which advisories,
or whether they are current.

Three explanations remain open, and they have different consequences:

1. **GHSA carries entries the PyPI and OSV feeds do not.** GitHub's Advisory Database is curated
   separately and is a superset for some ecosystems. If so, the gate is working as designed and is
   simply blind to a class of advisory — which is a real gap in a control the CI treats as blocking.
2. **The alerts are stale** — raised before a bump and never dismissed. Then the gate is right and
   GitHub is noise, and the fix is to dismiss them.
3. **Dependabot parses `uv.lock` directly** and resolves something the export does not reproduce.

### Consequence

Whichever it is, the same thing is true today: **the repository ships a blocking supply-chain gate
whose answer disagrees with the one GitHub puts in front of every developer on every push, and
nothing reconciles them.** A green `make deps-audit` is currently evidence about the PyPI/OSV feeds,
not about the project's known-vulnerability status — and the CI comment above the target
("a finding is a red build rather than a report nobody opens") describes a stronger property than
the target delivers.

### Fix

Do not change the gate before reading the alerts — the right change depends on which explanation
holds, and two of the three call for no code change at all.

1. Open the Security tab (`https://github.com/8fqycwdt8v-oss/Chemclaw3/security/dependabot`) and
   record the three advisories: package, version, GHSA id, status.
2. Check each against `uv.lock`. If they are current and real, the gate needs a second source —
   `pip-audit` accepts `--service osv` today and `osv-scanner` reads `uv.lock` natively; either
   closes the class.
3. If they are stale, dismiss them, and add the reconciliation to the gate so the two cannot drift
   silently again.

### Why this is filed rather than fixed

Every other finding in this audit was settled by running something. This one has a measurable half
and an unmeasurable half, and the honest report says which is which. Guessing at the three
advisories and "fixing" the gate against a guess would produce exactly the shape this audit keeps
finding elsewhere: a control that looks stronger than it is.
