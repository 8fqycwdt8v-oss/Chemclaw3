# Verdicts — `science-bo-fp--security.md`, reachability/consequence lens

Scope: the three findings marked **high**. The file has no `critical`. `medium`/`low` ignored.

The slice under review is unmodified relative to `HEAD` (`git diff HEAD --stat` over
`science/fingerprints`, `science/bo`, `connectors/bo`, `connectors/molfp` is empty), so everything
below is about the committed code. All runs are `uv run` against the live venv; the Postgres run
against the container stack already up (`infra-postgres-1`).

---

## A NaN `threshold` escapes `find_matches`' clamp and turns a populated index into a "genuine negative result"

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (as filed)
- **What I did**:

  1. `/tmp/aud/t1_clamp.py` — the clamp and the coercion in isolation:
     ```
     pydantic lax 'nan'        -> nan isnan=True
     pydantic lax 'NaN'        -> nan isnan=True
     pydantic lax 'inf'        -> inf isnan=False
     json.loads('{"threshold": NaN}') -> {'threshold': nan}
     ---- the clamp itself ----
       t=nan        -> clamped=nan
       t=-inf       -> clamped=0.0
       t=inf        -> clamped=1.0
       t=-1.0       -> clamped=0.0
       t=2.0        -> clamped=1.0
     ```
     `min(max(t, 0.0), 1.0)` is total for `±inf` and for out-of-range finites, and a no-op for NaN.

  2. `/tmp/aud/t1_tool.py` — **through the real MCP boundary**, not the private function:
     `server.call_tool("similar_molecules", {...})` on `connectors/molfp/server/tools.py`'s live
     `FastMCP` instance, over an in-memory store holding five molecules:
     ```
     indexed: 5
     threshold=0.3   hits: 3  index_empty: False  scan_trunc: False  hits_trunc: False
                     VERDICT: 3 indexed molecule(s) matched this query.
     threshold='nan' hits: 0  index_empty: False  scan_trunc: False  hits_trunc: False
                     VERDICT: No indexed molecule matched this query. The molecule fingerprint
                              index holds records and was searched, so this is a genuine negative
                              result.
     threshold='NaN' hits: 0  ... same verdict
     threshold=-5.0  hits: 5  (clamped to 0.0)
     threshold=5.0   hits: 1  (clamped to 1.0)
     ```
     The JSON *string* `"nan"` is enough — ordinary, standard JSON. Nothing in FastMCP's
     `arg_model.model_validate` rejects it.

  3. `/tmp/aud/t1_pg.py` — the production `PostgresFingerprintStore` against live Postgres
     (`molecule_fingerprints`, rows inserted under the current definition and deleted afterwards):
     ```
     count: 9 is_empty: False
     threshold=0.3   -> hits=6 index_empty=False | "6 indexed molecule(s) matched this query."
     threshold=nan   -> hits=0 index_empty=False | "No indexed molecule matched this query. The
                        molecule fingerprint index holds records and was searched, so this is a
                        genuine negative result."
     ```
     Postgres orders NaN above every other float, so `1 - (bits <%> q) >= 'NaN'` is false for every
     finite similarity — the durable backend fails the same way as the in-memory one, not
     differently.

- **Why**: I attacked reachability and could not find anything upstream that stops it.

  - **The trigger is one tool argument, not a private call.** `threshold: float | None = None` on
    `similar_molecules` / `similar_reactions` is declared, documented to the model in the tool
    docstring ("`threshold` (Tanimoto floor)"), and carries no `Field` constraint. It is the model's
    to set, which is the same trust boundary the module's own `top_k` clamp is written against.
  - **The only other source of `threshold` is config, and config cannot be NaN.**
    `fingerprint_similarity_threshold: float = Field(default=0.3, ge=0.0, le=1.0)` rejects NaN
    (`nan >= 0.0` is false), so an env override is not a second path. I traced every caller of
    `find_matches`: `molfp/search.py:93` and `rxnfp/search.py:47`, whose only threshold-supplying
    callers are the two MCP tools. `retrieval/retrievers.py:238` passes no threshold. So the entry
    point is exactly one and it is the outermost one.
  - **The sibling `top_k` is not affected** — `TypeAdapter(int|None).validate_python("nan")` is
    REJECTED — so the finding is correctly scoped rather than over-broad.
  - **The consequence is what is claimed, verbatim.** `verdict` is a `computed_field`, so the
    sentence is in the serialized payload the model composes its answer from, and all three
    "the search was incomplete" flags read `False`. This is a precedent / structure-identity answer:
    what a chemist is shown is an affirmative "we searched, there is nothing like this on file"
    over a corpus that was never filtered. "The caller might catch it" does not apply — there is
    nothing to catch; no exception is raised and no flag is set.

  What the reporter missed, and it makes the case sharper rather than weaker: the codebase already
  knows this exact hazard and already has the fix in the same directory tree. `Observation.value` is
  `Field(allow_inf_nan=False)` (`science/bo/problem.py:403`) with the docstring "`value` must be
  finite: NaN compares false in both directions, so it would silently win `best_of`". That is the
  same sentence about the same arithmetic, applied one package over and not here. The one-character
  version of the reporter's fix is `threshold: float | None = Field(default=None, allow_inf_nan=False)`
  at the two tool signatures, or the explicit `math.isnan` refusal in `find_matches` — the latter is
  better, because it is the single chokepoint both entry points already share.

