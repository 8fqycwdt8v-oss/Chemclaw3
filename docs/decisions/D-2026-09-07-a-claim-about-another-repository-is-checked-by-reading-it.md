# D-2026-09-07-a-claim-about-another-repository-is-checked-by-reading-it — one sibling search, and three cross-repo contracts that now have readers

**Context.** Four things in this tree are claims about `Chemclaw3-mcp`, and every one of them was
believed rather than checked.

`SERVED_ELSEWHERE_ALLOWANCE` bounds the tool schemas of three bundles this repository declares and
does not serve. `PREFIX_BOUND` is that allowance plus `tests/test_context_floor.py`'s ceiling, and
`core/config/agent.py` derives **both** compaction defaults from `PREFIX_BOUND`. So a number in
another repository sets what every request in this one may cost.

**Measured, and the first finding is the one that makes the rest moot.**

```
$ .venv/bin/python -m pytest tests/test_context_floor.py -rs -q
16 passed, 1 skipped
SKIPPED [1] …:1162: the 3 bundles served from Chemclaw3-mcp (chem, rxnpredict, safety) were NOT
measured: no Chemclaw3-mcp checkout at /home/user/Chemclaw3-mcp (set CHEMCLAW_MCP_CHECKOUT)…

$ CHEMCLAW_MCP_CHECKOUT=/home/user/8fqycwdt8v-oss/chemclaw3-mcp … -q   →  17 passed
```

