# Verdicts — sweep-supply-chain, reachability lens

Scope: only findings marked **critical** or **high**. The file contains exactly one — the `langsmith`
finding. `tblite`, `starlette`, `openpyxl`, `torch/botorch/linear_operator`, `scipy`, CI tag pinning
and `httpx2` are all medium/low and are out of scope; no verdict is given for them.

Working tree checked against `HEAD` before starting: `git status --porcelain` shows only an untracked
verdicts file from another agent, and `src/chemclaw/core/egress.py` is byte-identical to the pristine
copy. Nothing I read was mutated.

---

## `langsmith` — the egress control imports a package nothing declares, and pyproject asserts the opposite

- **Verdict**: OVERSTATED
- **Severity I would assign**: low

### What I did

**1. Granted and reproduced the mechanism.** `src/chemclaw/core/egress.py:36` is a bare
`import langsmith` at module scope; `core/config/__init__.py:78` imports it and line 338 calls
`pin_langsmith_egress(...)` at module scope. Blocking the module with a `MetaPathFinder`:

```
$ uv run python /tmp/no_ls.py
STARTUP FAILED: ModuleNotFoundError : No module named 'langsmith'
```

So the mechanism is exactly as described. Both attacks below are on the trigger and the consequence.

**2. Trigger — can any resolution actually drop `langsmith`?** Requirement metadata of the two
suppliers, read from the installed distributions:

```
langchain-core==1.5.5 -> langsmith<1.0.0,>=0.3.45   marker=None  extras_gate=False
deepagents==0.7.6     -> langsmith>=0.10.9          marker=None  extras_gate=False
```

Both unconditional — no extra, no marker. Both suppliers are *declared* first-party dependencies
(`pyproject.toml`: `langchain-core>=1.5.3`, `deepagents>=0.7.5,<0.8`), and `langchain-core` is itself
capped `<2.0.0` by eight packages in the closure (`langchain`, `langgraph`, `langgraph-sdk`,
`langchain-anthropic`, `langchain-openai`, `langchain-mcp-adapters`, `langchain-google-genai`,
`deepagents`). So the resolver may only move `langchain-core` inside 1.x and `deepagents` inside 0.7.x,
and every already-published release in both ranges has immutable metadata requiring `langsmith`.

I then ran the finding's own trigger against live PyPI, in a scratch copy of `pyproject.toml`+`uv.lock`:

```
$ cd /tmp/lockexp && uv lock --upgrade
Updated ... xxhash v3.8.1 -> v4.0.0
$ grep -A2 '^name = "langsmith"' uv.lock
name = "langsmith"
version = "0.11.0"
```

The exact command the finding names as the trigger, run today, resolves `langsmith` **0.11.0** — still
present, and nine minor versions above the `>=0.6` floor the finding says is missing. The claim
"nothing in `pyproject.toml` prevents `uv lock --upgrade` from producing that closure" is not true of
any closure that exists; it is true only of a hypothetical future release in which two independently
maintained packages both drop a hard requirement on LangChain's own telemetry client.

**3. Trigger — the `AttributeError` sub-claim.** The finding says the resolver "may legally pick a
langsmith on which `pin_langsmith_egress` raises `AttributeError`", on the premise of "a closure where
`deepagents` is not present". `deepagents` is a declared dependency pinned `>=0.7.5,<0.8`; it is not
absent by any resolver outcome, only by someone editing `pyproject.toml` — at which point they are
already editing the file the fix goes in. And uv resolves *highest* by default (confirmed by the
`--upgrade` run above, which moved every package up); no `--resolution lowest` appears anywhere in
`pyproject.toml`, the Makefile or `.github/workflows/`. So even with `deepagents` deleted, a fresh
resolve takes langsmith 0.11.0, not 0.3.45. Reaching 0.3.45 requires an explicit downgrade pin that
nobody has a reason to write.

**4. Consequence — "every entrypoint of the service fails to start".** Production and CI do not
resolve at all. `deploy/Containerfile:101` is `uv sync --frozen --no-dev`; `.github/workflows/ci.yml:90`
is `uv sync --locked`. Both install the committed lockfile, which pins `langsmith==0.10.17` with
hashes. The installed closure of a real deployment is not resolver output — it is the lock, and a lock
that lost `langsmith` would have to be committed by a human.

And such a commit cannot reach a deployment quietly. With `langsmith` blocked, `pytest` does not fail
a test — it fails to *collect*:

```
$ PYTHONPATH=/tmp/blk uv run pytest -p blockls tests/test_upstream_surface.py tests/test_decision_log.py -q
  File "/tmp/blk/blockls.py", line 5, in find_spec
    raise ModuleNotFoundError(f"No module named 'langsmith'")
ModuleNotFoundError: No module named 'langsmith'
```

conftest imports `chemclaw.core.config`, so the whole suite dies before the first test runs. The
failure mode is "CI is red on the commit that changes the lockfile", not "the service fails to start".
That is the loudest possible signal at the earliest possible point.

**5. The "pins nothing from langsmith" claim.** `tests/test_egress.py:129-131` does
`import langsmith` and calls `langsmith.configure(enabled=None)` in cleanup. A langsmith without
`configure` fails that test with `AttributeError` in CI. It is a weaker ratchet than an explicit
assertion in `test_upstream_surface.py` and I agree the assertion would be better — but "it currently
pins nothing from `langsmith`" overstates the gap.

### Why

What is true: `langsmith` is a third-party module imported at module scope of the one module every
entrypoint imports, and it is not in `[project.dependencies]`. The comment at `pyproject.toml:23`
("`langsmith` that nothing here imports") is factually wrong, and a false comment in the file where
dependency decisions are made is worth correcting. The one-line fix the finding proposes is correct
and cheap.

What is not true is the severity argument built on top of it. Every load-bearing step fails:

- The trigger does not exist in any resolvable closure — measured by running the finding's own
  `uv lock --upgrade` against live PyPI, which keeps `langsmith` and moves it to 0.11.0.
- Both suppliers are declared direct dependencies with hard, unmarked, non-extra requirements, one of
  them (`deepagents<0.8`) frozen to a range of already-published releases whose metadata cannot change.
- The `AttributeError`-on-0.3.45 scenario needs a first-party deletion of `deepagents` *and* a
  non-default lowest-version resolution strategy that is configured nowhere.
- The stated consequence ("every entrypoint fails to start") is a paraphrase of a CI collection error.
  Deployments install a hashed lockfile, and the transition from that lockfile to one lacking
  `langsmith` cannot pass CI.

Note also the internal inconsistency with the rest of the sweep: `langsmith` is the *best*-constrained
of the undeclared imports the same file lists — floored `>=0.10.9` and capped `<1.0.0` transitively —
while `starlette` (`next-major 2.0.0 blocked by: NOTHING`) and `openpyxl` (bare unversioned
requirement, no floor and no ceiling) are genuinely unbounded and are rated *medium*. If those are
medium, this one cannot be high.

I would file it as a low-severity hygiene item: declare `langsmith>=0.10.9`, delete the false clause
from the `deepagents` comment, and add `langsmith.configure` to `tests/test_upstream_surface.py`.
Same fix, one third the alarm.