---

## `campaign_progress` enumerates 2^k cells on the connector's event loop — a 2.8 KB tool call wedges the whole process

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium
- **What I did**:

  1. Confirmed the mechanism and the asymmetry. `grep -n "async def\|to_thread"` over
     `connectors/bo/server/tools.py`: `to_thread` at 546, 548 (`suggest_next_experiment`), 745
     (`generate_screening_design`), 847 (`predict_outcome`) — and `campaign_progress` ends
     `return read_progress(...)`, on the loop. `OptimizationProblem.parameters` is
     `Field(min_length=1)` with no maximum; the connector's `BodySizeLimit` is
     `connector_max_request_bytes = 1000000`, three orders of magnitude above the payload.

  2. `/tmp/aud/t2_progress.py` — reachability and cost:
     ```
     40-param payload VALIDATES; MCP request body = 3361 bytes; cells = 2**40 = 1,099,511,627,776
       n= 16 cells=2**16=      65,536 feasible=      49,152    0.152s  (431,463 cells/s)
       n= 18 cells=2**18=     262,144 feasible=     196,608    0.642s  (408,053 cells/s)
       n= 20 cells=2**20=   1,048,576 feasible=     786,432    2.546s  (411,900 cells/s)
     ```
     ~412k cells/s, so 2^40 is ~31 CPU-days. The reporter's numbers reproduce.

  3. `/tmp/aud/t2_loop.py` — the real tool coroutine with a 10 ms heartbeat, N=20:
     ```
     campaign_progress returned in 2.72s design_space=786432
     heartbeat ticks=20 (ideal ~296), worst stall 2.73s
     ```
     The loop is dead for the whole call. Mechanism granted.

  4. **The part the finding does not check** — `/tmp/aud/t2_http.py`: the real bo connector app under
     uvicorn on :8899, a real `streamablehttp_client` MCP session, N=22, and a kubelet-shaped
     `/healthz` GET with the Kubernetes default `timeoutSeconds: 1` running alongside:
     ```
     baseline probes (server idle):
       [idle0] /healthz -> 200 in 0.07s
       [idle1] /healthz -> 200 in 0.06s
     calling campaign_progress with N=22 (2208 byte payload)
       [during] /healthz -> PROBE FAILED after 1.11s (ReadTimeout)
     call finished in 12.17s
     ```

- **Why**: the mechanism is real and I confirm it. The **consequence is not**, and it is the
  consequence that carries the severity.

  The finding states: "There is no timeout above it (the connector's `request_timeout: 120` is the
  *client's* deadline; the server keeps running), no cancellation point" — and concludes ≈34
  CPU-days of wedged pod. There is no application-level timeout, correct. But
  `deploy/helm/chemclaw/templates/deployment-connectors.yaml` gives every connector container a
  `livenessProbe: httpGet /healthz` with no `timeoutSeconds`, `periodSeconds` or `failureThreshold`
  override — Kubernetes defaults of 1 s / 10 s / 3. The probe I ran above is that probe, and it
  fails the moment the loop is blocked. Three consecutive failures is ~20–30 s, after which the
  kubelet kills the container; SIGTERM also needs the loop, so it is SIGKILL after the grace period.

  So what a real deployment gets from the 2.8 KB call is **not** a 34-day wedge. It is: the bo
  connector pod stalls, fails liveness, is killed and restarted, dropping the in-flight MCP
  requests it was serving. Repeatable, and at `connectors.bo.replicas: 1` it takes the BO capability
  out for the restart window — a genuine availability defect, and worth the cap the reporter
  proposes. But the tipping point is around N≈24 (2^24/412k ≈ 41 s); below that the pod survives
  and the harm is a stall of seconds, not days. The blast radius is one capability pod that
  self-heals, with no integrity or confidentiality impact and no data loss (campaign state is in
  Postgres/Temporal, not in this process). That is medium, not high.

  Two further notes on the fix, since the reporter proposes both halves:
  - Moving `read_progress` onto `asyncio.to_thread` does help materially, which I measured on the
    tool that already does it (see the next finding: 389 of ~654 heartbeat ticks survived, worst
    stall 0.557 s — under the 1 s probe timeout). It does not bound the CPU burn.
  - The cap in `discrete_candidate_count` is the load-bearing half, and I agree it belongs there.
  - The finding's premise that `campaign_progress` "sits behind the *weakest* authorization gate"
    is correct as to the manifest but does not add reach here: the trigger is the tool arguments,
    and the tools it is compared against are equally callable.

