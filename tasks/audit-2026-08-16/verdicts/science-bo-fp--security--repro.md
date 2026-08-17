# Refutation pass — `science-bo-fp--security.md`, lens: does it actually reproduce?

In scope: the three findings marked **high**. The four marked medium/low are ignored per the brief.

Every script below is mine, written from the source; none of the reporter's scaffolding was run.
All runs are `uv run` in this checkout at `HEAD = 577b88c`, with Docker/Postgres up. The six source
files cited by the three findings are byte-identical to `HEAD` (`git diff --quiet HEAD -- <file>`
clean for each), so no other agent's live edit is contaminating the result.

---

## A NaN `threshold` escapes `find_matches`' clamp and turns a populated index into a "genuine negative result"

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  1. `/tmp/verif/f1_clamp.py` — the clamp expression at `store.py:475` verbatim, then the real
     `find_similar_molecules` over an `InMemoryFingerprintStore` holding five molecules:

     ```
     clamp(nan) -> nan  isnan=True
     clamp(-inf) -> 0.0 ; clamp(inf) -> 1.0 ; clamp(-1.0) -> 0.0 ; clamp(2.0) -> 1.0
     indexed: 5 is_empty: False
     threshold=0.2: hits=3 index_empty=False scan_truncated=False hits_truncated=False
       VERDICT: 3 indexed molecule(s) matched this query.
     threshold=nan: hits=0 index_empty=False scan_truncated=False hits_truncated=False
       VERDICT: No indexed molecule matched this query. The molecule fingerprint index holds
                records and was searched, so this is a genuine negative result.
     ```

  2. `/tmp/verif/f1_mcp.py` — the transport layer, against the MCP SDK's own message model rather
     than a hand-rolled parse. A JSON-RPC `tools/call` frame carrying the **bare** `NaN` literal is
     accepted by `mcp.types.JSONRPCMessage.model_validate_json` and yields `float('nan')`:

     ```
     (a) bare NaN literal PARSED by SDK -> nan isnan: True
     (b) string parsed to: 'nan'
     ```

  3. `/tmp/verif/f1_e2e.py` — the real tool through FastMCP's argument validation
     (`server.call_tool("similar_molecules", …)` on `connectors/molfp/server/tools.py`, store swapped
     for the in-memory one). All three spellings pass validation and all three produce the sentence:

     ```
     threshold=0.2   : 3 hits
     threshold='nan' : {"hits": [], "index_empty": false, "scan_truncated": false,
                        "hits_truncated": false, "verdict": "No indexed molecule matched this
                        query. … this is a genuine negative result."}
     threshold=nan   : same
     threshold='NaN' : same
     ```

  4. `/tmp/verif/f1_pg.py` — the production `PostgresFingerprintStore` against live Postgres
     (`make up`), four rows inserted under the current definition, rows deleted afterwards:

     ```
     count: 4 is_empty: False
     threshold=   0.3 -> 3 hits, index_empty=False
     threshold=   nan -> 0 hits, index_empty=False
         No indexed molecule matched this query. … this is a genuine negative result.
     ```

- **Why**

  The mechanism is exactly as described and I could not find anything upstream that prevents it.
  `min(max(t, 0.0), 1.0)` is a total clamp for every float *except* NaN, because both `max` and
  `min` reject the challenger on a `False` comparison and return the incumbent — so NaN passes
  through untouched while `±inf` and out-of-range finites are clamped correctly. NaN then lands in
  `1 - (bits <%> …) >= %(threshold)s`, and in Postgres NaN sorts above every float, so no row can
  satisfy the predicate. The in-memory backend's `m.similarity >= threshold` fails for the ordinary
  IEEE reason. Both backends return zero rows from a populated index.

  Reachability is real, not theoretical: the argument survives FastMCP's pydantic validation
  (`threshold: float | None`, no `ge`/`le`) in three separate spellings, one of which is a plain JSON
  string. The three flags the payload carries to keep the model honest — `index_empty`,
  `scan_truncated`, `hits_truncated` — are all correctly `False`, so the `verdict` computed field
  emits the strongest possible negative assertion. That is the single sentence this module exists to
  get right.

  Two things I add that the reporter did not have:

  - The asymmetry is sharper than stated. The *same quantity* from config is bounded:
    `config/fingerprints.py:33` declares `fingerprint_similarity_threshold: float = Field(ge=0.0,
    le=1.0)`, and `CHEMCLAW_FINGERPRINT_SIMILARITY_THRESHOLD=nan` is **rejected** at startup
    ("Input should be less than or equal to 1"). So a NaN threshold is refused where it comes from
    a trusted operator and accepted where it comes from the model — the inverse of the intended
    trust ordering.
  - The `verdict` string is not merely returned, it is *serialized*: `model_dump()` includes it
    (`dumped keys: [... 'verdict']`), which is the whole point of the `computed_field`. The false
    assertion therefore reaches the model's context, not just the return value.

  I sustain **high**. It is at the low end of high only because it needs a malformed argument rather
  than an ordinary one; the consequence — a manufactured "no precedent exists" with every guard
  reporting green — is the worst outcome this subsystem can produce.

