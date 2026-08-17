# mock-server--correctness — reproduction verdicts (round 1)

Lens: **does it actually reproduce?** Re-derived from source. I wrote my own probes under `/tmp`
(`my_launch_probe.py`, `my_launch_probe2.py`, `my_ord_audit2.py`) and a clean venv at
`/tmp/repro_freshvenv`; I did not run the reporter's scripts or reuse their transcripts.

In scope: the three findings marked **high**. The four marked medium/low are out of scope and not
verdicted.

Working tree at `/workspace/chemclaw3_mock` was clean at `2f09174` (only an untracked `uv.lock`),
so no mutation-experiment contamination; I did not need the pristine copy.

---

## 5,760 of the 9,987 seeded ORD records — the entire Suzuki-Miyaura dataset — are rejected wholesale by the real ORD adapter

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  Seeded the export dirs with the mock's *own* seeder rather than reconstructing records by hand:

  ```
  $ cd /workspace/chemclaw3_mock && MOCK_ELN_EXPORT_DIR=/tmp/myaudit/eln \
      MOCK_ORD_EXPORT_DIR=/tmp/myaudit/ord /tmp/repro_freshvenv/bin/python \
      -c "from app.eln.seed import seed_all; print(seed_all())"
  eln_ord_suzuki-miyaura-flow-hte = 5760 ... eln_ord = 10011
  $ ls /tmp/myaudit/ord | wc -l
  10011
  ```

  Then ran the **production path** in the core repo — `fetch_new_entries` off disk, then
  `map_to_ord` on each `RawEntry` (`/tmp/my_ord_audit2.py`):

  ```
  $ /home/user/Chemclaw3/.venv/bin/python /tmp/my_ord_audit2.py
  fetch_new_entries returned: 10011
    bh-amination-plate-btmg: 1317/1317 accepted
    bh-amination-plate-mtbd: 1318/1318 accepted
    bh-amination-plate-p2et: 1320/1320 accepted
    curated: 24/24 accepted
    nielsen-deoxyfluorination-screen: 80/80 accepted
    santanilla-amidation-screen: 96/96 accepted
    santanilla-sulfonamidation-screen: 96/96 accepted
    suzuki-miyaura-flow-hte: 0/5760 accepted
  TOTAL: 4251/10011 accepted; 5760 rejected
    x1536 OrdFormatError: compound has no resolvable structure identifier:
          {'identifiers': [{'type': 'NAME', 'value': '2a, Boronic Acid'}], 'reactionRole': 'REAC…
    x1536 … '2b, Boronic Ester' …
    x1536 … '2c, Trifluoroborate' …
    x1152 … '2d, Bromide' …
  ```

  My totals differ from the reporter's only by the 24 curated ORD fixtures they excluded:
  4251/10011 vs their 4227/9987. The rejected count is **identical**: 5760, and the per-dataset
  split matches theirs row for row.

  Settled the primary data independently:

  ```
  $ /tmp/repro_freshvenv/bin/python -c "import csv; rows=list(csv.DictReader(
        open('app/eln/real_data/suzuki_miyaura_flow_hte.csv'))); print(len(rows),
        sorted({r['r2_name'] for r in rows}))"
  5760 ['2a, Boronic Acid', '2b, Boronic Ester', '2c, Trifluoroborate', '2d, Bromide']

  $ .venv/bin/python -c "from chemclaw.core.reagents import resolve_compound_name as r; ..."
  '2a, Boronic Acid'   -> None
  '2b, Boronic Ester'  -> None
  '2c, Trifluoroborate'-> None
  '2d, Bromide'        -> None
  'Suzuki-Miyaura coupling product of 6-chloroquinoline with 2a, Boronic Acid' -> None
  ```

  Cited locations are real and current: `app/eln/real_hte.py:222-224` is
  `_component(None, "reactant", name=row["r2_name"])`; `:238` is the synthesized `product_name`.

