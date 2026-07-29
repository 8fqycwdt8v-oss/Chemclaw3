# D-117 — Consolidating the outstanding branches, and deleting what four generations of the design left behind

Three branches were open against `main`, two of them with live PRs, and none of them could be
merged. All three were cut before the Replit monorepo restructure, so `git diff main..branch`
reports a whole-tree file move: the branch's `agents/`, `workflows/`, `workers/` and `tests/` sit at
the repository root while `main`'s sit under `services/chemclaw/`. `git cherry` marks every commit
unmerged, which reads as "none of this work has landed" and is wrong — it is a patch-id artifact of
the move, and most of the content *had* landed by other routes.

**What a merge would actually have done.** `git diff --diff-filter=A main..branch` — the files a
merge would add — is the honest measure. Every added path is at the old root layout, so a merge
re-creates the whole service a second time in the wrong place. Worse, several of the additions are
modules `main` deliberately deleted: `agents/calc_tools.py` and `agents/bo_tools.py` (D-111/D-114),
`deploy/helm/chemclaw/templates/deployment-mcp.yaml`, `tests/test_mcp_server_spec.py`,
`tests/test_mcp_transport.py`, `workflows/bo_campaign.py`, and two `SKILL.md` files that moved into
`connectors/*`. Merging would have resurrected the pre-connector-seam architecture D-110 retired,
duplicated at paths nothing imports. So the branches are *ported*, not merged, and then deleted.

**What was genuinely missing turned out to be two small things and one real bug.**

`claude/chemclaw3-github-repos-8w2wvg` contributed nothing — every file it adds relative to `main`
is already present. It is deleted with no port.

