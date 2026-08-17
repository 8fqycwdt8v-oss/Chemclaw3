# Verdicts — agent governance slice, CORRECTNESS lens (reproduction pass)

Scope note: the findings file contains **one** finding at severity `high` and none at `critical`.
The other three are `medium`, `medium`, `low` and are out of scope. One verdict below.

Working-tree check: `src/chemclaw/agent/{authz,tool_authz,plan_gate,scratchpad}.py` are byte-identical
to the pristine `HEAD` copy at
`/tmp/claude-0/-home-user-Chemclaw3/41f2465f-44e8-5661-9ba7-5183da558c73/scratchpad/pristine`
(`diff -q` silent for all four), so nothing below is an artefact of another agent's mutation.

---

## The dry-run gate and the plan gate both read the raw `file_path`, but the tool writes the *normalized* one

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

### What I did

I did not run the reporter's script. I wrote two of my own (`/tmp/repro_a.py`, `/tmp/repro_b.py`)
from the source, adding spellings the reporter did not test.

**(1) The predicates, on six spellings, dry run active** — `uv run python /tmp/repro_a.py`:

```
'/memories/note.md'             durable=True  side_effecting=True  dry_run=REFUSED plan_gate_governs=True
'memories/note.md'              durable=False side_effecting=False dry_run=ALLOWED plan_gate_governs=False
'/./memories/note.md'           durable=False side_effecting=False dry_run=ALLOWED plan_gate_governs=False
'//memories/note.md'            durable=False side_effecting=False dry_run=ALLOWED plan_gate_governs=False
'/memories/../memories/note.md' durable=True  side_effecting=True  dry_run=REFUSED plan_gate_governs=True
'/memories'                     durable=False side_effecting=False dry_run=ALLOWED plan_gate_governs=False
```