- **Why**

  The rejection is structural, not incidental. `_smiles` (`ord_adapter.py:296-337`) tries
  SMILES → INCHI → NAME/IUPAC_NAME via `resolve_compound_name`, and raises `OrdFormatError` when
  all three miss. That call sits inside the component loop in `_components` (`:241-261`), which is
  called from `_inputs` inside `map_to_ord` — so one unresolvable component takes the **whole
  record**, not just that component. The caller (`ingest/eln/sync.py:189-220`) catches
  `ChemclawError`/`ValidationError` per entry and continues, appending to a rejected list. So the
  loss is exactly as described: 5,760 records written to disk, fetched, and dropped into a report
  field rather than failing the sync.

  The mock's blindness reproduces too. `tests/test_eln.py:161-170` asserts only
  `{i["type"] for i in product_identifiers} <= {"SMILES", "NAME"}`, and
  `test_real_hte_records_match_ord_adapter_shape` (`:156-170`) asserts merely
  `assert identifier_types` — presence, not resolvability. The comment at `:25-30` claiming
  NAME-only is fine "matching real ORD schema flexibility" is false against this consumer, as the
  reporter says.

  **Two things the reporter missed, both making it worse.**

  1. Fixing the four `r2_name` shorthands is *not sufficient*. The synthesized product carries a
     NAME identifier and nothing else, and it blocks independently:

     ```
     $ .venv/bin/python -c "<patch only the '2a' reactant to SMILES OB(O)c1ccccc1, then map_to_ord>"
     STILL REJECTED: OrdFormatError compound has no resolvable structure identifier:
       {'identifiers': [{'type': 'NAME', 'value': 'Suzuki-Miyaura coupling product of
        6-chloroquinoline with 2a, Boronic Acid'}]
     ```

     `_outcomes` (`:263-281`) calls `_smiles` on every product with no fallback. So the fix must
     cover both halves or the corpus still lands at 0/5760. The finding's fix note mentions the
     product; my measurement shows it is a hard second blocker, not an optional extra.

  2. The core repo's `_smiles` docstring (`ord_adapter.py:297-317`) asserts this exact corpus as
     the motivation for adding INCHI/NAME resolution — "of 10,011 ORD records, **5,761 were
     refused**, all of them the Perera Suzuki–Miyaura flow set … 57% of a real corpus lost" — and
     presents the NAME branch as the repair. Measured today the number is **5,760 of 10,011, still
     100% of that dataset**: the NAME branch resolves none of it, because the table returns `None`
     on the paper's shorthand. So a docstring on the consumer side *and* a test comment on the mock
     side both claim this case is handled, and neither is true. That is two independent places
     asserting a repair that did not happen.

  Everything the finding claims reproduces on my own scaffolding, with my own numbers, on the
  production code path. Confirmed.

---

## `POST /workflow/launch` accepts a body carrying no chemistry and returns a converged energy

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

- **What I did**

  Wrote my own probe (`/tmp/my_launch_probe.py`) driving the real FastAPI app through `TestClient`,
  launch → poll to terminal → artifact:

  ```
  $ /tmp/repro_freshvenv/bin/python /tmp/my_launch_probe.py
  EMPTY body {}: HTTP 200 {"workflowId":"mock-run-000001"}
     final status=SUCCEEDED  artifact HTTP 200: energy=-1342.150502 converged=True
  params={} (no chemistry): HTTP 200 {"workflowId":"mock-run-000002"}
     final status=SUCCEEDED  artifact HTTP 200: energy=-1342.150502 converged=True
  wrong key: ethanol as 'molecule': HTTP 200 ... energy=-1342.150502 converged=True
  wrong key: benzene as 'molecule': HTTP 200 ... energy=-1342.150502 converged=True
  identical energy for two different molecules: True
  explicit null smiles -> 422
  ```

  My `-1342.150502` for the empty body is byte-identical to the reporter's. Their second script
  reported `-336.536339` for the wrong-key case; mine differed because I had also renamed
  `method`. Re-running their exact case — rename **only** `smiles`, keep `method`/`basis_set`
  (`/tmp/my_launch_probe2.py`):

  ```
  ethanol  : energy=-1097.972949 converged=True
  benzene  : energy=-1097.972949 converged=True
  identical: True
  ```

  Different constant from theirs (different `method`/`basis_set` strings feed the sha256), same
  mechanism, same conclusion. Every cited location is real: `app/hpc/models.py:12` is
  `class LaunchParams`, `:29` is `params: LaunchParams = Field(default_factory=LaunchParams)`,
  and `app/hpc/router.py:23-30` reads `body.params.smiles/method/basis_set` unguarded.

  Verified the downstream half too: `connectors/qm/activities.py:156-163` really does build
  `QMJobResult(molecule_smiles=job.molecule_smiles, …)` from the *caller's* input, so a parsed
  energy is attributed to whatever the caller thinks it asked for.

