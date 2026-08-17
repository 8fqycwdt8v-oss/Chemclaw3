# Round 1 verification — `mcp-chem-rxn-kit--security.md`, lens: reachability + consequence

Scope: the four **critical/high** findings only. Everything ran against
`/workspace/chemclaw3-mcp` (its own `uv` venv, `.venv/bin/python`, 4 CPUs, `ulimit -s` = 8192 KB)
with the real `chem` and `rxnpredict` apps under uvicorn behind a real MCP handshake, and against
the caller at `/home/user/Chemclaw3`.

**The caller, established once for all four.** `src/chemclaw/connectors/transport.py` builds a tool
per MCP-advertised tool via `langchain_mcp_adapters.tools.load_mcp_tools`, so the *only* argument
schema on the backend side is the one the server itself advertises. There is no pydantic model, no
manifest field and no `wrap_tool_call` middleware in `src/chemclaw/agent/` that bounds a tool
argument's size or value (`agent_audit_max_arg_chars` truncates the *audit row*, not the call).
`src/chemclaw/api/routes/` exposes no tool-call passthrough — `POST /sessions/{id}/messages` is the
only door, so **every argument is emitted by the model.** That is the fact that decides findings 1
and 3, because model output is capped: `core/config/llm.py::llm_max_tokens = 4096`, applied to both
providers in `agent/llm_provider.py::_generation_options` (line 299).

---

## A 20,000-character SMILES argument kills the server process (SIGSEGV)

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium
- **What I did**:

  The crash itself reproduces exactly as reported. Isolated:

  ```
  $ for n in 5000 15000 20000; do uv run python /tmp/p1.py $n; done
  parse ok 0.011s atoms=5000   canon ok 0.495s
  parse ok 0.026s atoms=15000  canon ok 4.913s
  parse ok 0.036s atoms=20000  exit=139
  ```

  End-to-end against the real `chem` app under uvicorn, real bearer, real MCP `tools/call`
  (`/tmp/repro2/serve_chem.py` + `/tmp/repro2/attack.py`, tool `resolve_compound`):

  ```
  {"status":"ok","server":"chem","revision":"unknown"}
  tools: ['resolve_compound','stoichiometry_table','green_metrics','render_structure']
  wrap.sh: line 2: 14200 Segmentation fault  .venv/bin/python /tmp/repro2/serve_chem.py
  SERVER EXITED WITH STATUS 139
  ```

  The client did not get an error — it hung until its own timeout (`client exit=124`), and
  `curl /healthz` gave connection-refused. So: process death, confirmed, on the live tool.

  Then I bisected the cliff, because the exact length is what decides reachability:

  ```
  n=16000 canon ok 5.712s | n=17000 canon ok 6.278s | n=18000 canon ok 7.061s
  n=19000 exit=139
  ```

  So the minimum lethal argument is ~18,500–19,000 characters. A more compact topology does not
  lower it — the depth is the chain, and every atom costs at least one character: `"C("*6000 + "C"
  + ")"*6000` (18,001 chars, 6,001 atoms) canonicalises in 0.75 s.

  Finally I measured whether the real caller can emit such a string, with the live Anthropic
  credential:

  ```
  $ ANTHROPIC_API_KEY=… uv run python -c "…count_tokens…"
  19000 chars -> 9522 tokens
  20000 chars -> 10022 tokens
  ```

  2.0 characters per token for a run of `C`. The deployment's per-response cap is
  `llm_max_tokens = 4096`, so the largest SMILES the model can put in a tool argument is **~8,190
  characters**, and that only if the entire response budget goes to the string. 8,190 characters is
  *below* the 15,000 that canonicalises safely in 4.9 s, let alone the 18,500 that segfaults.

- **Why**: The mechanism is real and I confirmed the worst version of it — a signal, not an
  exception, killing the whole uvicorn process on a live tool through a normal authenticated call.
  What does not hold is the trigger. The finding's own framing is right that "untrusted input
  reaches this", but untrusted input reaches this *through the model*, and the model cannot emit
  19,000 characters in one tool call under the shipped `llm_max_tokens=4096`: generation stops at
  ~8.2 k characters with `stop_reason=max_tokens` and an incomplete `tool_use` block that never
  dispatches. There is no other caller — no REST passthrough, no ingest path, no activity that
  forwards a document-derived SMILES to `chem` (`grep -rn "resolve_compound|render_structure|
  classify_reaction" src/ --include=*.py` in the backend finds only prose and the in-process
  `core/reagents.py`).

  So this is a missing bound that is currently out of reach by ~2.3x, and is one ENV override
  (`CHEMCLAW_LLM_MAX_TOKENS`, a routine tuning knob with no security character) away from being in
  reach. That is worth fixing at the price of the three-line cap the finding proposes; it is not a
  critical. I would drop "the pod restart-loops as long as the input is retried" as well: the
  backend's `absorb_connect_failure` degrades a dead connector to "no tools this turn" rather than
  retrying it, so the retry pressure the loop needs is not there.