---

## `generate_screening_design` builds a 2^k-row design with no factor cap — memory exhaustion from a ~2 KB call

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium
- **What I did**:

  1. `/tmp/aud/t3_design.py` — reachability and measured growth, calling `factorial_design` on a
     validated `OptimizationProblem` of k two-level categoricals with no constraints:
     ```
     25-factor payload VALIDATES; body=1779 bytes; rows=2**25=33,554,432
     baseline RSS 989 MB
     n=  8 ->        256 runs in    0.02s, peak RSS    990 MB, JSON     0.0 MB
     n= 12 ->      4,096 runs in    0.30s, peak RSS    995 MB, JSON     0.6 MB
     n= 15 ->     32,768 runs in    2.51s, peak RSS   1030 MB, JSON     6.1 MB
     n= 17 ->    131,072 runs in   10.91s, peak RSS   1162 MB, JSON    27.9 MB
     n= 18 ->    262,144 runs in   22.53s, peak RSS   1336 MB, JSON    59.2 MB
     ```
     The reporter's table reproduces line for line, and I extended it one step. Nothing refuses:
     `factorial_design`'s four guards are the constraint refusal, `n_generators >= 0`,
     `n_center >= 0`, `n_repetitions >= 1`, and `_require_knobs_are_honoured` — none of them looks
     at k.

  2. `/tmp/aud/t3_rss.py` — memory attributable to the design alone, in the connector's own
     process image:
     ```
     python only: 42 MB
     bo connector app imported: 1057 MB
       n= 15   32,768 runs   2.47s  peak RSS 1096 MB (+39)
       n= 16   65,536 runs   5.15s  peak RSS 1135 MB (+78)
     ```
     ~1.2 KB per run, ~79 µs per run. k=25 extrapolates to ~40 GB and ~44 min of one CPU.

  3. `/tmp/aud/t3_loop.py` — the real tool over `server.call_tool`, k=16, with a 10 ms heartbeat:
     ```
     generate_screening_design(n=16) 6.29s; heartbeat ticks=389 (ideal ~654) worst stall 0.557s
     peak RSS 1221 MB
     ```
     Unlike `campaign_progress`, this one *is* on `asyncio.to_thread`, so the loop keeps running at
     ~60% and the worst stall stays under a 1 s liveness probe. The pod is not killed by liveness;
     it runs to the OOM.

- **Why**: the mechanism and the failure mode are exactly as filed — no k cap, ~2 KB payload,
  quadrupling per two factors in both time and memory, and (because the work is threaded off the
  loop) an OOM rather than a probe kill. I found nothing upstream that bounds it: the body-size
  limit is 1 MB, `parameters` has no `max_length`, `campaign_progress`'s missing `to_thread` is not
  shared here, and the `_roman` comment the reporter quotes is indeed prose about chemists.

  What I dispute is only the label, and one part of the framing:

  - **Severity.** This is availability-only, contained to one capability pod by the container's
    memory limit (a cgroup OOM kills the container, not the node), self-healing on restart, with no
    integrity or confidentiality impact and no persistent state to lose. It is the same class as
    the previous finding, reached the same way — a tool argument the model emits, which an attacker
    reaches only by influencing the model. Medium.
  - **"OOM-killed long before it answers"** is right in outcome but glosses the timing: at k=25 the
    process spends tens of minutes climbing before it dies, and the connector's `request_timeout:
    120` releases the caller at two minutes, so the observable event is a timed-out tool call
    followed some minutes later by a pod restart, not a prompt crash. That matters for anyone
    triaging it.

  One observation that bears on the memory headroom and that neither the finding nor I can settle
  from this checkout: `deploy/helm/chemclaw/values.yaml` gives every connector
  `resources.connector.limits.memory: 512Mi`, with no per-bundle override under `connectors.bo`,
  while the bo connector's *import* alone measures 1057 MB RSS here (torch/bofire/botorch). If that
  holds in the container, the shipped chart cannot start this pod at all, which is a separate defect
  from this finding and makes the exact k at which the OOM lands unknowable from the chart. I record
  it rather than lean on it: the unbounded growth is proven regardless of where the ceiling sits.

---

### Cleanup

No source file was mutated. The Postgres rows inserted by `/tmp/aud/t1_pg.py` were deleted in the
same script and verified gone (`leftover AUDIT rows: 0`). `git status` shows only other agents'
edits (`agent/plan_gate.py`, `retrieval/harness.py`), which I did not touch.
