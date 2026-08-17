# Round 1 — `servers/safety`, `servers/props` — security & hardening

Repo: `/workspace/chemclaw3-mcp`. All measurements below were run against the real code in this
repo with `uv run`; scripts are in `/tmp/audit/`.

Two structural facts underlie most of what follows, so they are stated once:

1. **A sync MCP tool runs on the event loop.** `mcp/server/fastmcp/utilities/func_metadata.py:93-96`
   — `if fn_is_async: return await fn(...) else: return fn(...)`. There is no thread hop. Every
   `props` tool and `safety.ich_impurity_limit` are `def`, not `async def`.
2. **The only input bound on the wire is `DEFAULT_MAX_REQUEST_BYTES = 1_000_000`**
   (`packages/mcp_server_kit/src/mcp_server_kit/app.py:55`). There is no cap on response size, on
   any string argument's length, on list length outside `safety`'s `MAX_COMPONENTS`, and no
   concurrency limit.

---

## `ich_impurity_limit` runs unbounded RDKit canonicalization on the event loop

- **Severity**: high
- **Location**:
  - `servers/safety/src/chemclaw_mcp_safety/tools.py:129-156` (`ich_impurity_limit`, declared `def`)
  - `servers/safety/src/chemclaw_mcp_safety/engine/ich.py:286-292` (`impurity_limit` → `resolve_compound_name`)
  - `servers/safety/src/chemclaw_mcp_safety/engine/reagents.py:137-140` (`require_canonical_smiles(name)` on the raw query)
  - `servers/safety/src/chemclaw_mcp_safety/engine/chem.py:79-93` (`Chem.MolFromSmiles` + `Chem.MolToSmiles`)
- **Trigger**: one authenticated `tools/call` with
  `{"name":"ich_impurity_limit","arguments":{"substance":"C"*20000}}` — a 20 KB request body, 2% of
  the 1 MB cap. Any string that misses both ICH tables and the reagent synonym table falls through
  to `require_canonical_smiles`, which hands it to RDKit.
- **Consequence**: the whole `safety` process stops serving. It is a single-process, single-loop
  server, so while this runs, every other chemist's `screen_hazards` call, every
  `screen_genotoxic_alerts` call and the kubelet's `/healthz` probe are all stalled behind it.
  Measured: 15 KB → 4.9 s frozen; 20 KB → still running after 300 s. `connector.yaml`'s
  `request_timeout: 30` bounds the *caller's* wait, not the server's work, so the caller gives up
  and the server stays wedged. Reachable without privilege: `ich_impurity_limit` is in
  `read_only:` (usable before any plan approval) and its `substance` argument is free text the model
  copies out of the chat turn.
- **Evidence**: the two docstrings that assert this is safe are both wrong.

  `servers/safety/src/chemclaw_mcp_safety/tools.py:30-32`:
  > "`ich_impurity_limit` is a dictionary lookup over two small tables and needs neither — and it is
  > the one tool here whose synchrony is a measured decision rather than a house style."

  `servers/safety/tests/test_event_loop_offload.py:20-23`:
  > "`ich_impurity_limit` has no test here on purpose: it is a dictionary lookup over an index built
  > once per process, it takes no `asyncio.to_thread` hop, and a test asserting it ran *on* the loop
  > would pin an implementation detail rather than a property worth keeping."

  It is not a dictionary lookup. `ich.py:289` calls `resolve_compound_name(substance)`, which at
  `reagents.py:138` calls `require_canonical_smiles(name)` — `MolFromSmiles` then `MolToSmiles` on
  the caller's raw string.

  Engine-level cost (`/tmp/audit/t4.py`, parse vs. write split):

  ```
  len=5000  parse=0.011s write=0.513s rss=61MB
  len=10000 parse=0.020s write=2.175s rss=77MB
  len=15000 parse=0.027s write=4.906s rss=99MB
  len=20000 TIMEOUT>300s
  len=25000 TIMEOUT>300s
  ```

  The cost is in `MolToSmiles` (canonical ranking), and there is a cliff between 15 k and 20 k.

  End-to-end over HTTP against the real `chemclaw_mcp_safety.app:app` behind uvicorn, with a
  `/healthz` prober polling every 50 ms (`/tmp/audit/e2e2.py`):

  ```
  tools/call: 4.89s | /healthz probes: n=11 max=4.864s median=0.0022s
  ```

  `max=4.864s` on the unauthenticated liveness route is the event loop being held for the full
  duration of one tool call.