---

## `campaign_progress` enumerates 2^k cells on the connector's event loop — a 2.8 KB tool call wedges the whole process

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  1. Read the path. `problem.py:1048` `discrete_candidate_count` returns `total` early when there are
     no exclusions and otherwise walks `product(*options)`; `progress.py:238` calls it unconditionally
     to fill `design_space`; `tools.py:638` is `return read_progress(...)` — a bare call, while
     `tools.py:546/548/745/847` all use `await asyncio.to_thread(...)`. So `campaign_progress` is the
     one CPU-bound tool in that file that runs on the loop. All four line numbers are current.

  2. `/tmp/verif/f2_space.py` — my own timing of the enumeration, N binary categoricals plus one
     `ExcludeConstraint`:

     ```
     n= 16 cells=2^16=      65,536 feasible=      49,152    0.211s  rate=310,225 cells/s
     n= 18 cells=2^18=     262,144 feasible=     196,608    0.751s  rate=349,087 cells/s
     n= 20 cells=2^20=   1,048,576 feasible=     786,432    3.065s  rate=342,096 cells/s
     n= 22 cells=2^22=   4,194,304 feasible=   3,145,728   12.201s  rate=343,780 cells/s
     n=40 accepted by OptimizationProblem; payload bytes = 4439
     cells discrete_candidate_count would enumerate: 1099511627776
     ```

     ~343k cells/s against the reported ~375k — same number. 2^40 at that rate is **3.2 × 10^6 s
     ≈ 37 CPU-days** in one un-yielding loop (reporter said ~34; same).

  3. `/tmp/verif/f2_loop.py` — the real tool coroutine via `server.call_tool("campaign_progress",
     …)` with a 10 ms heartbeat task running alongside on the same loop, N=20:

     ```
     campaign_progress returned in 2.73s, design_space=786432
     heartbeat ticks during call window: 1 (ideal ~272), worst stall 2.73s
     ```

  4. Checked for anything above it that would cut the call short. `connectors/server.py:385`
     `connector_app` installs `CallerLogMiddleware` and the MCP session manager and **no** request
     timeout; `request_timeout: 120` in `connectors/bo/connector.yaml` is consumed by
     `connectors/registry.py:270 request_timeout_seconds`, i.e. on the *client* side in core. The
     server keeps running after the client gives up.