**(2) What upstream does with the same strings** (`deepagents 0.7.6`,
`deepagents.backends.utils.validate_path` + `deepagents.middleware.filesystem._check_fs_permission`
against this repo's real `filesystem_permissions()`):

```
'memories/note.md'              -> validated '/memories/note.md'    permission=allow
'/./memories/note.md'           -> validated '/memories/note.md'    permission=allow
'//memories/note.md'            -> validated '//memories/note.md'   permission=allow
'/memories/../memories/note.md' -> ValueError: Path traversal not allowed
'/memories'                     -> validated '/memories'            permission=deny
```

**(3) Where the write lands** — real `scratchpad_backend()` over a real `CompositeBackend` +
`StoreBackend`, actor `audit-actor`:

```
routes: ['/memories/']
raw 'memories/note.md'    -> validated '/memories/note.md'  -> WriteResult(error=None, path='/memories/note.md')
raw '/./memories/note2.md'-> validated '/memories/note2.md' -> WriteResult(error=None, path='/memories/note2.md')
store namespace ('memories', '7709c4c003e680fa') -> [(('memories','7709c4c003e680fa'), '/note.md'),
                                                     (('memories','7709c4c003e680fa'), '/note2.md')]
```

**(4) The real middleware objects, not the bare predicates** — `uv run python /tmp/repro_b.py`
drives `tool_authz.refuse_writes_on_dry_run.awrap_tool_call` and
`plan_gate.enforce_plan_approval.awrap_tool_call` with a handler that reports whether the body ran,
against a session id with an unapproved todo list:

```
=== dry-run middleware ===
/memories/note.md        -> REFUSED: DryRunRefusal: DRY RUN — write_file changes stored data …
memories/note.md         -> TOOL BODY RAN for memories/note.md
/./memories/note.md      -> TOOL BODY RAN for /./memories/note.md

=== plan gate middleware (session with an unapproved plan) ===
/memories/note.md        -> REFUSED: PlanNotApprovedError: write_file changes stored data …
memories/note.md         -> TOOL BODY RAN for memories/note.md
/./memories/note.md      -> TOOL BODY RAN for /./memories/note.md
```

**(5) Test coverage.** `grep -rn "writes_durable_memory\|side_effecting_call" tests/` →
`tests/test_upstream_surface.py:420` (parameter *name* only) and `tests/test_scratchpad.py:161-197`,
which assert only `MEMORY_ROOT`-prefixed, `SCRATCH_ROOT`-prefixed and unreadable arguments. No test
exercises a non-canonical spelling. The reporter's claim about coverage holds.

**(6) The precondition.** `grep -rn "agent_memory_enabled" src/ infra/ .env.example` →
`core/config/agent.py:101: agent_memory_enabled: bool = False`, gated again at
`api/runner.py:631` (`not settings.agent_memory_enabled or settings.session_store != "postgres"`
→ `None`), and `.env.example:309` ships `false`. No Helm value sets it. `memory_store()` has
exactly one caller. With `store=None`, `scratchpad_backend` adds no `/memories/` route at all
(`scratchpad.py:197-202`), so the misclassified write resolves to `StateBackend` — the same place
the *correctly* classified scratchpad write goes.

### Why

**The mechanism is real and reproduces exactly, on my own scaffolding.** Line numbers and symbols
are current: `authz.py:198` is `writes_durable_memory`, `:234` is `side_effecting_call`,
`tool_authz.py:95` is `dry_run_refusal`, `plan_gate.py:115` is `gated_call`. The single line that
decides everything is `authz.py:231`, `return path.startswith(MEMORY_ROOT)`, applied to the model's
raw string, while `async_write_file` (`deepagents/middleware/filesystem.py:2042`) applies
`validate_path` *before* `awrite`. Both gates let the tool body run on both non-canonical spellings,
and the write really does land in the store namespace. The docstring at `authz.py:213-215` claiming
the gate cannot be "bypassable by malformed input" is true only for arguments it cannot read; it is
false for a readable argument spelled differently, exactly as the finding says. I found no upstream
guard that recovers the distinction — the permission rules are evaluated on the *validated* path and
return `allow`.

**What does not hold is the headline consequence as stated.** "So a dry run writes durable memory,
and an unapproved plan writes durable memory" is false in every configuration this repository ships.
The reporter discloses this two paragraphs later, which is honest, but the `high` label is attached
to the headline rather than to the disclosure. Three things bound the blast radius:

1. **Unreachable today.** `agent_memory_enabled` defaults `False` and is `false` in `.env.example`;
   there is no second path to a store. Today both spellings write turn-local graph state, which is
   what the gate is *supposed* to allow — so the observable behaviour is currently correct by
   accident, and the defect is latent.
2. **Blast radius when enabled is one namespace.** The only thing this bypass reaches is
   `write_file`/`edit_file` under `/memories/`, per-actor and erasable. Every other state-changing
   call — job launches, `propose_knowledge_note`, connector `state_changing` tools, template
   launchers — is gated by *name* through `side_effecting_tools()` and is unaffected by any path
   spelling. `delete` is not registered at all (`scratchpad_tools()` withheld it, verified:
   `('edit_file','glob','grep','ls','read_file','write_file')`), so the bypass cannot remove an
   existing memory, only add one.
3. **The gate fails closed on the dangerous direction.** `/memories/../memories/note.md` is
   classified `durable=True` and then rejected by `validate_path` anyway; `//memories/note.md`
   escapes the gate but also escapes the route (it validates to `//memories/note.md`, which
   `CompositeBackend` does not match to the `/memories/` route), so it is ungated *and* non-durable.
   The exploitable set is exactly the spellings `os.path.normpath` collapses onto `/memories/…`
   — relative, `./`, `/./` — none of which reach anything the canonical spelling does not.

I also confirmed the finding's own secondary case rather than taking it on trust:
`_check_fs_permission(filesystem_permissions(), "write", "/memories")` returns `deny`, so
`file_path="/memories"` is blocked by the permission rules and is latent, as claimed.

So: real defect, correct fix direction (normalize with the same function the tool uses, before
deciding), worth doing and cheap. But a gate whose bypass is unreachable in every shipped
configuration and whose enabled-state blast radius is "add a file to your own memory namespace
during a dry run" is a **medium**, not a high. Nothing about the `high` rating survives contact with
`agent_memory_enabled: bool = False`.