- **Fix**: two changes, both needed.
  1. Make `ich_impurity_limit` `async` and wrap the body in `asyncio.to_thread`, exactly as the two
     screens beside it already do — and delete the docstring sentence claiming it does not need to.
  2. Bound the input before it reaches RDKit. `substance` is a chemical name, a symbol or a SMILES;
     nothing legitimate exceeds a few hundred characters. Add a length guard in
     `chem.require_molecule` (a `MAX_SMILES_LENGTH`, refused as `InvalidSmilesError` so the message
     reaches the model) so every entry point in this server inherits it, and add the corresponding
     test to `test_event_loop_offload.py` that the docstring currently argues against.

---

## `MAX_COMPONENTS` bounds the component count, not the response — 6 KB in, 29.6 MB out

- **Severity**: high
- **Location**:
  - `servers/safety/src/chemclaw_mcp_safety/engine/screen.py:61-74` (the `MAX_COMPONENTS` comment)
  - `servers/safety/src/chemclaw_mcp_safety/engine/screen.py:421-436` (the pair cross-product)
  - `servers/safety/src/chemclaw_mcp_safety/engine/genotox.py:201-217` (the identical formation-pair loop)
- **Trigger**: 64 components — exactly at the cap, so nothing is refused — each a dot-disconnected
  SMILES that matches *both* sides of every pair rule in `rules.yaml`:

  ```
  ("C"*(i+1)) + "O." + "OO.[H-].[BH4-].[N-]=[N+]=[N-].ClCCl.CN(C)C=O.CC(=O)C.NN"   for i in 0..63
  ```

  Each fragment satisfies one side of one rule: `OO` (peroxide), `[H-]` and `[BH4-]` (hydrides),
  `[N-]=[N+]=[N-]` (azide anion), `ClCCl` (chlorinated), `CN(C)C=O` (DMF), `CC(=O)C` (ketone),
  `NN` (hydrazine). The `C`-prefix makes the 64 strings distinct so `parse_components`'
  dedup does not collapse them.
- **Consequence**: 6.1 KB request → 29.6 MB response and 1.78 s of blocked event loop. Sustained,
  this is both a bandwidth amplifier against whatever sits in front of the server and a memory
  amplifier inside it — the flag list, the pydantic dump and FastMCP's duplicate `structuredContent`
  + text block all live in RSS at once. Downstream it is worse than bandwidth: 24,192 hazard flags
  land in the agent's context window, which is not a result any model can report.
- **Evidence**: the comment that justifies the number is measurably wrong.
  `screen.py:70-72`:
  > "64 is far above any real reaction … and bounds the worst case to ~1,000 pair flags and
  > single-digit milliseconds."

  Measured (`/tmp/audit/pairs2.py`, engine only):

  ```
  components=64 request=5996B  0.178s
    total flags=24384  pair flags=24192  (comment claims '~1,000 pair flags')
    response=13842570B  amplification=2309x
  ```

  24,192 = 6 pair rules × 64 × 63. The comment's ~1,000 assumes one rule and one direction.

  End-to-end over HTTP (`/tmp/audit/e2e3.py`), with the `/healthz` prober:

  ```
  safety.screen_hazards, 64 crafted components
    request=6,100B (cap 1,000,000)  duration=2.03s  response=29,563,649B  amplification=4846x
    concurrent /healthz: n=12 max=1.779s  <-- event loop blocked this long
  ```

  Note the loop still blocks for 1.78 s even though the *screen* runs in `asyncio.to_thread`: the
  `asyncio.to_thread` hop covers the SMARTS matching, not the serialization of what it returned.
  `genotox` has the same shape with one formation pair (64 × 63 = 4,032 alerts).
- **Fix**: cap the *output*, not only the input. In `screen_reaction` and
  `screen_genotoxic_alerts`, bound the number of pair flags emitted per rule (a few dozen is beyond
  any real reaction) and, when truncated, say so in `verdict` — silence there would re-create the
  "an absent flag is not a clean screen" failure this module exists to prevent. Correct the
  `MAX_COMPONENTS` comment to the measured figure, and lower the cap: at 16 components the same
  crafted payload yields 6 × 16 × 15 = 1,440 flags, which is what the comment already believes it
  is buying. Also validate `CHEMCLAW_SAFETY_MAX_COMPONENTS` (`screen.py:74`) — it is
  `int(os.environ.get(...))` with no bounds, so an env typo silently removes the cap entirely.

---

## `props.compare_solvents` takes an unbounded list — 700 KB in, 81.6 MB out, 15 s of frozen server

- **Severity**: high
- **Location**: `servers/props/src/chemclaw_mcp_props/tools.py:401-440` (`compare_solvents`,
  declared `def`); the synchrony rationale at `servers/props/src/chemclaw_mcp_props/tools.py:12-16`
- **Trigger**: one `tools/call` with `{"name":"compare_solvents","arguments":{"names":["dcm"]*100000}}`
  — 700 KB, under the 1 MB cap. `names` has no length limit anywhere; every resolved name expands
  into a full `ComparisonRow`.
