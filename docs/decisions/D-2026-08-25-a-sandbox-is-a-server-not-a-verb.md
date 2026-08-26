# D-2026-08-25-a-sandbox-is-a-server-not-a-verb — the agent gets Python, and `execute` stays withheld

**Status:** accepted · **Date:** 2026-08-25 · Does not supersede anything. It *completes*
`agent/scratchpad.py`'s declination of deepagents' `execute` verb by giving the capability that
declination was blocking a different home, and leaves the declination itself in force.

## Context

`scratchpad_tools()` hands a turn six filesystem verbs and withholds two. The `delete` argument is
`D-2026-08-12`'s. The `execute` argument is:

> deepagents 0.7 ships exactly one concrete sandbox (`LangSmithSandbox`), which this repository
> declines on content-egress grounds, and `LocalShellBackend` is documented as unrestricted.

Both halves are true, and the refusal is right. **But it is a refusal of two specific sandboxes, and
it was being read as a decision about execution.** The 2026-08-25 field benchmark measured what that
reading costs: numpy 2.4.6, pandas 3.0.3, scipy 1.17.1 and RDKit 2026.3.5 are installed *in the
process the agent runs in* — `science/fingerprints`, `science/calc` and `science/bo` import them —
and the agent can reach none of them. It cannot canonicalise an unexpected SMILES, fit a kinetics
curve, re-parse an output file whose format surprised it, or aggregate a table a tool just returned.
Every comparable chemistry agent executes: ChemCrow, Coscientist, El Agente, OpenClaw.

The abstract question — "should the agent be able to execute code" — is not answerable, which is why
it sat open. The answerable question is **where**.

## Decision

**The capability is an MCP server in `Chemclaw3-mcp`, and the `execute` verb stays withheld.**

`servers/pyexec`, port 8899, one tool: `run_python(code, data)`. A short program runs in a
disposable child process with numpy, pandas, scipy and RDKit importable; `data` goes in as a JSON
dict and whatever the program assigns to `result` comes back. No session, no persisted namespace,
no file that outlives the call.

That repository is the right home for a reason and not for convenience. It already enforces
no-egress at four independent layers — a runtime `socket.connect` guard armed on import, an AST scan
per server, the whole suite run with the guard armed, and a default-deny `NetworkPolicy` asserted in
both directions — and `make offline-run` proves it by *removing the network* and checking every
answer is unchanged. Nothing reachable from inside this repository is close to that posture, and all
of it was already built.

**The consequence that makes this a good seam rather than a relocation: Chemclaw3 needs no code
change.** D-118 and `D-2026-08-09-a-connector-we-do-not-run` made the address the whole knob.
Verified rather than asserted — with the server running and `CHEMCLAW_CONNECTORS_DIR` pointed at the
fleet's manifests, `run_python` appears on the agent surface and answers, and `git diff src/` is
empty.

## Half the controls are the boundary and half are not, and that division is the design

Stating it is part of the decision. A sandbox's characteristic failure is a reviewer believing a
stronger claim than it can support.

**Defence in depth — real, and porous by construction.** A guarded `__import__` in the analysis
namespace allowlisting about thirty modules; `open`, `eval`, `exec`, `compile`, `input`,
`breakpoint`, `exit` and `quit` withheld from builtins; `socket.connect`, `connect_ex` and
`create_connection` replaced with a refusal. These make the sandbox's shape obvious to an honest
caller and raise the cost for a dishonest one. **They are not a wall** — `numpy` is reachable, the
attribute graph hanging off a live library is large, and a Python-level sandbox is a research area
rather than a solved problem. Calling this half the boundary would be the `map_to_hpc_identity`
shape this repository deletes code over: a control that exists in order to be pointed at.

**The boundary, which holds granting a complete escape from the half above.** A separate disposable
process, never an in-process `exec`. Killed by *process group* on a wall clock, so a run that spawns
children dies whole — the `calc` server's `run_isolated` lesson, where a naive timeout killed one
pid and left the rest burning CPU. `RLIMIT_CPU`, `AS`, `FSIZE`, `NOFILE` and `NPROC` set **soft and
hard**, because a soft limit alone is raised back by the program being limited in three lines. An
environment **built from a five-name allowlist rather than filtered**, so this pod's own bearer
token cannot reach the child by being a variable nobody thought to delete. `python -I -B` in a temp
working directory deleted on return. A rootless image. An empty `egress:` — not even DNS.

## Classification: `read_only`, deliberately

The tool writes nothing, persists nothing, and has no effect outside a directory deleted before it
returns. What the classification actually decides is whether the agent may call it *while building
the plan a human is asked to approve* (D-167). "Which of these two routes has the lower E-factor" is
a question that has to be answerable before somebody signs off on one of them, not after.

## Two designs were built, measured and thrown away

Both are recorded in `servers/pyexec/README.md` and in the module docstrings, because neither
reason is guessable from the result.

**The import guard was a `sys.modules` purge plus a `sys.meta_path` finder — the obvious
construction, and it fails in both directions at once.** It breaks the libraries: `scipy.optimize`
imports `sys` lazily *at call time*, so a guard refusing `sys` turned `brentq` into a
`SandboxImportError`. And it does not hold: `import` consults `sys.modules` before any finder, so
the first library to re-import `os` repopulates the cache and a caller's `import os` never reaches
the guard. Replacing `__import__` in the analysis namespace's builtins separates caller from library
exactly — a caller's `import` resolves it from the mapping its frame was given, a library's from the
untouched `builtins` module.

**Warming the scientific stack cost more than it saved.** An earlier version imported numpy, pandas,
four `scipy` submodules and RDKit before handing over, so the caller's CPU budget would not be spent
on our imports. Measured: an empty child is **11 ms** and the warm-up cost **1.2–1.9 s on every
call** — `scipy.stats` alone is 1.6 s — paid in full by an analysis needing only `math`. The lazy
import problem it was really protecting against is the guard's job instead.

## Consequences

- **`agent/scratchpad.py` is unchanged and its docstring stays true.** No `execute`, no `delete`.
  The sentence about deepagents' two sandboxes remains the reason, and it remains correct.
- **One narrow change to the fleet's shared kit.** `runner.py` is the only file there that imports
  `socket` — in the child, to disable it — so `no_egress.py` grew an `exempt` parameter rather than
  the runner evading an AST scan. The exemption is paid for: a test parses that file and asserts
  every attribute it touches on the socket module is an assignment target, and that the three are
  exactly the outbound calls.
- **A tool whose input is a program is new for this system**, and prompt injection reaching the
  model reaches it. That is why the boundary is the process and the deployment, and why the porous
  half is written down as porous.
- **What this is not** bounds what may be added later without a new ADR: not a shell, not a
  notebook (no state survives a call), not a file tool (`open` is not in builtins), and not a data
  source. Reversing any of those is a decision, not a configuration change.

## Verification

`make check` in `Chemclaw3-mcp`: 985 passed, ruff clean, `mypy --strict` clean. `make offline-run`:
985 passed with the network removed. 47 of those tests are this server's and most assert refusals —
a fork bomb, a program that outlives its CPU signal, a memory bomb, `open`, `__import__('os')`, and
a socket reference reached through an allowed module. From this repository: the manifest resolves,
the session opens, and a live call returns while `git diff src/` stays empty.
