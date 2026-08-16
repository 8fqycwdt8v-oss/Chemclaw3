# Post-migration review — nine reviewers over `ab3da835..ad1cb1ef`

Status: **done and merged.** `Chemclaw3#189` (`e7de2bdf`) and `Chemclaw3-mcp#8` (`53c6035b`).

## What was asked

Review every line of the capability migration and its neighbours, using subagents heavily. Then,
on four decisions taken afterwards: restore the artifact path, wire the chart, close four smaller
gaps, and merge on green.

## Steps

- [x] Nine scoped reviewers, each told to reproduce rather than reason: the calc client, the durable
      layer, the safety removal, the connector/chart seam, layer 1, the BO inversion, dangling
      references, test quality (by hand mutation), and cross-repo consistency.
- [x] Every headline re-verified independently before reporting it (lessons rule 35).
- [x] Fixed: the exception relabelling in `calc_session`, the `McpError` classification, five
      un-heartbeated call sites, 24 dead calculator settings, and six stale claims that a deleted
      control still runs.
- [x] Restored the Hessian to the artifact store as `ArrayOffloadingStore`
      (`D-2026-08-16-a-result-too-big-for-its-row-is-an-artifact`).
- [x] Chart: `CHEMCLAW_CALC_SERVER_URL`, three bearer slots, three egress ports, and the runbook
      claim that matched the wrong one in `values.yaml`.
- [x] Deleted `run_cached` and `cached_remote`'s unreachable no-key branch.
- [x] Measured the verifier against a non-compliant `openai_compatible` server.
- [x] mcp repo: the logD contract test and the ported ANC unit tests.

## Verified

`make lint`, `make type` (592 files), every validator, and the full suite with Postgres up:
**3989 passed, 1 skipped, 0 failed**. CI green on both repos before either merge.

Live, against a real calc server on 8860: `calculation_key` matched the key stamped on the result
for **6 of 6** cached tools, and `predict_logd` correctly derived none — which closes the "not
verified" tranche 4 shipped with.

Each fix carries a test checked **red against the pre-fix code**; the artifact policy's three rules
were mutated separately, one at a time.

## Review

**What went well.** The fan-out earned its cost in one specific way: two reviewers independently
found the `calc_session` relabelling, from different directions, which is the kind of agreement a
single pass cannot produce. And the instruction to reproduce rather than reason is what turned
"24 settings look unused" into a measurement — they were all in `.env.example`, so the parity test
that existed was green, and the parity nobody had written was the one that mattered.

**What the reviewers got wrong, which is why every headline was re-checked.** One reported "dead
code: none found" while 24 settings, `run_cached` and `put_all` were all dead. Another reported that
`langchain_openai` silently downgrades `json_schema` to `function_calling`; reading the upstream
source showed neither downgrade path can trigger for a Pydantic v2 schema or a non-`gpt-4` model
name. Reporting either as given would have misled in opposite directions.

**A failed approach, recorded so it is not retried.** The Hessian restore was first written as a
bespoke cached path in `compose.py`, mirroring the pre-split `run_cached_hessian`. It needs its own
MCP session to fetch the key before the lookup, which makes `compose` a *second* module that opens
one — and `calc_server_fake.install` patches `remote.calc_session` on the stated ground that it is
the only one. The heartbeat test went red on a real socket immediately. The replacement expresses
the policy as a `ResultStore` wrapper, which is smaller and leaves every caller untouched. **The
test failure produced a better design than the plan did.**

**A process defect worth keeping.** One mutation check silently did not apply: `ruff format` had
reflowed the code I was pattern-matching on, so the replacement was a no-op and the test "passed"
against an unbroken tree. It only surfaced because the expected failure did not appear. **A mutation
that does not fail is evidence of nothing until you have confirmed the mutation landed** — assert
the target text was found, rather than trusting a string replace.

**A claim of mine that needed narrowing.** I reported the two `predict_logd` implementations as
"identical to every printed digit". GFN2-xTB's SCF is not bit-reproducible run to run here (drift in
the 9th significant figure), so that was one lucky pair of calls. What holds — and what the contract
test now pins, against a frozen pKa — is that the two agree exactly *given the same input*. Cache
keys are unaffected: they name inputs and program versions, never SCF output.