---

## Eight `render_structure` calls with a 4 KB SMILES take the pod out of service, and the caller hanging up does not stop it

- **Verdict**: CONFIRMED (and understated — one call is enough)
- **Severity I would assign**: high
- **What I did**:

  Single-call cost, in-process (`/tmp/p2.py`, `render_svg` verbatim):

  ```
  n=200   render ok 0.02s
  n=1000  render ok 1.53s
  n=2000  render ok 18.54s
  n=4000  (no completion inside a 120 s ceiling)
  ```

  The finding's own premise — that you need 8 concurrent calls, because 8 is the default
  `to_thread` executor — is too generous to the server. **One** call is enough
  (`/tmp/repro2/one_render.py 1 2500`, one MCP session, one `render_structure`, 2,500-char SMILES):

  ```
  baseline: 200 in 0.08s
  during t+2s: FAILED ReadTimeout after 10.07s
  during t+4s: FAILED ReadTimeout after 10.06s
  during t+6s: FAILED ReadTimeout after 10.15s
  during t+8s: 200 in 1.52s
  ```

  `/healthz` — which touches nothing — is dead for >30 s behind a single worker thread. That
  contradicts `chem/tools.py`'s own module docstring ("RDKit releases the GIL for the heavy passes,
  so the threads are real parallelism"): `Compute2DCoords` evidently does not.

  The no-cancellation half, measured with a real disconnect rather than a task cancel — 8 renders
  of `"C"*2000` fired, the client process `SIGKILL`ed at t=6 s:

  ```
  attacker SIGKILLed at t=6s
  t=16s  code=000 latency=10.01
  t=26s  code=000 latency=10.02
  ...
  t=76s  code=000 latency=10.01     (then 200 again at ~t+100s)
  ```

  76 s of a totally unresponsive pod with no client attached, from 16 KB of request body. The
  client-side-only nature of the manifest bound is confirmed too:
  `connectors/registry.py:296` turns `request_timeout` into `read_timeout_seconds` on the httpx
  connection — a read timeout, nothing the server ever learns about.

  One correction *against* the finding's fix framing, and it strengthens the case: the backend's
  own chart does put a `livenessProbe` on `/healthz` for connector deployments
  (`deploy/helm/chemclaw/templates/deployment-connectors.yaml:75`) with k8s defaults — 10 s period,
  3 failures — so ~30 s of this is a kill. My measured window is 76 s.

- **Why**: Reachable, and comfortably so: 2,500 characters is ~1,250 tokens, well inside the 4,096
  the model can emit, and `render_structure`'s whole job is "draw whatever the chemist named". The
  consequence is what is claimed and then some — not "a queue behind a busy executor" but total
  loss of the process's event loop from a single thread, persisting long after the caller is gone,
  and long enough to trip a liveness probe. The only part I would not sign is the "restart loop"
  wording: one restart per attack is what the measurement supports, a loop needs the attack
  repeated. Everything else holds. High is right; I considered critical and held back only because
  it is availability-only and self-healing.

---

## `rxnpredict`'s two synchronous tools run RDKit on the event loop — one call stalls every other request on the process

