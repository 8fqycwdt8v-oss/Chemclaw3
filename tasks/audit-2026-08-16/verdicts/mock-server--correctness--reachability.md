# chemclaw3_mock — CORRECTNESS, reachability/consequence verification

Lens: is the trigger producible by a real caller, and is the consequence what is claimed?
In scope: the three findings marked **high**. Findings 4–7 (medium/low) not examined.

Working-tree check: the four backend files I relied on
(`connectors/qm/specs.py`, `connectors/qm/hpc/nextflow.py`, `connectors/qm/activities.py`,
`ingest/eln/ord_adapter.py`) are byte-identical to the pristine `HEAD` copy — `diff -q` printed
nothing for all four. `/workspace/chemclaw3_mock` is clean at `2f09174` apart from an **untracked**
`uv.lock` (relevant to finding 3, see there).

---

## 5,760 of the 9,987 seeded ORD records — the entire Suzuki-Miyaura dataset — are rejected wholesale by the real ORD adapter

- **Verdict**: CONFIRMED
- **Severity I would assign**: high
- **What I did**:

  Seeded the mock's export dirs exactly as `start.sh` does (`MOCK_HTE_MAX_RECORDS_PER_DATASET`
  defaults to `0` = seed everything, `app/config.py:49`), then drove the **real** backend adapter's
  own `fetch_new_entries` → `map_to_ord` loop over that directory — not a hand-built payload:

  ```
  $ MOCK_ORD_EXPORT_DIR=/tmp/seedtest/exports/ord .../python -c "from app.eln.seed import seed_all; print(seed_all())"
  {... 'eln_ord_suzuki-miyaura-flow-hte': 5760, ..., 'eln_ord': 10011}
  $ ls /tmp/seedtest/exports/ord | wc -l
  10011

  $ .venv/bin/python /tmp/verify_fetch.py     # CHEMCLAW_ORD_EXPORT_DIR -> that dir
  fetched=10011 mapped_ok=4251 rejected=5760 (suzuki rejects=5760)
  ```

  Per dataset (`/tmp/verify_ord.py`), matching the reporter's numbers exactly:

  ```
  bh-amination-plate-p2et: 1320/1320 accepted
  bh-amination-plate-btmg: 1317/1317 accepted
  bh-amination-plate-mtbd: 1318/1318 accepted
  suzuki-miyaura-flow-hte: 0/5760 accepted
      x1536  OrdFormatError: compound has no resolvable structure identifier:
             {'identifiers': [{'type': 'NAME', 'value': '2a, Boronic Acid'...
      x1536  ... '2b, Boronic Ester' ...
      x1536  ... '2c, Trifluoroborate' ...
  santanilla-*: 96/96, 96/96   nielsen-*: 80/80   curated-ord: 24/24
  TOTAL: 4251/10011 accepted; 5760 rejected
  ```

