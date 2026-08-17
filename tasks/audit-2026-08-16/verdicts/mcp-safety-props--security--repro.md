# Refutation pass — `mcp-safety-props--security.md`, lens: does it reproduce?

Repo under test: `/workspace/chemclaw3-mcp` @ `9217011`. Nothing from the reporter's `/tmp/audit/`
was run or read; every script below is mine, under `/tmp/v/`. Two harnesses:

- `/tmp/v/e2e.py` — uvicorn in the *same* process as the caller (this is what I believe the
  reporter used; it under-reports loop stalls because the client's own reads interleave).
- `/tmp/v/e2e_sep.py` — uvicorn as a **subprocess**, caller and `/healthz` prober (50 ms) in this
  process, and it reports the server's exit code afterwards. This is the harness the numbers below
  come from, because it is the only one where a `/healthz` latency is unambiguously the *server's*
  loop being held.

In-scope: the three findings marked **high**. All three reproduce. Two corrections to the record
follow, one of which makes a finding worse than filed.

---

## `ich_impurity_limit` runs unbounded RDKit canonicalization on the event loop

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (the crash below is borderline critical)
- **What I did**

  Re-derived the path from source, not from the finding. `tools.py:130` →
  `ich.impurity_limit` (`ich.py:276`) → miss on `_fold(substance)` → `resolve_compound_name`
  (`reagents.py:121`) → miss on `_normalize(name)` → `require_canonical_smiles(name)`
  (`reagents.py:138`) → `Chem.MolFromSmiles` + `Chem.MolToSmiles` (`chem.py:85`). Every cited
  line number and symbol is real and current.

  Engine-level (`/tmp/v/t1.py`, `uv run` in `servers/safety`):

  ```
  len=1000  t=0.030s rss=98MB   limit=None
  len=2500  t=0.126s rss=101MB  limit=None
  len=5000  t=0.514s rss=106MB  limit=None
  len=7500  t=1.159s rss=113MB  limit=None
  len=10000 t=2.130s rss=122MB  limit=None
  len=15000 t=4.901s rss=144MB  limit=None
  ```

  Within a few percent of the reporter's table at every point (they had 0.524 / 2.195 / 4.933).

  Real server, separate process, `/healthz` prober (`/tmp/v/e2e_sep.py safety ich_impurity_limit`,
  `substance = "C"*15000`):

  ```
  req=15125B dur=4.94s resp=60621B amp=4x
  healthz n=12 max=4.902s median=0.0031s
  server alive after: True
  ```

  One 15 KB request — 1.5 % of the 1 MB body cap — held the loop for 4.90 s. The unauthenticated
  liveness route was stalled for the whole of it.

- **Why**: the mechanism and the consequence are exactly as filed. Two corrections, neither of
  which saves the finding:

  1. **The finding's structural fact #1 is wrong about this tool.** `tools.py:130` is
     `async def ich_impurity_limit`, not `def` — the finding's "Every `props` tool and
     `safety.ich_impurity_limit` are `def`, not `async def`" is half false, and its fix step 1
     ("make `ich_impurity_limit` `async`") is already done. This changes nothing measured: the body
     is `return impurity_limit(substance)`, a blocking call made directly inside a coroutine, so
     the loop is held either way — which is what the 4.902 s `/healthz` stall shows. Only the
     *missing* half of the fix (`asyncio.to_thread`) is real. (The `props` half of fact #1 *is*
     correct: all six `@server.tool()` functions in `servers/props/.../tools.py` are `def`.)

  2. **"20 KB → still running after 300 s" does not reproduce, and the truth is worse.** At
     `len ≥ 19000` `Chem.MolToSmiles` does not hang, it **segfaults**. Isolated
     (`/tmp/v/t3.py`): `n=18000 write=7.056s exit=0`; `n=19000` and `n=20000` both `exit=139`
     (SIGSEGV), with parse succeeding first (`parse=0.036s atoms=20000`). End to end against the
     real server as its own process:

     ```
     req=20125B  call failed: RemoteProtocolError('Server disconnected without sending a response.')
     server alive after: False  rc= -11
     ```

     `rc=-11` is the `safety` server process being killed by SIGSEGV. So a single 20 KB
     `tools/call` does not wedge the server — it **kills** it, taking every in-flight screen with
     it and requiring a restart. Reachable exactly as the finding says: `ich_impurity_limit` is in
     `read_only:` in `servers/safety/connector.yaml` (verified) and `substance` is free text.

  The finding is solid. What the reporter missed: the failure at the cliff is a remote process
  crash, so the fix's length guard is not a latency optimization — it is what stands between an
  oversized string and a killed pod. Bound it well below 18 000 (a few hundred, as the fix says).

---

## `MAX_COMPONENTS` bounds the component count, not the response — 6 KB in, 29.6 MB out

- **Verdict**: CONFIRMED
- **Severity I would assign**: high
- **What I did**

  I derived the payload myself from `rules.yaml` rather than trusting the finding's string. Parsed
  the corpus: `incompatible_pairs` has **6** entries (`oxidizer-with-reductant`,
  `azide-with-dichloromethane`, `saline-hydride-with-chlorinated-solvent`,
  `hydride-with-dipolar-aprotic`, `peroxide-with-ketone`,
  `complex-hydride-with-chlorinated-solvent`). Read each SMARTS and confirmed the fragment set
  `O.OO.[H-].[BH4-].[N-]=[N+]=[N-].ClCCl.CN(C)C=O.CC(=O)C.NN` satisfies *both* sides of all six,
  so `matches = [(a,b) for a in left for b in right if a != b]` (`screen.py:426`) is the full
  `n(n-1)` for every rule.

  Engine only (`/tmp/v/pairs.py`):

  ```
  n=16 MAX=64 req=1228B 0.031s total_flags=1488  pair_flags=1440  resp=771977B    amp=629x
  n=64 MAX=64 req=6100B 0.172s total_flags=24384 pair_flags=24192 resp=13842570B  amp=2269x
  ```

  24 192 = 6 × 64 × 63, and `resp=13842570B` is the reporter's figure to the byte.

  Real server, separate process (`/tmp/v/e2e_sep.py safety screen_hazards`):

  ```
  req=6100B dur=2.06s resp=29563649B amp=4846x
  healthz n=16 max=1.682s median=0.0035s
  server alive after: True
  ```

- **Why**: reproduces on an independently constructed payload, at the same magnitudes, and the
  comment at `screen.py:70-72` ("bounds the worst case to ~1,000 pair flags and single-digit
  milliseconds") is wrong by 24× on flags and by ~200× on time at the process boundary. Nothing
  refuses the call — 64 is *at* the cap, so `require_screenable_size` passes it. The finding's
  observation that the `asyncio.to_thread` hop does not cover serialization is confirmed by the
  1.68 s `/healthz` stall on a screen whose SMARTS matching took 0.17 s. The finding's own
  suggested cap of 16 also checks out arithmetically: I measured 1 440 pair flags there, which is
  what the comment already claims to be buying.

  Nothing here is exaggerated; if anything the 29.6 MB understates the agent-side cost, since it is
  24 384 flags landing in a context window.

---

## `props.compare_solvents` takes an unbounded list — 700 KB in, 81.6 MB out, 15 s of frozen server

- **Verdict**: CONFIRMED
- **Severity I would assign**: high
- **What I did**

  `tools.py:402` is `def compare_solvents(names: list[str])` — sync, no bound on `names`, no bound
  on `unknown`. Confirmed by reading the body: one `ComparisonRow` per resolved name, appended
  unconditionally.

  Real server as its own process, 100 000 × `"dcm"` (`/tmp/v/e2e_sep.py props compare_solvents`):

  ```
  req=700117B dur=14.83s resp=81601345B amp=117x
  healthz n=15 max=14.469s median=0.0028s
    top5=[14.469, 0.076, 0.022, 0.0045, 0.0042]
  server alive after: True
  ```

  Request 700 117 B, 70 % of the 1 MB cap — accepted. Response 81 601 345 B. One `/healthz` probe
  waited 14.47 s; the rest are sub-10 ms, i.e. the loop was held in one continuous block for
  essentially the whole call, not contended in slices.

- **Why**: reproduces at the reporter's numbers (they had 15.04 s / 81 601 345 B / max 14.911 s;
  I get 14.83 s / 81 601 345 B / max 14.469 s — the response size is byte-identical).

  One methodological note that *strengthens* rather than weakens the finding: my first run used a
  same-process harness and reported `healthz max=2.550s`, which would have looked like a partial
  refutation of the 14.9 s figure. It was an artifact of the caller sharing the loop with the
  server. Moving uvicorn into its own process — the deployed shape — produced 14.469 s. So the
  reporter's stall number is right, and anyone re-checking this with an in-process harness will get
  a misleadingly small one.

  The module docstring at `tools.py:12-16` ("microseconds — so the tools are synchronous … a server
  that starts doing real work must revisit this") is, as filed, true per name and false per call.

---

## Summary

3 in scope, 3 CONFIRMED, 0 refuted. Two record corrections:

- `ich_impurity_limit` is already `async def`; only the `asyncio.to_thread` half of that fix is
  outstanding. The finding's structural fact #1 should be amended.
- The 20 KB ICH case is a **SIGSEGV that kills the server process** (`rc=-11`), not a >300 s hang.
  That makes the input-length guard the load-bearing half of that fix, and makes this the most
  severe of the three.