- **Why**

  The mechanism is real and I reproduce every step of it. What does not hold is **high**.

  The trigger is not reachable from anything that exists. I grepped every caller of the endpoint
  across both repos: the only in-tree client is
  `src/chemclaw/connectors/qm/hpc/nextflow.py:99-105`, which builds
  `{"pipeline": …, "revision": …, "params": {"smiles": job.molecule_smiles, …, "basis_set": …}}`
  — the correct keys, always, unconditionally. `job.molecule_smiles` has already passed the
  durable boundary's validation before the launcher is reached. So no runtime condition, no
  configuration, and no input drives the defective path: reaching it requires *editing
  `nextflow.py`*. That is a latent fidelity gap in a test double, not a live defect.

  The consequence the finding names is therefore conditional on a second, separate first-party
  regression, and it is entirely confined to a mock — nothing here reaches production, corrupts a
  real cache, or produces a wrong answer today. The honest statement of the harm is: "if someone
  renames the pipeline params (which is exactly what happens when wiring a real Tower pipeline),
  the mock-backed E2E will not notice." That is worth fixing — the fix is three `Field(min_length=1)`
  declarations and is strictly correct — but it is a missing-negative-test class of defect, which is
  medium.

  I considered whether the README's documentation of the endpoint for hand-driving (`README.md:81`)
  makes a hand-written curl with a wrong key reachable, which would push it back up. It does make
  the trigger *human*-reachable, but a human typing the wrong key gets a plausible number and no
  error either way, which is the same medium-grade "silent leniency" harm rather than a new one.

  One peripheral claim I could not check: "A real Seqera/Tower launch with no pipeline and no
  params is a 400." No tenant here. It is plausible and not load-bearing for the mechanism, so it
  does not change the verdict either way.

---

## The vendor MCP server does not start: `mcp>=1.2` resolves to mcp 2.0.0, which has no `mcp.server.fastmcp`

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  Built a genuinely fresh venv and installed the package from the declared constraints only:

  ```
  $ uv venv /tmp/repro_freshvenv --python 3.11
  $ uv pip install --python /tmp/repro_freshvenv/bin/python -e .
  $ /tmp/repro_freshvenv/bin/python -c "import importlib.metadata as m; print(m.version('mcp'))"
  2.0.0
  ```

  Enumerated what `mcp.server` actually ships in 2.0.0 rather than taking the reporter's word:

  ```
  $ /tmp/repro_freshvenv/bin/python -c "import pkgutil, mcp.server; print([x.name for x in
        pkgutil.iter_modules(mcp.server.__path__)])"
  ['__main__', '_otel', '_streamable_http_modern', 'apps', 'auth', 'caching', 'connection',
   'context', 'elicitation', 'extension', 'lowlevel', 'mcpserver', 'models', 'request_state',
   'runner', 'session', 'sse', 'stdio', 'streamable_http', 'streamable_http_manager',
   'subscriptions', 'transport_security', 'validation']
  ```

  No `fastmcp`. The documented entrypoint (`start-mcp.sh` → `python -m app.mcp_tools.vendor_server`):

  ```
  $ /tmp/repro_freshvenv/bin/python -m app.mcp_tools.vendor_server
    File "/workspace/chemclaw3_mock/app/mcp_tools/vendor_server.py", line 15, in <module>
      from mcp.server.fastmcp import FastMCP
  ModuleNotFoundError: No module named 'mcp.server.fastmcp'
  ```

  Collection-abort claim, verified by running:

  ```
  $ uv pip install --python /tmp/repro_freshvenv/bin/python -e ".[dev]"
  $ /tmp/repro_freshvenv/bin/python -m pytest -q
  ERROR collecting tests/test_mcp_vendor.py
  E   ModuleNotFoundError: No module named 'mcp.server.fastmcp'
  ERROR tests/test_mcp_vendor.py
  !!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
  1 error in 0.81s
  ```

  And the stated remedy:

  ```
  $ uv pip install --python /tmp/repro_freshvenv/bin/python "mcp<2"
   - mcp==2.0.0  + mcp==1.29.0
  $ /tmp/repro_freshvenv/bin/python -m pytest -q
  28 passed, 1 warning in 10.52s
  ```

  `pyproject.toml:11` is `"mcp>=1.2",` and `vendor_server.py:15` is the import — both exact.

- **Why**

  This is not a stale or environment-specific claim: it reproduces from a clean `uv venv` today,
  on the declared constraint, with no reporter scaffolding. Zero tests run — pytest exits on a
  **collection** error, so the HPC and ELN suites never execute, which means the entire mock is
  unverified from a fresh checkout, not just the MCP third of it. That collection abort is what
  earns high on its own: a repo whose test suite cannot start is a repo whose green history says
  nothing about its current state.

  I also confirmed this is not merely hypothetical for the checked-in environment: the repo's
  *committed* working venv is already broken the same way —

  ```
  $ .venv/bin/python -c "import importlib.metadata as m; print(m.version('mcp'))"
  2.0.0
  $ .venv/bin/python -c "from mcp.server.fastmcp import FastMCP"
  ModuleNotFoundError: No module named 'mcp.server.fastmcp'
  ```

  so the mock's HTTP-transport vendor server — the one piece that exercises Chemclaw3's
  `HttpMcpServerSpec` path — cannot be started here right now, not only after a reinstall. The
  reporter's baseline note ("green once `mcp` is pinned below 2.0") is the only reason any other
  finding in that file has a working substrate at all.

  The fix as written (`"mcp>=1.2,<2"`) is correct and I verified it restores 28 passing tests.
  Confirmed.