- **Why**: Trigger and consequence both hold, and the trigger is not hypothetical — it is the
  configured default of the shipped live harness. `infra/live/e2e-full-stack/up.sh:114` seeds the
  mock into `$MOCK_REPO/data/eln/exports/ord`, `:188` sets `CHEMCLAW_ORD_EXPORT_DIR` to the same
  path, and `CHEMCLAW_DATA_SOURCES="graph,eln-json,eln-ord"` enables the ORD source. Nothing
  upstream narrows it: `OrdJsonAdapter.fetch_new_entries` has **no batch cap** — it globs the whole
  directory (measured: 10,011 entries fetched in one call) — so every one of the 5,760 files is
  fetched and then rejected. The rejection is per-record and total: `_smiles` raises before any
  reactant/product/yield is used, so the paper's procedure, catalyst, base and yield go with it
  (`ord_adapter.py:337`). `resolve_compound_name` is a committed synonym table that returns `None`
  by design, so the four `2a`–`2d` shorthands and the synthesized product description cannot
  resolve — I confirmed the failing identifier is the NAME-only reactant the finding names.

  Two small corrections, neither changing the verdict:
  - "per-entry-silent" is slightly generous to the defect's stealth. `ingest/eln/sync.py:255-264`
    logs each rejection at **WARNING** on first sight (DEBUG only on replay), and the run logs
    `ingested=… rejected=…` at INFO. It is invisible to *assertions*, not to logs.
  - the corpus is 10,011 ORD files, not 9,987 — the finding's denominator excludes the 24 curated
    ORD records, which do all pass. 5,760/10,011 = 57.5%.

  Severity: high is right for this repo. The mock's single largest advertised dataset
  (`README.md:146` — "5,760, Suzuki-Miyaura, Perera et al.") is 100% unusable by the only consumer
  that exists, and the mock's own tests structurally cannot see it: they assert an identifier
  *type* is in `{"SMILES","NAME"}`, which is a shape claim about a schema, not a claim about this
  consumer. No production data path is harmed — this is a fidelity harness — which is why it is
  high and not critical.

---

## `POST /workflow/launch` accepts a body carrying no chemistry and returns a converged energy

- **Verdict**: OVERSTATED
- **Severity I would assign**: low
- **What I did**:

  Reproduced the mechanism against the mock, exactly as reported:

  ```
  $ /tmp/freshvenv1/bin/python /tmp/verify_hpc.py
  empty body -> 200 {'workflowId': 'mock-run-000001'}
    poll -> {'workflow': {'status': 'SUCCEEDED'}}
    artifact -> energy=-1342.150502 converged=True
  smiles=null -> 422
    CCO under wrong key -> energy=-336.536339 converged=True
    c1ccccc1 under wrong key -> energy=-336.536339 converged=True
  identical: True
  ```

  Then traced back to the outermost real caller. `launch_run`
  (`connectors/qm/hpc/nextflow.py:88-110`) is the **only** code in the backend that posts to
  `/workflow/launch` (`grep -rn "workflow/launch" src/` → that file and its unit test, nothing
  else), and it builds the body from three *literal* keys:

  ```python
  "params": {"smiles": job.molecule_smiles, "method": job.method, "basis_set": job.basis_set}
  ```

  And the values cannot be empty — `QmJobSpec` declares all three `Field(min_length=1)`
  (`connectors/qm/specs.py:39-41`):

  ```
  $ .venv/bin/python -c "QMJobInput(molecule_smiles='', method='B3LYP', basis_set='d')"
  rejected -> String should have at least 1 character [type=string_too_short]
  $ ... QMJobInput(method='B3LYP', basis_set='d')
  rejected -> molecule_smiles: Field required
  ```