- **Verdict**: CONFIRMED (mechanism), with the magnitude overstated
- **Severity I would assign**: medium
- **What I did**:

  The inline-call claim is upstream's code as quoted — `.venv/.../mcp/server/fastmcp/utilities/
  func_metadata.py:93-96` reads exactly `if fn_is_async: return await fn(...) else: return fn(...)`
  — and the measurement confirms it. `/tmp/repro2/loopblock.py` polls `/healthz` every 100 ms while
  one `classify_reaction` is in flight, against the real server:

  ```
  n=10     call took 0.08s  WORST healthz 0.01s
  n=15000  call took 5.48s  WORST healthz 5.30s
  ```

  Reproduced. But 15,000 characters is again above what the caller can emit, so I re-ran at the
  reachable ceiling:

  ```
  n=8000   call took 1.45s  WORST healthz 1.27s
  ```

  Also checked: the finding says `rxnpredict` is "the **only** server in the fleet with no
  `tests/test_event_loop_offload.py`". `ls servers/*/tests/test_event_loop_offload.py` returns
  `calc`, `chem`, `safety` — `props` has none either, and by `chem/tools.py`'s own docstring that is
  deliberate there.

- **Why**: The defect is real and the property is genuinely unasserted: two `def` tools on a server
  whose siblings all hop to a thread, and a measurable whole-process stall proportional to input
  size. What I will not sign at "high" is the size of that stall. The reachable worst case is ~1.3 s
  of a stopped event loop per call, not ~5 s, because the 15,000-character input the finding
  triggers on cannot be produced by the only caller (see the first finding); and at any *plausible*
  reactant string the stall is ~0.08 s. A capability pod that goes deaf for a second under a
  deliberately padded argument is worth fixing — it is two `async def`s and two `to_thread`s — but
  it is not in the same class as the `render_structure` finding above, where one ordinary-looking
  call costs the pod a minute. Medium.

---

## `top_k` is unbounded on every served prediction tool; the `le=50` bound lives on schemas nothing calls

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium
- **What I did**:

  The unreachable-validation half is exactly as reported. `grep -rn
  "ForwardRequest|ConditionsRequest|ClassifyRequest|HealthResponse" --include=*.py
  servers/rxnpredict` returns four hits, all of them the definitions in `engine/schemas.py`. The
  advertised schema, read off `server.list_tools()`, carries no bound:

  ```
  predict_forward_reaction … "top_k": {"default": 5, "title": "Top K", "type": "integer"} …
  ```

  And the value reaches the predictor untouched — registered a spy predictor and called the real
  MCP tool:

  ```
  MCP tool accepted top_k=1000000000 -> predictor received 1000000000
  MCP tool accepted top_k=-1 -> predictor received -1
  ```

  Nothing on the backend side re-bounds it: `load_mcp_tools` builds the LangChain arg schema from
  that same unbounded JSON schema. `grep -rn "top_k" servers/rxnpredict/src` shows no clamp between
  the tool body and `num_beams=max(top_k, 5)` / `num_return_sequences=top_k`.

  The consequence I could not reproduce and partly contradicted. `torch`/`transformers` are not
  installed in this workspace (`ModuleNotFoundError` for both), so `reaction_t5` cannot be loaded
  here and the OOM claim is untested. Against it: both ensemble tools gather with
  `return_exceptions=True` and log a per-model warning (`tools.py`, `predict_forward_reaction` /
  `predict_reaction_conditions`), so a predictor that raises `RuntimeError`/`MemoryError` on a
  refused allocation is *swallowed* — the caller gets `n_models_succeeded=0`, not a dead pod. A
  1e9-beam allocation is hundreds of GB and fails at malloc rather than being touched, which is the
  clean-exception case, not the OOM-killer case.

  What I could demonstrate is a different, quieter consequence the finding underplays — negative
  `top_k` is a *silently wrong scientific answer*, not an error:

  ```
  top_k=5 : per-model=['CCO','CCN','CCC','CCCl']  consensus=['CCO','CCN','CCC','CCCl']
  top_k=-1: per-model=['CCO','CCN','CCC']         consensus=['CCO','CCN']
  top_k=0 : per-model=[]                          consensus=[]
  ```

  Two of four predicted products vanish from the consensus with no error anywhere. A chemist is
  shown a shorter ranked list that looks complete.

- **Why**: The reachability half is fully confirmed — `top_k=1000000000` is ten characters, the
  model can trivially emit it, the served schema advertises no bound, and the bound that exists
  sits on three envelope classes nothing in the repository constructs. That part of the finding is
  solid and the fix (`Annotated[int, Field(ge=1, le=50)]`) is correct.

  The severity rests on "a single tool call is an out-of-memory kill of the pod in the shipped
  image", and that is the part I cannot grant. It needs the `reaction_t5` extra actually loaded
  (the live stack runs `fake_a`/`fake_c` per `infra/live/e2e-full-stack/up.sh:91`), it is stated for
  a value whose allocation almost certainly fails cleanly rather than being reaped, and the ensemble
  path converts a raising predictor into a degraded answer by construction. An attacker who *sized*
  `top_k` to the pod's memory limit might get there; that is a different, unproven claim than the
  one filed. Downgraded to medium, where the demonstrated harm — silently truncated predictions on
  a negative value, and unbounded work handed to a model with no wall-clock bound — sits.
