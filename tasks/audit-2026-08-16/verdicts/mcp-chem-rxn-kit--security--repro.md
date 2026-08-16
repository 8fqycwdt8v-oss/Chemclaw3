# Round 1 verification — `mcp-chem-rxn-kit--security`, lens: **does it actually reproduce?**

Scope: the four **critical/high** findings only. Everything below was re-derived from the source in
`/workspace/chemclaw3-mcp` with my own scripts under `/tmp/v/`; I did not run `/tmp/repro/*` or
`/tmp/probe*.py` and did not rely on the reporter's transcripts. Servers were started with my own
wrapper so the *wait status* of the uvicorn process is captured, which is the part that decides
"crash" versus "slow". RDKit in this venv is **2026.03.5**; host is 4 CPUs, `ulimit -s` 8192 KB.

---

## A 20,000-character SMILES argument kills the server process (SIGSEGV)

- **Verdict**: CONFIRMED
- **Severity I would assign**: critical

- **What I did**

  1. Isolated, `/tmp/v/seg.py` (`MolFromSmiles` then `MolToSmiles` on `"C"*n`), run as
     `uv run --project servers/chem python /tmp/v/seg.py <n>`:

     ```
     --- n=5000    parse 0.011s atoms 5000    canon ok 0.518s   exit=0
     --- n=15000   parse 0.026s atoms 15000   canon ok 4.935s   exit=0
     --- n=16000   parse 0.030s atoms 16000   canon ok 5.702s   exit=0
     --- n=18000   parse 0.033s atoms 18000   canon ok 7.192s   exit=0
     --- n=20000   parse 0.035s atoms 20000   <no canon line>   exit=139
     ```

     139 = 128 + SIGSEGV(11). Parse survives; the death is in `MolToSmiles`. I narrowed the cliff
     the reporter bracketed at 15k/20k to **between 18,000 and 20,000**.

  2. End-to-end against the real `chem` app under uvicorn, with a real MCP handshake and a valid
     bearer token (`/tmp/v/client_chem.py` using `mcp.client.streamable_http` — my own client, not
     the reporter's), calling `resolve_compound(name="C"*20000)`:

     ```
     tools: ['resolve_compound', 'stoichiometry_table', 'green_metrics', 'render_structure']
     ...
     Processing request of type CallToolRequest
     [21:05:33] WARNING: not removing hydrogen atom without neighbors
     SERVER EXIT=139
     ```

     The server log ends mid-call with no traceback and the process's wait status is 139.

  3. Same input against the real `rxnpredict` app, `classify_reaction(reactants="C"*20000)`
     (`/tmp/v/rxn_attack.py`):

     ```
     healthz err ReadError
     httpx.RemoteProtocolError: Server disconnected without sending a response.
     SERVER EXIT=139
     ```

  4. Incidental, and worse than reported: `render_structure(smiles="C"*20000)` against the same
     server killed it with **`SERVER EXIT=137`** (SIGKILL — memory, not stack) before the client's
     60 s timeout. So the large-input door has two lethal exits, not one.

  5. Cited symbols and lines are current: `chem.py:84` `stripped = smiles.strip()` … `:92`
     `return mol` inside `require_molecule` (def at `:59`), `require_canonical_smiles` at `:95`;
     `preprocessing.py:11-25` is `canonical_smiles` + `canonical_multi_smiles`. No length check
     exists in either. 20,000 bytes is 2 % of `DEFAULT_MAX_REQUEST_BYTES = 1_000_000`
     (`app.py:56`), as claimed.

- **Why**: the claim reproduces exactly, on both servers, through the served MCP surface with a
  valid credential, from a single call with no concurrency. A signal, not an exception — so
  `_sanitize_tool_errors`, `ValueError` handling and the `asyncio.to_thread` hop are all irrelevant
  to it, which is the reporter's point and is correct. Critical stands.

---

## Eight `render_structure` calls with a 4 KB SMILES take the pod out of service, and the caller hanging up does not stop it

- **Verdict**: CONFIRMED (with a correction to the *mechanism*, which makes it worse and invalidates one of the three proposed fixes)
- **Severity I would assign**: high

- **What I did**

  1. Cost curve for `Draw.rdDepictor.Compute2DCoords` (`/tmp/v/coords.py`), which is what
     `depiction.py:30-51` calls:

     ```
     n=500    coords ok 0.20s
     n=1000   coords ok 1.62s
     n=2000   coords ok 21.77s
     n=4000   did not finish inside the remaining ~90s of a 180s budget
     ```

     Roughly cubic-to-quartic. A single 4,000-atom depiction occupies a worker for minutes, so the
     "8 concurrent" framing is generous to the server rather than to the attacker.

  2. Live: `/tmp/v/starve.py` opens 8 independent MCP sessions against the real `chem` app on
     :8970 and calls `render_structure(smiles="C"*4000)` on each. Independent `curl` probes from a
     separate shell, while the attack was in flight:

     ```
     http=000 total=10.002756s
     http=000 total=10.002327s
     http=000 total=10.002791s
     server pid alive? yes
     ```

     `/healthz` — an `async def` route that returns a three-key dict — served nothing at all.

  3. The persistence half. I killed the attacking client outright (`pkill -f starve.py`; confirmed
     no `starve.py` process in `ps -eo cmd`), then probed again:

     ```
     healthz http=000 total=20.002142s
     healthz http=000 total=20.002274s
     healthz http=000 total=20.002824s
     server alive
     ```

     and the uvicorn process still at **93.6 % CPU** in `ps`, minutes after the socket closed.
     Two more 30 s probes earlier in the same window also returned nothing. Work continues after
     the caller is gone, exactly as claimed; `connector.yaml`'s `request_timeout: 30` is
     client-side and does not touch it.

  4. **Mechanism correction.** The finding attributes the outage to exhausting the default
     `asyncio.to_thread` executor (`min(32, cpu+4)` = 8). That alone cannot explain a dead
     `/healthz`, because `/healthz` is `async def` (`app.py:201-202`) and never touches that pool.
     I measured the real cause (`/tmp/v/gil.py`: N threads running `Compute2DCoords` while a pure
     Python heartbeat thread times its own wakeups):

     ```
     N=4 atoms=1200 wall=13.20s WORST_heartbeat_stall=3.79s
     ```

     A Python thread doing nothing but `time.sleep(0.01)` stalled **3.79 s**. `Compute2DCoords`
     holds the GIL for multi-second stretches, so it starves the event loop directly — which
     contradicts `depiction.py`'s own docstring ("RDKit releases the GIL for the heavy passes, so
     the threads are real parallelism") and `tools.py`'s module docstring, both of which assert the
     opposite as the justification for the `to_thread` design.

- **Why**: every observable in the finding reproduces — `/healthz` dead during the attack, still
  dead with the attacker gone, process alive and burning CPU. The consequence (liveness-probe
  failure → restart loop from 32 KB of request body) follows directly. I would keep it at high and
  amend the fix list: proposal (2), "give the fleet its own bounded executor", would **not** restore
  `/healthz`, because the contention is the GIL and not the pool; only bounding the input
  (proposal 1) actually helps, and the docstrings claiming GIL release should be corrected rather
  than relied on.

---

## `rxnpredict`'s two synchronous tools run RDKit on the event loop — one call stalls every other request on the process

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

- **What I did**

  1. Source: `grep -n` on `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/tools.py` gives
     `334: def list_available_models` and `382: def classify_reaction`, both bare `def` under
     `@server.tool()` at `:333` / `:381` (the finding cites the decorator lines — off by one, but
     the symbols are real and current). No `asyncio.to_thread` on either path.
  2. The dispatch claim: `.venv/lib/python3.11/site-packages/mcp/server/fastmcp/utilities/func_metadata.py:93-96`
     reads exactly `if fn_is_async: return await fn(...) else: return fn(...)`. Verified in the
     installed distribution, not from the finding.
  3. My own measurement (`/tmp/v/rxn_attack.py`: one MCP session, a concurrent `/healthz` poller at
     100 ms, worst observed latency recorded around the call), against the real server under
     uvicorn:

     ```
     baseline: size=10    call=0.11s  isError=False  WORST_healthz=0.01s
     attack:   size=15000 call=4.95s  isError=False  WORST_healthz=4.86s
     ```

     The reporter printed 0.09 s / 0.10 s and 4.97 s / 4.86 s. Mine agree to within a few percent.
  4. `ls servers/*/tests/test_event_loop_offload.py` returns `calc`, `chem`, `safety` — and not
     `rxnpredict`. The claim that the property is unasserted here holds.

- **Why**: mechanism verified in upstream source, numbers re-measured independently and matched.
  An `/healthz` endpoint that touches nothing going from 10 ms to 4.86 s is loop blockage, not
  load. High stands.

---

## `top_k` is unbounded on every served prediction tool; the `le=50` bound lives on schemas nothing calls

- **Verdict**: CONFIRMED (the OOM sub-claim is the one part I could not measure here — see below)
- **Severity I would assign**: high

- **What I did**

  1. Advertised schema, read off the live `FastMCP` server (`/tmp/v/topk.py`, `server.list_tools()`):

     ```
     predict_forward_reaction top_k schema: {"default": 5, "title": "Top K", "type": "integer"}
     predict_reaction_conditions top_k schema: {"default": 5, "title": "Top K", "type": "integer"}
     predict_forward_reaction models schema: {"anyOf": [{"items": {"type":"string"},"type":"array"},{"type":"null"}], "default": null}
     envelope REJECTS 1e9: Input should be less than or equal to 50 [type=less_than_equal, input_value=1000000000, input_type=int]
     ```

     No `minimum`, no `maximum` on the served surface; the bound exists only on `ForwardRequest`.
  2. Dead-envelope claim: `grep -rn "ForwardRequest\|ConditionsRequest\|ClassifyRequest\|HealthResponse" --include=*.py .`
     (excluding `.venv`) returns **four lines, all in `engine/schemas.py` itself** — the class
     definitions at `:68`, `:81`, `:131`, `:138`. Nothing imports or constructs them. Confirmed.
  3. Passthrough, with my own spy predictor registered via
     `predictors.register_forward` (`/tmp/v/spy.py`), calling the *tool*:

     ```
     top_k=1000000000   -> tool ok | predictor received: [1000000000]
     top_k=-1           -> tool ok | predictor received: [-1]
     top_k=0            -> tool ok | predictor received: [0]
     ```

  4. Over real HTTP with a real handshake against the running server (`/tmp/v/topk_http.py`),
     `top_k` of `1000000000`, `-1` and `51` were all *accepted by validation* — the returned error
     is `no forward predictors are available in this deployment`, i.e. about the missing model, not
     about the argument.
  5. Consumption sites are real: `forward/reaction_t5.py` passes `num_beams=max(top_k, 5)` and
     `num_return_sequences=top_k` straight into `self._model.generate(...)`, and
     `meta/aggregator.py:98,194` slice `sorted_candidates[:top_k]` (where `-1` silently drops the
     last element instead of being refused). `servers/rxnpredict/Containerfile:14,24,35` does
     install `[reaction_t5,rxn_insight]`, so that predictor is present in the shipped image.

- **Why**: the finding's title claim reproduces in full — unbounded on the served surface, bounded
  only on envelopes nothing references, and the hostile value reaches the predictor argument
  unmodified. The one part I could **not** re-measure is the specific end state: `transformers`
  and `torch` are not installed in this workspace, so I did not observe a pod OOM-kill from
  `num_beams=1e9`; that half is inference from the call site rather than measurement, and an
  extreme value may fail as a `RuntimeError` while a mid-sized one (a `top_k` that allocates a few
  GB) is the one that actually kills the process. That nuance does not change the verdict or the
  severity: an unvalidated caller-controlled beam count on an inference server, plus `-1`/`0`
  reaching `results[:top_k]` and `n_best`, is a high-severity input-validation defect on its own,
  and the proposed fix (`Annotated[int, Field(ge=1, le=50)]` on the signature) is correct — FastMCP
  builds the argument model from the signature, which is precisely why the current bare `int`
  produces the schema printed above.

---

## Summary

| Finding | Verdict | Severity |
|---|---|---|
| 20,000-char SMILES SIGSEGV | CONFIRMED | critical |
| 8 × 4 KB `render_structure` takes the pod out of service | CONFIRMED (mechanism amended: GIL, not executor) | high |
| `rxnpredict` sync tools on the event loop | CONFIRMED | high |
| `top_k` unbounded on the served surface | CONFIRMED (OOM sub-claim unmeasured) | high |

Nothing in scope was refuted. Two things the reporter missed, both making the picture worse:
`render_structure` with `"C"*20000` kills the process by **SIGKILL/memory** (exit 137) as well as
by SIGSEGV, and the depiction outage is **GIL starvation**, so `depiction.py` and `tools.py` both
carry a docstring asserting the opposite of what I measured — and one of the three proposed fixes
(a private bounded executor) would not restore `/healthz`.