- **Why**: The mechanism is real and I reproduce every number in the finding, including the
  `QMJobResult`-from-`job.molecule_smiles` attribution at `activities.py:156-163`. What does not
  hold is the trigger. The finding's own trigger statement names it: "*a rename or a dropped field
  in the caller*" — i.e. the input is producible only by editing the backend's source. Two
  independent upstream guards stand in the way of every real caller: a pydantic model that rejects
  empty and missing chemistry before a payload is built at all, and a payload whose keys are
  hardcoded string literals rather than derived from anything a caller controls. There is one
  launcher interface (`hpc_launch_interface`, a two-value literal — the other value is the
  in-process mock, which never speaks HTTP). So today, in every configuration, the mock only ever
  receives well-formed chemistry.

  The consequence is also softer than stated. "Returns a *plausible-but-wrong* answer that is then
  persisted and cached as the requested molecule's energy" describes the mock's ordinary,
  correct-key behaviour too: `Job.energy_hartree` is `sha256(smiles|method|basis)` scaled into
  `[-2000, -50]` — a fabricated number for *every* input, correct key or not. The incremental harm
  of the defect is therefore narrower than the finding's framing: not "a wrong energy is cached"
  (that is the mock's design), but "two different molecules become indistinguishable", i.e. an E2E
  test loses its ability to detect that the chemistry ever moved. That is a genuine loss of
  discriminating power in the harness, and the proposed fix (required fields, 422) is the right
  one — but it is a fidelity gap that would catch a *future* caller regression, not a defect
  anything can trigger now. High implies something that bites today; this does not. Low.

---

## The vendor MCP server does not start: `mcp>=1.2` resolves to mcp 2.0.0, which has no `mcp.server.fastmcp`

- **Verdict**: CONFIRMED
- **Severity I would assign**: high
- **What I did**: Built a fresh venv from the repo's own documented install path
  (`README.md:25` — `pip install -e .`; `README.md:197` — `pip install -e ".[dev]"`):

  ```
  $ uv venv /tmp/freshvenv1 && VIRTUAL_ENV=/tmp/freshvenv1 uv pip install -e ".[dev]"
  $ /tmp/freshvenv1/bin/python -c "import importlib.metadata as m; print('mcp', m.version('mcp'))"
  mcp 2.0.0
  $ /tmp/freshvenv1/bin/python -m app.mcp_tools.vendor_server
    File ".../app/mcp_tools/vendor_server.py", line 15, in <module>
      from mcp.server.fastmcp import FastMCP
  ModuleNotFoundError: No module named 'mcp.server.fastmcp'
  $ /tmp/freshvenv1/bin/python -c "import pkgutil, mcp.server as s; print([m.name for m in pkgutil.iter_modules(s.__path__)])"
  ['__main__', '_otel', '_streamable_http_modern', 'apps', 'auth', 'caching', 'connection',
   'context', 'elicitation', 'extension', 'lowlevel', 'mcpserver', 'models', 'request_state',
   'runner', 'session', 'sse', 'stdio', 'streamable_http', 'streamable_http_manager',
   'subscriptions', 'transport_security', 'validation']        # no `fastmcp`
  $ /tmp/freshvenv1/bin/python -m pytest -q
  E   ModuleNotFoundError: No module named 'mcp.server.fastmcp'
  ERROR tests/test_mcp_vendor.py
  !!!! Interrupted: 1 error during collection !!!!
  1 error in 0.84s
  ```

- **Why**: Trigger and consequence both exactly as stated, and I checked the two things that could
  have made it unreachable, both of which fail to save it:

  1. **No lockfile pins it.** `git ls-files` shows 34 tracked files and `uv.lock` is not among them
     (`git ls-files --error-unmatch uv.lock` → "did not match any file(s) known to git"). The
     `uv.lock` present in the working tree is untracked — it is an artifact of an earlier session in
     this shared checkout, not repo content, and a fresh `git clone` has nothing to resolve against.
     `.gitignore` does not exclude it either; it was simply never committed.
  2. **No CI or startup guard.** There is no `.github/` in the tracked file list, and `start-mcp.sh`
     is a bare `exec .venv/bin/python -m app.mcp_tools.vendor_server` with no version check.

  Consequence is if anything stated conservatively: the process does not "raise one frame up" —
  it dies during module import at interpreter start, before FastMCP is constructed, so
  `start-mcp.sh`'s `exec` returns non-zero and nothing restarts it. `pytest` does not merely fail
  one module: the collection error **interrupts the whole run**, so `tests/test_hpc.py` and
  `tests/test_eln.py` never execute — a fresh checkout has zero passing tests, not 28.

  Scope check that keeps it at high rather than critical: the main FastAPI mock (HPC launcher +
  ELN) is unaffected — `import app.main` succeeds under mcp 2.0.0, so `start.sh` still runs. What
  is dead is the one component that exercises Chemclaw3's `HttpMcpServerSpec` path, plus the
  entire test suite. An unbounded `>=` on a library that renamed its entry point is the root cause
  and the fix (`mcp>=1.2,<2`) is correct.
