# D-088 — Third reconciliation with `main` (PR #23): ADR renumbering, and the chart's env parity guard

`main` landed the graph-cache TTL and the Helm render gate while this branch was in review. Two
resolutions were mechanical (both CI steps and both `make` targets are additive; the `tasks/todo.md`
logs are append-only and both kept). Three were not.

**The ADR numbers had collided head-on, and this is the fix.** This branch appended its ADRs as
D-074…D-076 and D-081…D-082 while `main` had independently allocated the *same* numbers for
different decisions — a defect this branch introduced in the first reconciliation and that nobody
caught, because nothing checks the log for uniqueness. `main`'s allocation keeps the numbers (it
merged first and its numbers are already cited from `BACKLOG.md`, `docs/backlog-plan.md` and
`DEFERRED.md`); this branch's five renumber to **D-083…D-087**, and the seven references that
pointed at them — `tasks/todo.md`, `docs/gap-closure-plan.md`, `DEFERRED.md`, `agents/chem_tools.py`,
`tests/test_safety_pairs.py` — move with them. An append-only log with duplicate ids is not an
audit trail, so the collision is fixed rather than annotated.

**`main`'s new chart test caught a real defect in this branch, and the fix widened the guard.**
`test_chart_config_keys_are_real_settings` asserts every `CHEMCLAW_*` env the chart injects names a
real `Settings` field — the point being that pydantic-settings *silently ignores* an unknown
prefixed environment variable, so an operator who sets it gets no error and no effect. This branch's
knowledge-sync work added five such keys (`…_REPO_TOKEN`, `…_REPO_URL`, `…_SYNC_DIR`,
`…_PUBLISH_DIR`, `…_SYNC_INTERVAL_SECONDS`), and only one of them was even visible to the test.

The naive resolution — exempt them — would have thrown away the guard. The premise it encodes is
slightly too narrow rather than wrong: the real invariant is not "every key is a `Settings` field"
but **"every key is read by something"**, and `deploy/knowledge-sync.sh` and `deploy/entrypoint.sh`
are first-party consumers that happen to be shell. So the check now (a) reads the `_helpers.tpl`
env block as well as `values.yaml`, closing the half of the surface it could not see, and (b)
*discovers* the shell-consumed names by scanning `deploy/*.sh` instead of listing them. Discovery,
not a list: the earlier lesson on this branch was that a guard which enumerates catches drift while
one that hardcodes only catches what someone already thought of. Mutation-verified by adding a
`CHEMCLAW_TYPO_SETTING` key to `values.yaml` — the guard names it.

The knowledge-repo push credential is therefore a *fourth* declared secret, against the
three-secret model (D-047). Recorded rather than waved through: the PR-gate submitter shells out to
`git push` and a git host authenticates that push with a token — there is no federated exchange for
it the way there is for the Entra-fronted APIs. The alternative is a knowledge layer that cannot
write.

A companion test asserting shell-consumed keys are never *also* `Settings` fields was written and
then deleted: every overlap it found (`CHEMCLAW_SERVICE_HOST`/`_PORT`, which `entrypoint.sh` passes
to uvicorn) was shared by design, so its exemption list equalled its finding list. A guard with no
possible signal is decoration.

**`service/runner.py` had absorbed two of everything.** Both branches had independently built a
per-turn signal sink and a "last plan emitted" variable, and the auto-merge kept all four. The
consequences were live, not cosmetic: `begin_turn()` and `set_job_sink()` are the *same* contextvar,
so calling both nested one buffer inside the other and the teardown reset them out of LIFO order;
and two `_current_plan` definitions meant the second silently shadowed the first. Consolidated to
one sink and one plan variable. `main`'s `_current_plan` is the one kept — its `None` return
distinguishes "this agent has no plan" from "this agent does not plan", which an empty list cannot
express — with this branch's reason-for-existing (gap RCH-5) folded into its docstring. The
post-resume drain now takes the whole signal buffer rather than only job ids, so a note proposed
during a mid-turn resume still reaches the stream.