- **Why**

  Confirmed, and my loop measurement is worse than the one filed. The reporter reported 20 heartbeat
  ticks where ~280 were due; I measured **1 tick where ~272 were due**. The loop was not degraded,
  it was dead for the entire call — which is what a pure-Python `for` loop with no `await` in it does
  when run directly in a coroutine. Every other in-flight MCP request and every SSE stream on that
  uvicorn worker stalls with it.

  Nothing bounds the exponent: `OptimizationProblem.parameters` is `Field(min_length=1)` with no
  maximum (`problem.py:251`) and `CategoricalParameter.categories` is `Field(min_length=2)` with no
  maximum (`problem.py:73`) — I confirmed the 40-parameter problem validates, at a 4.4 KB serialized
  payload (the reporter's 2,799 B is a leaner hand-written form of the same thing; the difference is
  immaterial). `campaign_progress` is declared `read_only` in the manifest and the endpoint declares
  `auth: mode: none`, so the tool least likely to be refused is the one that stops the pod.

  The comment at `problem.py:1060-1063` ("this space is small by construction: it is the space a
  unique-seeding loop already walks one point at a time") is a true statement about the two `engine.py`
  callers (348, 447 — both inside functions that `tools.py` reaches through `asyncio.to_thread`) and a
  false one about the `progress.py` caller, which takes the problem straight from the request and asks
  for the count as a display field. The reporter has this exactly right.

---

## `generate_screening_design` builds a 2^k-row design with no factor cap — memory exhaustion from a ~2 KB call

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  1. Read `engine.py:870 factorial_design`. It refuses constraints, `n_generators < 0`, `n_center < 0`,
     `n_repetitions < 1`, calls `_require_knobs_are_honoured`, and then dispatches to
     `_full_design` (`engine.py:831`), which hands the domain to `FractionalFactorialStrategy` and
     materializes `[{p.name: _cast(...) for p in problem.parameters} for _, row in frame.iterrows()]`.
     There is no k check anywhere on that path.

  2. `/tmp/verif/f3_fact.py` — my own growth measurement, k two-level categoricals, no constraints,
     reporting the request size, wall time, peak RSS and the serialized response size:

     ```
     baseline RSS after imports: 988 MB
     k=  8 request=  959B ->        256 runs in    0.02s, peak RSS    990 MB, response JSON   0.02 MB
     k= 12 request= 1393B ->      4,096 runs in    0.28s, peak RSS    995 MB, response JSON   0.51 MB
     k= 15 request= 1720B ->     32,768 runs in    2.47s, peak RSS   1030 MB, response JSON   5.14 MB
     k= 17 request= 1938B ->    131,072 runs in   10.91s, peak RSS   1161 MB, response JSON  23.46 MB
     k= 19 request= 2156B ->    524,288 runs in   47.93s, peak RSS   1552 MB, response JSON 105.38 MB
     ```

     (k=19 is my own added point, beyond the reporter's range. Box has 15 GB total.)

  3. `/tmp/verif/f3_tool.py` — through the real MCP boundary, to prove the tool itself is the entry
     point rather than the engine function:

     ```
     tool k=12: 4096 runs in 0.33s, resp 0.61 MB, RSS 1066 MB
     tool k=15: 32768 runs in 2.87s, resp 6.13 MB, RSS 1147 MB
     ```

  4. Tested the `n_repetitions` sub-claim separately. All-categorical is refused
     ("n_repetitions needs at least one continuous factor"), but with one continuous factor present
     it is unbounded:

     ```
     continuous present, n_repetitions=1    ->     32 runs
     continuous present, n_repetitions=5    ->    160 runs
     continuous present, n_repetitions=1000 -> 32,000 runs
     ```

- **Why**

  My numbers land on the reporter's to within measurement noise (0.02/0.28/2.47/10.91 s against
  0.02/0.31/2.52/10.66 s; RSS 990/995/1030/1161 MB against 990/995/1030/1162 MB), so the measurement
  reproduces independently. The 4× per two factors holds through my k=19 point in both time and
  memory.

  Extrapolating from k=19 (524,288 runs, +564 MB over baseline, 47.9 s) to the reporter's k=25 is a
  64× step: ≈ 36 GB and ≈ 51 minutes, against their "~45 GB and ~45 minutes". Same conclusion on a
  15 GB box, and the OOM in fact arrives earlier — k≈23 already wants ~9 GB of live objects on top of
  the ~1 GB torch/bofire baseline, from a ~2.4 KB request. Long before that, the response is the
  defect on its own: **105 MB of JSON at k=19**, aimed at a model's context window, from a
  2,156-byte call.

  The finding's framing of the asymmetry is fair and checkable: `fingerprint_max_top_k` exists as a
  named control precisely because a model-supplied count reaches a query (`config/fingerprints.py:34-39`),
  and `find_matches` applies it at one chokepoint — while `factorial_design` validates four
  scalars for sign and nothing at all for magnitude. The only bound in the BO module on the same
  axis is the prose at `problem.py:566-571` ("a 40-factor two-level screen is not a thing anyone
  runs"), which is a claim about chemists, not a check on callers.

  `generate_screening_design` is `read_only` in the manifest with `auth: mode: none` on the endpoint,
  the same exposure as finding 2. Unlike finding 2 it *is* on `asyncio.to_thread`
  (`tools.py:745`), so it does not wedge the loop — it exhausts the process instead. High stands.

---

## Working-tree note

No source file was mutated. Four rows inserted into `molecule_fingerprints` for the Postgres leg of
finding 1 were deleted in the same session (`id LIKE 'verif-nan-%'`). Scripts are under
`/tmp/verif/`. No `git stash`, `git checkout -- .` or tree-wide revert was run.