`claude/ci-test-timeout-guard` (PR #27) contributed only `pytest-timeout`. Its job-level
`timeout-minutes` had already reached `main` by another route; the per-test cap had not. Both
matter, and they are not redundant: the job timeout bounds the runner bill, the per-test timeout
*names the test*. The `signal` default is kept rather than `thread` because `thread` calls
`os._exit()` and takes the whole session down, which would reproduce the original failure — a hang
early in alphabetical collection order stopping everything after it from running.

`claude/session-history-endpoints` (PR #25) contributed only its approval-signal fix; its two
routes and `SessionOwnerStore.list_for_owner` had already landed. That fix is the real find.
`service/runner.py` yielded `ApprovalRequestEvent(prompt=...)` and never set `approval_id`, so the
field documented since D-032 as "the durable hold's handle, so a surface can actually answer it via
`POST /approvals/{id}/decision`" was always `""`. `service/static/app.js` does
`if (!evt.approval_id) return;` — so the Yes/No control never rendered, and **every interaction
approval was unanswerable from every surface**. The durable hold, the decision route and the review
queue were all built, tested, and reachable only by a client that already knew an id nothing ever
told it.

The fix is the mechanism the same file already uses three times over: an `ApprovalSignal` on the
per-turn signal buffer, recorded by `start_approval` and mapped in `_signal_event` beside
`JobSignal`, `ProposalSignal` and `QuestionSignal`. A turn signal rather than a return value for
exactly the reason D-077 gives for the other three — the handle must come from the tool that opened
the hold, never from anything the model can author, or the agent could fabricate an approval.
It is announced on the already-started path too, so re-surfacing a candidate stays answerable;
without that, the idempotent branch would hand back an id it never announced.

Plan approvals keep `approval_id == ""`, and that emptiness is now load-bearing rather than
incidental: they have no durable hold and are answered by the next turn, so a handle there would
point a surface at a workflow that does not exist. The field is what distinguishes the two kinds.

**What was deleted, and why each was safe.** The repository carried four generations of itself.

*The Replit-era TypeScript monorepo* — `artifacts/`, `lib/`, `scripts/`, `package.json`, a 210 KB
`pnpm-lock.yaml`, `tsconfig{,.base}.json`, `.replit`, `replit.md`, `issues_replit.md`,
`attached_assets/`, and `.agents/` — 174 tracked files, every one last touched by the single Replit
commit, and referenced by **nothing** in the Python service (grepped across `*.py`, `*.md`, `*.yml`,
`*.yaml`, `Makefile`, `*.sh`, `*.toml`). The real client lives in its own repository. Removing it
removes the only reason this repo needed a Node toolchain, and removes `replit.md` — a second
status document that contradicted the first on two load-bearing points (a pip-managed venv against
`uv sync` everywhere, and "Anthropic via Replit AI Integration, no API key required" against the
config-selected provider seam). `.agents/memory/` went with it: one of its two files recorded "no
pull requests" as a standing rule, which is the opposite of what `CLAUDE.md` says.

*A bare git repository committed into the source tree* — `services/chemclaw-notes-remote.git/`, 319
files. It is the throwaway push target for the PR-gate during local testing; it has no business in
version control.

*Three submodule gitlinks with no `.gitmodules`* — `chemclaw-mock`, `chemclaw-notes-repo`,
`chemclaw-ui` were all mode-`160000` entries in a repository that has no `.gitmodules` file at all,
so every fresh clone, CI included, got three directories `git submodule update` could not resolve.
A dangling gitlink is not a third state between "vendored" and "separate repo"; it is a defect.

*`mcp_servers/calc/server.py`* — 297 lines defining a **second live copy** of seven tools the `calc`
bundle also serves, with byte-identical `predict_pka` bodies. Three documents already stated it was
deleted (`mcp_servers/README.md`, this log at D-113, `tasks/todo.md`), and it was still built into
the image and dispatchable as `CHEMCLAW_COMPONENT=mcp-calc`. This is exactly the merge class D-116
describes — a file deleted on one side and edited on the other comes back — and it is the one
instance that pass did not catch.

*The xTB cost-model island* — `calc/xtb_cost.py` had **zero importers**; its only mentions anywhere
were past-tense prose describing the design D-114 replaced. It went with its test module and its
five orphaned settings (each of which also had an `.env.example` line). The `.env.example`↔config
parity tests passed throughout, because they only check that the two sides mirror *each other* —
neither can see that both sides are dead.

*Two more zero-importer modules* — `agents/job_events.py`, whose replacement's docstring already
said the consolidation had happened; and `scripts/validate_ord.py`, a self-declared shim
(`make eln-validate` runs `python -m eln.validate`).

**One thing the deletion pass found rather than removed.** `xtb_scan_max_points` looked like part of
the dead cost island — its only reference was inside the dead test. It is not: it is a *cap on an
agent-triggerable operation* that has described itself since it was added as bounding a scan "the
way `xtb_hessian_max_atoms` bounds a Hessian", and unlike that one it was **never enforced
anywhere**. `ScanSpec.values` carried `min_length=1` and no maximum, and every point is a full
constrained geometry optimization, so the length of a list the model supplies *is* the cost of the
call. Deleting it would have quietly removed an intended safety property; it is now a validator on
the spec, where every caller — tool, durable job, cache key — is built from it.

**The CI that everyone believed ran.** GitHub Actions reads workflows only from the repository root,
so `services/chemclaw/.github/workflows/{ci,deploy}.yml` had never executed once. What that cost:
`make cov` and its 80% floor, `make eval`, `make eln-validate`, `make helm-validate`, and the image
build with its non-root entrypoint smoke test. Three live documents asserted otherwise —
`pyproject.toml` ("CI runs `make cov` as a gate") and two `[x]` entries in `BACKLOG.md`. Every gate
now runs from the root; the stranded copies are deleted rather than left as "the service's own
record", because a record that contradicts the executing configuration is worse than no record.

The `rollout` job did not come with them. Its entire body was `echo "docker push + helm upgrade
..."`. Writing the real one now would mean asserting a registry, a namespace and a credential shape
that do not exist yet, so it is recorded in `DEFERRED.md` with the trigger that would make it
writable — a real cluster.

**The `mcp-calc` case taught a second lesson.** `tests/test_deploy_chart.py` checked that every
component the chart declares has an entrypoint case — the crash-loop direction. It could not see the
reverse: an entrypoint case for a component nothing deploys. That is precisely how a "deleted"
module stayed routable in a production image. Both directions are now asserted.

**Three lists of "the first-party packages", all wrong in different ways.** `make type` omitted
`service` and `sources`; the wheel `packages` list and `[tool.coverage.run] source` both omitted
`connectors` — 37 modules, the entire capability surface — and `templates`. `pyproject.toml` states
the invariant it was violating ("a non-editable `pip install` of the wheel must ship all of them or
the `chemclaw` command and its imports break"), and nothing checked it. The `make type` gap is the
*same* bug the repo had already found and hand-fixed once for `connectors`/`templates`; the hand fix
did not stop it recurring for two other packages, because the mechanism was a comment saying "keep
this list in sync". `tests/test_packaging.py` now derives all three from the filesystem. Type
checking went from 353 to 366 files, and `service/` — never directly checked before — had four real
errors, one of them a `type: ignore` silencing an `Any` leak out of `app.state`.

**The prose contract was checking the wrong file.** `scripts/validate_prose_contract.py` matched a
backtick immediately followed by `(`. `_INSTRUCTIONS` — the most important agent-facing prose in the
codebase, and the first thing a tool rename breaks — names every tool **bare**
(`gather_evidence sweeps all internal sources`). The pattern therefore matched **zero times** there,
and only `SKILL.md` files were ever really validated. Its stand-in, a hardcoded eleven-name set in
`tests/test_agent.py`, could only catch drift in names someone had already thought to list, while
the instructions named at least ten more that nothing covered.

The fix is a second pattern for bare `snake_case`. An underscore is what makes that safe against
English prose, which does not contain any: measured over the entire corpus it produced exactly one
false positive, an argument name inside a call, now excluded by the pattern. Both the validator and
the test extract from one function, so they cannot disagree about what the prose says. This is
sequenced deliberately **before** the connector-seam work that renames tools — a contract fixed
after the rename it was supposed to catch is not a contract.

Fixing it also exposed a fourth name space problem: `validate_skills`, `validate_templates` and
`validate_prose_contract` each unioned in-process tools with connector tools, while only
`agents/chemclaw_agent.py` also unioned the generated template launchers. A skill naming
`run_hazard_briefing` failed validation although the tool exists. All four now call one
`available_tool_names()`.