The checkout was there. `_sibling_python` searched one path in one casing (`parents[2] /
"Chemclaw3-mcp"`) under one variable (`CHEMCLAW_MCP_CHECKOUT`); `infra/live/siblings.sh` searched
four candidates in two casings under `CHEMCLAW_MCP_REPO`. Both landed on 2026-09-06, in different
pull requests, and **`siblings.sh`'s own header documents fixing exactly this bug** — *"`make
live-up` failed at the fleet checkout on a machine that had the fleet checkout."* The ratchet
re-introduced it the same day, one directory over. Consequence: `SERVED_ELSEWHERE_ALLOWANCE`,
`PREFIX_BOUND` and both compaction defaults had never been checked by a machine anywhere. The skip
was loud, which is a real mitigation and is why nothing worse happened; it is still a control
nothing exercised.

Three more, measured after the search was fixed:

- **`SERVED_ELSEWHERE` names three of the five bundles the fleet publishes**, and the completeness
  guard beside it iterates `enabled()` — which under `make test` reads only this tree's
  `connectors/`. A bundle whose manifest lives next door was structurally invisible: the honest
  answer to *"what goes red when the fleet adds one?"* was "nothing, unless the fleet also adds its
  manifest here." Over the same measurement path, widened: `chem` 5,577/12, `props` 2,936/6,
  `pyexec` 1,142/1, `rxnpredict` 2,655/6, `safety` 1,632/3 — **13,942 tokens over 28 tools**.
  `infra/live/e2e-full-stack/up.sh` puts that whole directory on `CHEMCLAW_CONNECTORS_DIR`, so that
  lane's prefix is **78,560** against a `PREFIX_BOUND` of 76,000.

- **`chem`, `safety` and `rxnpredict` are declared twice, in two repositories**, and nothing
  compared the copies. They agree today — same tools as sets, same `read_only`, same `token_env`,
  same timeouts — but first-directory-wins resolves a collision with no merge, no warning and no
  log, and `connector validation passed` either way. The fleet's own `manifests/README.md` bans a
  second copy of one declaration *inside* the fleet and then ships one across the boundary,
  asserting the equality in prose.

- **The `calc` backend seam has no manifest in either direction.** `calc` is `mount: backend`,
  deliberately unloadable here, and it carries every calculation this system runs. Ten tool names
  and their argument dicts are hardcoded in `connectors/calc/compose.py` and `remote.py`; the fleet
  records `servers/calc/tool-surface.json` precisely as the rename tripwire and nothing here read
  it; `tests/calc_server_fake.py` is a hand-written reproduction of the same contract, so a fleet
  rename left the whole suite green and failed at runtime. Measured against the recorded surface:
  **0 argument keys undeclared, 20 tools matching 20** — the contract is sound and the check was
  absent. That is the finding, not a refutation of it.

**One premise from the review was wrong and is recorded as such.** It counted six bundles the fleet
serves, including `rxnlabel`. `rxnlabel` and `calc` live in `manifests-internal/`, which no
published `export` line names, and both declare a `mount:` key this repository's `extra="forbid"`
manifest model *refuses* — so mounting them is a startup error naming the file, not a silent
capability. Five is the number, and `rxnlabel`'s 1,197 tokens are not part of any prefix here.

**Decision.**

1. *One sibling search, and it is the shell's.* `infra/live/siblings.sh` gains `sibling_env_vars`,
   a table of every variable that may name a checkout, which `sibling_repo` consults after the
   caller's own — so `CHEMCLAW_MCP_REPO` and `CHEMCLAW_MCP_CHECKOUT` both work in both lanes.
   `tests/siblings.py` **invokes** that function rather than reimplementing it. A Python copy of
   those fourteen lines would be a second answer to one question, which is the defect `siblings.sh`
   exists to have ended; re-committing it inside the fix would have been this repository's
   signature mistake. The cost is a `bash` subprocess on a path that then spawns the sibling's
   interpreter anyway.

2. *The allowance is not widened, and the excess is stated instead.* `props` and `pyexec` are
   declared only in `Chemclaw3-mcp`; no `connectors:` entry in the chart names them and no
   `enabled()` here returns them. Charging them to `PREFIX_BOUND` would raise both compaction
   defaults for every deployment on account of two bundles those deployments never bind — and the
   configuration that does bind them talks to `chemclaw.cli.mock_llm`. So `SERVED_ELSEWHERE` and
   its allowance keep their meaning (*what this repository declares and does not serve*), and a
   second constant, `FLEET_PUBLISHED_ALLOWANCE`, bounds the fleet's whole published directory —
   which is what the e2e lane pays, and the only place a fleet-added bundle now lands.

3. *Three cross-repo contracts get readers.*
   `tests/test_sibling_manifest_agreement.py` compares the two declarations of `chem`, `rxnpredict`
   and `safety` — **as sets, not lists**: `chem`'s twelve tools are in a different order in the two
   files today and order decides nothing, so a list comparison would have failed on its first run
   and taught a reader to bump a check rather than read it. The same file walks
   `connectors/calc/`'s hardcoded call sites and checks their tool names, their argument keys and
   the server's *required* arguments against `tool-surface.json`, and pins the fake's declared
   surface to it. The subjects are **derived** from the two trees — the names both declare, the
   call sites the AST finds — so a fourth port or an eleventh call site is covered from the commit
   that lands it. A call site whose tool expression the walker cannot resolve to string literals
   **fails**; it is never passed over.

4. *The skip is counted.* `tests/conftest.py::_report_sibling_skips` joins the Postgres, Temporal
   and helm reporters. Two sentences in `tests/test_context_floor.py` already claimed it existed;
   `grep` for a sibling in `tests/conftest.py` found nothing. Correcting the prose was the
   alternative and it is the worse one — `-ra` does print the skip, among two hundred others, and
   this epilogue exists precisely because a line in that list is not something a reader sees.

**Deliberately not done: cloning the sibling in CI, and this is a recommendation rather than a
change.** `.github/workflows/` is outside the change set this was written in, so the decision is
recorded and the work is not claimed. The two halves have very different costs and only one is
plausible:

- The **manifest and `tool-surface.json` checks** need a checkout and no build — five YAML files
  and one JSON file. A shallow clone is seconds, and they are the checks that catch a rename.
  These should run in CI, and `sibling_python` is split from `sibling_root` so that they can.
- The **schema measurement** needs the fleet's built `.venv`: RDKit, torch and a T5 checkpoint's
  dependencies, to answer `tools/list` for three servers. That is a large CI job to bound a number
  that moves on somebody else's merge, and the honest place for it is a scheduled or fleet-side
  run. It stays opt-in.

Leaving both silent was the third option and is the one this ADR rejects: a check nothing runs is
not a control, and a loud skip is the least it can be.

**What keeps it true.**

- `test_the_ratchet_finds_the_checkout_the_live_lane_finds` — fails when this file's search and
  `infra/live/siblings.sh`'s resolve differently on a machine that has the checkout. Watched red
  against the pre-fix `_sibling_python`.
- `test_the_bundles_both_repositories_declare_are_the_ones_charged_to_the_allowance` — reads the
  fleet's `manifests/` and fails when the set of names both trees declare stops being
  `SERVED_ELSEWHERE`.
- `test_the_whole_directory_the_e2e_lane_mounts_is_bounded_too` — bounds every bundle the fleet
  publishes, which is the half nothing here watched.
- `test_a_bundle_declared_in_both_trees_declares_the_same_surface` — tools, `read_only`,
  `state_changing` and `token_env`, for every name declared twice.
- `test_the_calc_seam_calls_only_tools_the_fleet_records_serving` and
  `test_the_fake_calc_server_serves_exactly_the_surface_the_fleet_records` — the seam that has no
  manifest on either side.
- `tests/conftest.py::_report_sibling_skips` — every one of the above can only pass, fail, or say
  out loud that it did not run.
