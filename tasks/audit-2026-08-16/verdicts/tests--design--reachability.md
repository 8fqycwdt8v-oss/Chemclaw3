# Verdicts — `tests/` design findings, reachability lens

Scope: only findings marked **critical** or **high** in
`tasks/audit-2026-08-16/findings/round1/tests--design.md`. That file has **one**: the Temporal
`thread`-timeout hook. The other eight are medium/low and were not examined.

---

## The Temporal `thread`-timeout hook selects on an incidental attribute and misses two Temporal modules

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (as a test-infrastructure control; it has no product-behaviour
  consequence, so read "high" as "a control that silently does not fire", not as a defect a chemist
  could ever see)

### What I did

**1. Reproduced the missing marker.** Collection plugin printing the closest `timeout` marker per
module:

```
$ PYTHONPATH=/tmp/tmk uv run pytest -p showmark --collect-only -q \
      tests/test_templates.py tests/test_template_job_step.py \
      tests/test_qm_workflow.py tests/test_orchestrator.py
MARK tests.test_orchestrator     ((), {'method': 'thread'})
MARK tests.test_qm_workflow      ((), {'method': 'thread'})
MARK tests.test_template_job_step (None, None)
MARK tests.test_templates        (None, None)
```

Matches the finding exactly. `grep -rn "start_env_or_skip" tests/*.py` confirms the cause: 10 modules
import it at module scope, `tests/test_template_job_step.py:173`/`:407` and `tests/test_templates.py:474`
import it inside the test function, and `tests/test_orchestrator.py:31` is at module scope only
because it sits inside a `with workflow.unsafe.imports_passed_through():` block — the finding's
"passes by accident of indentation" is right.

**2. Confirmed the two missed modules really drive a real server and a real worker** (i.e. they do
not skip here):

```
$ uv run pytest tests/test_templates.py tests/test_template_job_step.py -q -rs
45 passed, 8 warnings in 24.00s     # zero skips — the time-skipping server starts in this sandbox
```

Both start `temporalio.worker.Worker` against `start_env_or_skip()` and `execute_workflow`
(`test_templates.py:509-527`, `test_template_job_step.py:185-205`, `:438-460`).

**3. Measured the consequence rather than taking the docstring's word for it.** Built the hang shape
the hook's docstring describes — a workflow submitted to a queue whose worker has not registered that
workflow type (`/tmp/hang/test_hang2.py`) — and ran it under both methods with the same cap.

`signal` (the method the two missed modules get), 15 s cap, killed externally at 200 s:

```
$ timeout 200 uv run pytest /tmp/hang/test_hang2.py -q --timeout=15 --timeout-method=signal
=== SIGNAL ===
Terminated
EXIT=143
```

Nothing printed. Repeated with a 10 s cap and a hard `SIGKILL` at 90 s: `EXIT=137`, log file **0
lines**. No test name, no traceback — the exact shape the docstring claims.

`thread`, same test, same 15 s cap:

```
$ timeout 100 uv run pytest /tmp/hang/test_hang2.py -q --timeout=15 --timeout-method=thread
  File "/tmp/hang/test_hang2.py", line 36, in test_hang_unregistered_workflow_type
    asyncio.run(_run())
  ...
+++++++++++++++++++++++++++++++++++ Timeout +++++++++++++++++++++++++++++++++++
EXIT=1
```

Fired at 15 s, named the test, dumped the stack. So the difference the hook exists for is real and I
measured it on this machine, not read it.

**4. Isolated what actually breaks `signal`.** A control run with *no worker at all*
(`/tmp/hang/test_hang.py` — workflow submitted to a queue nobody polls) **did** get capped by
`signal`, at 15.6 s with a full traceback. The only difference between that and the run that escaped
is the presence of a running `Worker`, i.e. the SDK core's own threads. So the at-risk population is
precisely "modules that start a Temporal `Worker`" — and both missed modules do.

### Why

Every load-bearing claim in the finding holds and I could not find anything upstream that prevents
it. There is no `pytestmark` in either module, no per-test `@pytest.mark.timeout(method=...)`, and
`_apply_timeout_scale` returns immediately when `PYTEST_TIMEOUT_SCALE` is unset, so nothing else
puts a `thread` method on those items. The trigger is not "call a private function" — it is ordinary
collection of two committed test files on every CI run.

Three things I would add or correct, none of which changes the verdict:

- **The finding repeats the docstring's stated mechanism, and that mechanism is wrong.** The
  docstring says the test is "blocked inside `temporalio`'s Rust core (PyO3)" and so never returns to
  run the SIGALRM handler. The stack the `thread` watchdog dumped shows the main thread in ordinary
  Python — `asyncio.base_events._run_once` → `selectors.EpollSelector.select` → `epoll.poll` — and the
  loop is demonstrably still turning (see next bullet). The conclusion is right; the explanation in
  `tests/conftest.py:217-239` is not, and per the rules of this audit that docstring was never
  evidence for it. I confirmed the conclusion independently.
- **One of the two modules is partly self-protected, which the finding does not mention.** Both
  Temporal tests in `test_template_job_step.py` wrap `execute_workflow` in
  `asyncio.wait_for(..., timeout=30)` plus a 30 s `execution_timeout`. I checked whether that guard
  survives this hang shape (`/tmp/hang/test_hang3.py`): it does — `FAILED … - TimeoutError`,
  `1 failed in 31.30s` under `--timeout-method=signal`. So that module fails in ~31 s with a name,
  not in 28 minutes. It still runs uncapped by the suite's own control, but its realistic worst case
  is much smaller than the finding implies.
- **The other module is worse than the finding says.** `tests/test_templates.py:474`'s Temporal test
  has **no** `asyncio.wait_for` and **no** `execution_timeout` — a bare
  `await client.execute_workflow(...)` (lines 509-527). It is the only Temporal test in the repo I
  found with neither an in-test guard nor the `thread` marker, so a regression in `TemplateWorkflow`
  or in activity registration hangs it until the job-level `timeout-minutes: 30`
  (`.github/workflows/ci.yml:49`) kills the run with no test name.

On severity: the finding's own framing ("the hook is a control that silently does not cover two of
the modules it exists for") is literally true and is the right way to score it. Nothing here reaches
production, and the cost when it bites is a wasted 30-minute CI job and an undiagnosable red `main` —
which is what the control was written to prevent. I would keep **high** for a test-infrastructure
finding and note it is not a product defect.

The finding's fix (b) — set `timeout_method = "thread"` globally — is also cheaper than it looks:
note that `timeout_method` is **not** in `pyproject.toml` at all today (`grep -n timeout_method
pyproject.toml` → nothing). `signal` is pytest-timeout's platform `DEFAULT_METHOD`
(`pytest_timeout.py:26-30`), so the conftest docstring's "`pyproject.toml` sets `timeout_method =
signal`" is also inaccurate; the comment at `pyproject.toml:313-315` explains a setting the file does
not contain.

### Hygiene

No source file was modified. All experiment files live under `/tmp/hang/` and `/tmp/tmk/`.
`git status --porcelain` shows only other agents' verdict files.
