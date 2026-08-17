# mcp-calc — correctness: reproduction verdicts

Lens: **does it actually reproduce?** Scope: critical + high only.

The findings file contains **no critical finding and exactly one high finding**. The remaining five
are marked medium/low and are out of scope; no verdict is rendered on them.

Working-tree check first: `git status --porcelain` in `/workspace/chemclaw3-mcp` is **empty**,
`git diff HEAD -- servers/calc/src/chemclaw_mcp_calc/tools.py` is empty, and `grep -rn MUTANT
servers/calc/src` finds nothing. Everything below was measured against clean `HEAD`
(`9217011`), not a mutated checkout.

---

## `predict_site_reactivity` truncates the ranking here, so the payload the caller re-ranks is missing atoms

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (unchanged)

### What I did

I did not run the reporter's `/tmp/probe/p7.py`. I wrote my own probes from the source.

**1. The server function, ibuprofen, tool defaults** (`/tmp/myprobe/a.py`, run with
`uv run python` inside `/workspace/chemclaw3-mcp/servers/calc`). It calls
`tools.predict_site_reactivity(smiles=...)` with no `mode` and no `top_n`, and separately calls
`xtb_props.compute_fukui(*xtb_props.fukui_inputs(SM), "electrophilic")` to get the untruncated
ranking for comparison:

```
configured top_n default: 15
total_atoms: 33 served: 15
full sites: 33
TRUE nucleophilic top5:
  #1 idx=11 O f_plus=0.1393 served=True
  #2 idx=12 O f_plus=0.0694 served=True
  #3 idx=4 C f_plus=0.0678 served=True
  #4 idx=10 C f_plus=0.0674 served=False
  #5 idx=32 H f_plus=0.0621 served=True
SERVED re-sorted top5:
  #1 idx=11 O f_plus=0.1393
  #2 idx=12 O f_plus=0.0694
  #3 idx=4 C f_plus=0.0678
  #4 idx=32 H f_plus=0.0621
  #5 idx=24 H f_plus=0.0618
```

My numbers are identical to the reporter's to the fourth decimal, derived independently.

**2. Atom 10's identity, settled against the structure the server actually built** — not against the
input SMILES string, since `fukui_inputs` canonicalizes (`structure.smiles` is
`CC(C)Cc1ccc(C(C)C(=O)O)cc1`, a different atom order than the finding's input string). Reading the
neighbours off that molecule with RDKit:

```
10 C [(8, 'C', 'SINGLE'), (11, 'O', 'DOUBLE'), (12, 'O', 'SINGLE')]
11 O [(10, 'C', 'DOUBLE')]
12 O [(10, 'C', 'SINGLE'), (30, 'H', 'SINGLE')]
```

Atom 10 is a carbon bearing one C, one double-bonded O and one single-bonded O that itself carries
an H — i.e. `–C(=O)OH`, the carboxyl carbon of ibuprofen (C13H18O2, 33 atoms with hydrogens, which
matches `total_atoms: 33`). The identification in the finding is correct.

**3. Over the real MCP socket, not just the Python function.** The reporter only exercised the
in-process coroutine. I dropped a probe test into the server's own `test_server.py` harness (real
uvicorn on loopback, real MCP handshake, real bearer auth) and read `structuredContent`:

```
WIRE total_atoms= 33 len(sites)= 15
carboxyl C (idx 10) present on wire? False
WIRE top_n=100 len(sites)= 33
```

So the truncated payload is genuinely what a client receives; nothing downstream of the tool body
restores it.

**4. The cache-key collision** (`/tmp/myprobe/b.py`), three argument sets through the server's own
`calculation_key`:

```
{'smiles': IBU}                                 -> xtb.fukui@…:de9b0409ff554846:b41312b0cdc59ab7
{'smiles': IBU, 'top_n': 100}                   -> xtb.fukui@…:de9b0409ff554846:b41312b0cdc59ab7
{'smiles': IBU, 'top_n': 3, 'mode': 'nucleophilic'} -> xtb.fukui@…:de9b0409ff554846:b41312b0cdc59ab7
```

One key for all three. This is deliberate — `engine/identity.py:157` states "`mode` and `top_n` are
accepted and do not enter the key", and `identity.py:318` lists them in `accepts`.

**5. The core-repo caller**, read directly at
`/home/user/Chemclaw3/src/chemclaw/connectors/calc/server/tools.py:782-797`. It sends
`{"smiles": smiles}` and nothing else, with the comment asserting *"the row holds every atom, so
asking for more sites re-slices a cached result"*. `cached_remote`
(`src/chemclaw/connectors/calc/remote.py:317-352`) passes those arguments verbatim to both
`remote_key` and `remote_compute`, and `cached_compute` persists whatever `remote_compute` returned
— i.e. the 15-site payload.

### Why

Every link in the chain reproduces on my own scripts, and the cited code is real and current
(`tools.py:329-334`, `limit = top_n if top_n > 0 else settings.xtb_fukui_top_n`;
`config.py:77 xtb_fukui_top_n: int = 15`). The consequence is exactly as stated: the server sorts
33 sites by `f_minus`, keeps 15, and the `f_plus`/`f_zero` columns it still ships describe a set
that has already been filtered by a *different* index. Following the tool's own docstring
instruction (`tools.py:313-316`, "Read the other rankings off `f_minus`/`f_plus`/`f_zero` rather
than calling again") on ibuprofen loses the carboxyl carbon — the archetypal nucleophilic-addition
site, and the very example the same docstring names. Nothing raises.

Three things I would add that make it worse than reported:

- **No test in this server can catch it.** The only molecules exercised through the Fukui path are
  ethanol (`CCO`, 9 atoms) in `test_engine.py:173` / `test_calc_version.py:83`, and toluene
  (`Cc1ccccc1`, exactly 15 atoms) in `test_engine.py:151`. Both are at or below the limit, so the
  slice is a no-op in every one. The single wire test that touches this tool
  (`test_server.py:163`) passes `top_n: 3` *explicitly*, so it exercises the truncation without
  ever comparing `len(sites)` to `total_atoms`. The trivially available invariant
  `total_atoms == len(sites)` when `top_n` is unset exists nowhere.

- **The two repos contradict each other inside one model.** The core repo's
  `SiteReactivityResult` docstring (`src/chemclaw/science/calc/models.py:344`) says `sites` is
  "truncated to the most susceptible `len(sites)` of `total_atoms`" — the model *knows* truncation
  is possible — while the caller forty lines away in `connectors/calc/server/tools.py:794-797`
  asserts the row holds every atom and builds its `top_n` contract on that. One of the two is
  wrong, and it is the one the caller depends on.

- **The collision is not merely theoretical for the caching layer.** Because `top_n` is outside the
  key by design, a row populated by *any* client's default call is served to a later `top_n=100`
  request through the core cache. The server itself would answer 33 (measured above), but a cache
  hit never reaches the server — the same argument `ranked_for`'s docstring already makes for
  `mode`, applied to `top_n`, where it was not applied.

The one line of the finding I would soften: "`top_n=100` … re-slices a 15-element list and still
returns 15" is true of the **core** wrapper on a cache hit, and is *false* of the MCP server itself,
which returns all 33 for `top_n=100` (measured over the wire). The finding does attribute this
correctly to the core side, so this is a precision note rather than a defect in the claim.

The proposed fix is the right shape. If `top_n` is kept for wire compatibility it must enter the
key, but deleting it is better, since the row that gets cached should be the complete one and the
core wrapper already owns presentation slicing.
