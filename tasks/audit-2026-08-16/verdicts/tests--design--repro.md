# Verification — `tests/` design findings, "does it actually reproduce?" lens

Scope: only findings marked **critical** or **high**. The file has exactly one such finding
(the Temporal `thread`-timeout hook, severity high); everything else is medium or low and is
out of scope, not judged.

---

## The Temporal `thread`-timeout hook selects on an incidental attribute and misses two Temporal modules

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

### What I did

**1. Re-derived the selection criterion from source, not from the reporter's plugin.**
`tests/conftest.py:216` `pytest_collection_modifyitems` is current and reads exactly as quoted:

```
241	    for item in items:
242	        module = getattr(item, "module", None)
243	        if module is not None and hasattr(module, "start_env_or_skip"):
244	            item.add_marker(pytest.mark.timeout(method="thread"))
```

`grep -rn start_env_or_skip tests/` gives 12 modules that call it. Ten import it at module
scope. Exactly three call sites import it *inside a function*:
`tests/test_template_job_step.py:173`, `tests/test_template_job_step.py:407`,
`tests/test_templates.py:474` — i.e. the two modules the finding names, and no others.
`tests/test_worker_shutdown.py` imports `Worker` at module scope but drives a `_FakeWorker`,
so it is not a fourth case.

**2. Wrote my own collection probe** (`/tmp/verif/showmarks.py`, a `pytest_collection_finish`
hookwrapper that prints each module's *closest* `timeout` marker after the repo's own hook has
run — deliberately not the reporter's script):

```
$ PYTHONPATH=/tmp/verif uv run pytest -p showmarks --collect-only -q \
      tests/test_templates.py tests/test_template_job_step.py tests/test_qm_workflow.py \
      tests/test_orchestrator.py tests/test_bo_knowledge.py
PROBE tests/test_bo_knowledge.py        marker=((), {'method': 'thread'})  hasattr=True
PROBE tests/test_orchestrator.py        marker=((), {'method': 'thread'})  hasattr=True
PROBE tests/test_qm_workflow.py         marker=((), {'method': 'thread'})  hasattr=True
PROBE tests/test_template_job_step.py   marker=None                       hasattr=False
PROBE tests/test_templates.py           marker=None                       hasattr=False
```

So the two modules collect with **no** `timeout` marker at all, leaving them on
`pyproject.toml:316`'s `timeout = 180` under pytest-timeout's default method. `timeout_method`
is never set anywhere in `pyproject.toml` (checked: `grep -n timeout pyproject.toml`), so the
default `signal` applies — the file's comment at :313 says so and the observed behaviour below
matches it.

`test_orchestrator.py`'s pass is by indentation, as claimed: its import sits at
`tests/test_orchestrator.py:31`, inside `with workflow.unsafe.imports_passed_through():` at
module scope, so it *is* a module attribute.

**3. Did not take the docstring's "signal cannot reach a Temporal hang" on trust — measured it.**
The finding leans on that claim (a docstring is a claim, not evidence), so I built the hang
myself: `/tmp/verif/hang/test_hang.py` starts a real `WorkflowEnvironment.start_time_skipping()`,
runs a `Worker` registered for one workflow, and executes a *different* workflow type on the
same queue — the exact "submitted to a queue whose worker had not registered it" shape. Two
identical tests, one marked `method="signal"`, one `method="thread"`.

```
$ time timeout 60 uv run pytest /tmp/verif/hang/test_hang.py -q -k signal
Terminated
real 1m0.028s            # 10 s cap never fired; killed by my outer bound
(an earlier run with a 120 s outer bound was also killed by the bound, not by the cap)

$ uv run pytest /tmp/verif/hang/test_hang.py -q -k thread
... +++++++++++ Timeout +++++++++++     # fired at ~10 s, dumped every thread's traceback
```

Control, to prove the `signal` method works at all in this container:

```
$ uv run pytest /tmp/verif/hang/test_ctrl.py -q     # time.sleep(60) under method="signal"
E       Failed: Timeout (>5.0s) from pytest-timeout
1 failed in 5.25s
```

So: `signal` caps a pure-Python block in 5.25 s and does **not** cap this Temporal block in
120 s; `thread` caps it in 10 s. The mechanism is real and measured, not inherited from prose.

**4. Checked the two uncovered modules really have that shape.** Both start a real
`WorkflowEnvironment`, register a real `Worker`, and `execute_workflow(TemplateWorkflow...)`
(`tests/test_template_job_step.py:185`/`:439`, `tests/test_templates.py:510`).

### Why

Every factual element of the finding reproduces independently: the line numbers and symbols are
current, the two named modules are exactly the set with function-scope imports, they genuinely
collect with no marker, and the consequence of that — a `signal` cap that never fires on a
Temporal block — is something I measured rather than read.

One thing the reporter missed that makes it worse: `tests/test_template_job_step.py:414-416`, in
one of the two *uncovered* modules, carries a comment explicitly reasoning about "the activity is
scheduled to a queue nobody polls and the run hangs" — the module's author already identified this
exact hang as a live risk in that file, and it is the file the hook does not cover.

Two mitigations argue against calling it critical rather than high: the outer backstop is now
`.github/workflows/ci.yml:49` `timeout-minutes: 30` (not the 6 h of the original incident), and
the gap is latent — it needs a second defect in `TemplateWorkflow` or its queue wiring to
manifest. That is what a control is for, so it does not reduce the finding below high, but it
bounds the blast radius to one 30-minute runner and a nameless red `main` rather than a day's
budget.

Nothing in the repo was mutated for this verification; all scaffolding lives under `/tmp/verif/`.
