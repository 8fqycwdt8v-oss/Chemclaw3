# Verification: sweep-supply-chain (lens: does it actually reproduce?)

Scope note: the findings file contains **one** finding marked critical or high — the `langsmith`
one. Everything else in it is medium or low and is out of scope for this pass and was not verified.

Working tree checked first: `git status --porcelain` was empty at `0da9f3d`, so nothing below is an
artifact of another agent's mutation experiment. No diff against the pristine copy was needed.

---

## `langsmith` — the egress control imports a package nothing declares, and pyproject asserts the opposite

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

### What I did

I did not run the reporter's `/tmp/no_langsmith.py`. I wrote my own blocker
(`/tmp/mine_nolangsmith.py`) — a `MetaPathFinder` inserted at `sys.meta_path[0]` that raises
`ModuleNotFoundError` for `langsmith` and any submodule, plus a purge of already-loaded
`langsmith*` entries — and imported the real modules.

Every load-bearing fact in the finding reproduces:

```
$ grep -n "langsmith" src/chemclaw/core/egress.py
36:import langsmith
71:    langsmith.configure(enabled=False)
$ grep -n "egress\|pin_langsmith" src/chemclaw/core/config/__init__.py
78:from chemclaw.core.egress import pin_langsmith_egress
338:pin_langsmith_egress(allowed=settings.langsmith_tracing_allowed)
```

Line numbers and symbols are real and current. The import at `egress.py:36` is unguarded — no
`try/except ImportError`, no lazy import inside the function — and `config/__init__.py:338` calls
it at module scope, not inside a factory.

Declaration check, parsed rather than grepped:

```
$ uv run python -c "...tomllib..."
39                      # len(project.dependencies)
[]                      # dependencies mentioning langsmith
optional: None
groups: ['dev'] -> []   # dev group mentioning langsmith
```

So `langsmith` is declared nowhere. Confirmed.

My own reproduction:

```
$ uv run python /tmp/mine_nolangsmith.py chemclaw.core.config
IMPORT FAILED: chemclaw.core.config: ModuleNotFoundError: No module named 'langsmith'
rc=1
```

The false comment is real. `pyproject.toml:23`, inside the `deepagents` block, states
"`langsmith` that nothing here imports" while `src/chemclaw/core/egress.py:36` imports it. That
clause is simply wrong.

The version claim is real:

```
$ uv run --with "langsmith==0.3.45" --no-project python -c "import langsmith; print(hasattr(langsmith,'configure'))"
0.3.45 configure: False
```

Reverse dependencies computed from `uv.lock` myself (not from the finding's transcript):

```
runtime dep of deepagents 0.7.6
runtime dep of langchain-core 1.5.5
```

— nothing first-party, as claimed.

### What does **not** hold — why OVERSTATED rather than CONFIRMED

**1. The trigger is not reachable from anything this repository can do.** The finding's trigger is
"any resolution in which `langsmith` is not in the closure", and asserts "nothing in
`pyproject.toml` prevents `uv lock --upgrade` from producing that closure". Both current suppliers
require it *unconditionally* — not via an extra, not behind a marker:

```
--- langchain-core==1.5.5
    langsmith<1.0.0,>=0.3.45
--- deepagents==0.7.6
    langsmith>=0.10.9
```

No `uv lock --upgrade` against today's index can drop it. Producing that closure requires an
upstream metadata change, not a resolver run.

**2. Under the finding's own hypothetical, `langsmith` is still required by code chemclaw imports.**
This is the part that most weakens the consequence. `langchain_core` itself does not survive
without it:

```
$ uv run python /tmp/mine_nolangsmith.py langchain_core
IMPORT OK
$ uv run python /tmp/mine_nolangsmith.py chemclaw.api.app
IMPORT FAILED: chemclaw.api.app: ImportError: module ''langchain_core.runnables'.'base'' not found
                                             (No module named 'langsmith')
```

Note the failure of the front door is **not** the one the finding attributes to `egress.py` — it
is `langchain_core.runnables.base`. So for `langchain-core` to make `langsmith` an extra (the
finding's stated trigger) it would have to change its *code* as well as its metadata, and the
uncaught defect would then be "chemclaw imports a package that fell out of the closure", of which
`egress.py` is one site among several rather than the singular startup killer. "Every entrypoint of
the service fails to start" is the right sentence about a wrong closure; it is not a description of
a defect specific to `egress.py`.

**3. The floor / `AttributeError` half is effectively unreachable, and there is a control the
finding missed.** The finding hedges it as "on a closure where `deepagents` is not present" —
but `deepagents>=0.7.5,<0.8` is a *declared, capped* first-party dependency, so it is present by
construction and floors `langsmith>=0.10.9`. On top of that, uv's default resolution takes the
highest compatible version, so 0.3.45 is not what a resolve produces even absent the floor. And the
symbol the finding wants ratcheted is already exercised: `tests/test_egress.py:91` calls
`pin_langsmith_egress(allowed=False)` directly, so a `langsmith` without `configure` turns
`make test` red rather than failing at startup.

```
$ uv run python -m pytest tests/test_egress.py -q
2 passed in 0.25s
```

The finding is right that `tests/test_upstream_surface.py` pins nothing from `langsmith`; it is
wrong that the symbol is unguarded, which is the substance of what that ratchet would buy.

**4. Nothing reaches production unattended.** `uv.lock` pins `langsmith==0.10.17` and the image
builds `uv sync --frozen`; a changed closure only arrives through a deliberate `uv lock --upgrade`
and its diff.

### Why

The mechanism is exactly as described and reproduces on my own scaffolding: an undeclared,
unfloored third-party import sitting at module scope on the one import path every entrypoint takes,
with a comment in `pyproject.toml` asserting the opposite. That is a genuine hygiene defect and the
one-line fix the finding proposes is correct and worth taking.

What does not survive is **high**. Severity here is the probability-weighted claim, and every path
to the stated consequence needs an upstream to change both its metadata and its code; the
`AttributeError` variant additionally needs a declared, capped dependency to vanish and is caught by
an existing test if it ever happened. Today the closure is pinned, the control works (I did not
re-verify the mechanism itself — the finding concedes it works and I found nothing suggesting
otherwise), and no observable failure exists. That is a medium: fix the declaration and delete the
false comment, but do not treat it as a startup-crash risk.