- **Consequence**: the entire `props` server is frozen for ~15 s per request. Every props tool is
  synchronous, so nothing else on the process runs — not another chemist's `solvent_properties`
  call, not `/healthz`. Response is 117× the request. No authentication of *scale* exists: the
  bearer token is per-server, so any holder can repeat this.
- **Evidence**: the module docstring is the claim that fails.
  `servers/props/src/chemclaw_mcp_props/tools.py:12-16`:
  > "Everything here is a dictionary lookup and a few floating-point operations — microseconds — so
  > the tools are synchronous. That is a measured statement about *this* server rather than a house
  > style … Nothing here does that; a server that starts doing real work must revisit this."

  True per name; false per call, because nothing bounds the number of names.

  Engine-level scaling (`/tmp/audit/props1.py`):

  ```
  n=   1000 req=     7011B    0.004s  resp=     316586B  amplification=  45.2x
  n=  10000 req=    70011B    0.044s  resp=    3160586B  amplification=  45.1x
  n= 100000 req=   700011B    0.811s  resp=   31600586B  amplification=  45.1x
  ```

  End-to-end over HTTP against `chemclaw_mcp_props.app:app` (`/tmp/audit/e2e3.py`):

  ```
  props.compare_solvents, 100k names
    request=700,117B (cap 1,000,000)  duration=15.04s  response=81,601,345B  amplification=117x
    concurrent /healthz: n=11 max=14.911s  <-- event loop blocked this long
  ```

  (81.6 MB rather than 31.6 MB because FastMCP emits the same payload twice — once as
  `structuredContent`, once as a text content block.)
- **Fix**: bound `names` in `compare_solvents` (the table holds 44 solvents; a comparison of more
  than ~32 names is not a comparison) and refuse over the bound with a `ValueError` naming the
  limit, the same shape `require_screenable_size` uses in `safety`. Cap `unknown` the same way so a
  list of 100k typos cannot be echoed back either. While there, clamp `top_n` in
  `solvent_swap_candidates` to `>= 0`: `selection.py:166` does `scored[:top_n]`, so a negative
  `top_n` silently returns *all but the last N* rather than N.

---

## No bound on a single SMILES, and the work is not cancelled when the caller gives up

- **Severity**: medium
- **Location**:
  - `servers/safety/src/chemclaw_mcp_safety/tools.py:85-87` (`screen_hazards` → `asyncio.to_thread`)
  - `servers/safety/src/chemclaw_mcp_safety/engine/chem.py:74-82` (`require_molecule` — validates
    shape, never length)
- **Trigger**: `{"name":"screen_hazards","arguments":{"smiles":["C"*15000]}}` — a 15 KB request —
  with the client abandoning the connection after 1 s.
- **Consequence**: `MAX_COMPONENTS` bounds how many structures a call may carry but nothing bounds
  how big one is, so the cost per request byte is unbounded. Because `asyncio.to_thread` cannot be
  interrupted, a client timeout (`connector.yaml` sets `request_timeout: 30`) frees the caller and
  not the server. anyio's default thread limiter is 40, so ~40 concurrent 20 KB requests — 800 KB of
  traffic total — occupy every worker thread for hundreds of seconds each with no way to reclaim
  them, while the `screen_hazards` tool that a chemist actually needs queues behind them.
- **Evidence** (`/tmp/audit/cancel.py`, real server, client aborts at t=1.0 s, then process CPU is
  sampled once a second):

  ```
  request bytes: 15120
  client aborted after 1.0s (TimeoutError) -- connection closed
    t=+1s  process CPU since abort:   4.92s
    t=+2s  process CPU since abort:   4.92s
    ...
  ```

  4.92 CPU-seconds were spent on a request whose client was gone — the screen ran to completion. At
  20,000 characters the same call costs >300 CPU-seconds (see the first finding's table); the
  request grows 33% and the cost grows 60×.
- **Fix**: add a `MAX_SMILES_LENGTH` check inside `chem.require_molecule` so every caller in the
  server — both screens, `parse_components`, and the ICH resolver — inherits one refusal, and assert
  it in `servers/safety/tests/test_tools.py` beside the existing `MAX_COMPONENTS` tests. A few
  hundred characters admits every real structure; the largest SMILES in the vendored reagent table
  is far below that.

---

## The vendored-corpus checksum is self-attesting, so it detects accidents and not substitution

- **Severity**: low
- **Location**:
  - `servers/safety/src/chemclaw_mcp_safety/engine/screen.py:55-59` (`RULES_DIR`, and the comment
    beside it), `screen.py:14-19` (the "the checksum is the point" paragraph),
    `screen.py:210-249` (`read_table`)
  - the verification itself: `packages/mcp_server_kit/src/mcp_server_kit/datasets.py:94-99`
- **Trigger**: any write to the corpus directory inside the image or a mount — edit `rules.yaml`,
  then recompute `sha256` into the `dataset.json` sitting in the same directory.
- **Consequence**: the hazard table can be shortened and the server will load it, screen with it,
  and answer `"No rule in the hazard table matched. This is not a safety assessment."` — the exact
  reading this module is built around never being wrong about. `read_table` cannot tell a
  re-stamped table from the reviewed one, because the manifest is unsigned and lives beside the file
  it attests.
- **Evidence**: `datasets.py:94-99` compares `_digest(records_path)` against
  `manifest["sha256"]` read from `directory / "dataset.json"` — the same directory. Demonstrated
  (`/tmp/audit/tamper.py`): copy the corpus, drop the single `organic-azide` rule, re-stamp the
  manifest, point `RULES_DIR` at the copy:

  ```
  before: ['organic-azide']
  after : []
  verdict: No rule in the hazard table matched. This is not a safety assessment.
  ```

  The docstring claim being checked, `screen.py:16-18`:
  > "…the checksum is the point: a swapped-in table would be a different set of claims wearing the
  > same citations."

  It holds against the *accidental* corruption `datasets.py:13-15` names ("truncated by a bad COPY")
  and not against a deliberate one. Kept at **low** because the shipped image is the mitigation:
  `servers/safety/Containerfile` installs as root and runs as UID 1001, so the corpus is not
  writable by the serving process, and reaching it requires a build-pipeline or registry compromise
  that already implies worse.
- **Fix**: either sign the manifest (record the expected digest somewhere the running image cannot
  rewrite — a build-time constant compiled into the package, or a detached signature verified
  against a public key baked in), or downgrade the prose so the control is not credited with more
  than it does. The cheapest honest version: pin the four `sha256` values as module constants in
  `screen.py` / `genotox.py` / `ich.py` and pass them to `read_table`, so a substituted corpus has
  to also modify installed Python to pass. Add `readOnlyRootFilesystem: true` to the deployment
  alongside `servers/safety/deploy/networkpolicy.yaml` while there.

---

## What was checked and found sound

Recorded so the next round does not re-plough it:

- **Bearer auth** (`packages/mcp_server_kit/src/mcp_server_kit/auth.py:49-83`) — fails closed on an
  unset/empty `token_env`, compares with `hmac.compare_digest` on bytes (timing-safe, and no
  `TypeError` on non-ASCII), and is middleware rather than a route dependency so the `/` mount
  cannot bypass it. `OPEN_PATHS` is exact-match, so `/healthz/` and `//metrics` fail closed.
  Verified over HTTP: `/mcp` without the header is 401.
- **`/metrics` is unauthenticated but carries nothing** — `generate_latest(REGISTRY)` on a server
  that registers no custom metric returns only `python_gc_*`/`process_*` defaults (1,896 bytes,
  checked live). No caller identity, no argument values, no label carrying user data.
- **No injection surface.** There is no SQL, no shell, no template, no `eval`, no dynamic import,
  no user-controlled filesystem path, and no deserialization of caller data beyond pydantic. Corpus
  YAML goes through `yaml.safe_load` (`screen.py:237`). `selection.py:144`'s
  `int(max_ich_class)` on a hostile string raises a plain `ValueError` that is reported as a tool
  error (checked: `max_ich_class="'; DROP--"` → `invalid literal for int()`).
- **No SSRF / egress.** Neither server holds an HTTP client; `mcp_server_kit.egress` patches
  `socket.connect`/`connect_ex` and is armed by default (`MCP_EGRESS_GUARD=on` in both
  Containerfiles), and both `deploy/networkpolicy.yaml` files declare `Egress` in `policyTypes`
  with an empty `egress: []` (deny-all, DNS included).
- **No secret reaches a log or a response.** `CallerLogMiddleware` logs path/actor/session only;
  `BearerAuthMiddleware` logs the refusal without the offered credential; `_sanitize_tool_errors`
  (`app.py:88-110`) replaces non-`ValueError` exception text with a generic notice. The one
  information leak that survives is absolute install paths inside `SafetyRulesError` messages
  (`screen.py:239-241`), which is disclosed only to an already-authenticated caller and is not worth
  a finding on its own.
- **Numeric edge inputs are handled.** JSON `1e400` for `pressure_mbar` is parsed to `None` by
  pydantic and rejected before `boiling_point_at` sees it, so the `pressure_mbar <= 0` guard cannot
  be bypassed with an infinity; `boiling_point_at`'s bisection is a fixed 200 iterations
  (`correlations.py:170`) and cannot be made to loop.
