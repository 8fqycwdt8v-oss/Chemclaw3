# Verification — `mcp-safety-props--security.md`, reachability lens

Scope: the three **high** findings. No criticals were filed. Medium/low ignored.

## The one fact that decides two of the three

Both servers sit behind a single caller. `servers/safety/deploy/networkpolicy.yaml` admits `/mcp`
only from `app.kubernetes.io/name: chemclaw` (and `monitoring` for `/metrics`), and on the backend
side there is **no route that invokes a tool by name with caller-supplied arguments** — the whole
route set is `src/chemclaw/api/routes/{approvals,jobs,notes,ops,plan,proposals,results,sessions,streams,turns}.py`
and `grep -rni "tool_name|invoke_tool|call_tool" src/chemclaw/api/` returns nothing. So every byte
of every tool argument is emitted by the model.

The model's per-response output is capped:

```
src/chemclaw/core/config/llm.py:80   llm_max_tokens: int = Field(default=4096, gt=0)
/home/user/Chemclaw3/.env.example:576  CHEMCLAW_LLM_MAX_TOKENS=4096
src/chemclaw/agent/llm_provider.py:299  options = {"max_tokens": settings.llm_max_tokens}   # both provider paths, lines 263 and 282
```

Nothing in `deploy/helm/chemclaw/values.yaml` overrides it. A response that hits the cap truncates
the `tool_use` block mid-JSON, so an over-cap argument is not "slow to send" — it is never
dispatched.

I measured the payloads with the **real Anthropic tokenizer** (`messages.count_tokens`,
`claude-sonnet-4-5`, using the environment's `API-KEY`), not an estimate:

```
{"substance": "C"*8100}      ->   4,063 tokens      (fits)
"C"*20000 (the F1 trigger)   ->  10,008 tokens      (2.4x over cap)
64 crafted components (F2)   ->   4,298 tokens      (just over; 60 fits)
{"names": ["dcm"]*100000}    -> 400,011 tokens      (98x over cap)
```

That is the axis the finding file never checked, and it separates the three findings cleanly.

---

## `ich_impurity_limit` runs unbounded RDKit canonicalization on the event loop

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium (filed as high)

- **What I did**

  Confirmed the mechanism first — the code path is exactly as described and the two docstrings
  asserting "dictionary lookup" are false. `impurity_limit` → `resolve_compound_name` →
  `require_canonical_smiles` → `MolFromSmiles` + `MolToSmiles` on the raw query.

  Engine cost (`/tmp/av/f1.py`):

  ```
  len=1000   0.063s   len=5000  0.518s
  len=10000  2.130s   len=15000 4.737s   len=18000 7.002s
  ```

  End-to-end against real uvicorn + `chemclaw_mcp_safety.app:app`, with a `/healthz` prober every
  50 ms (`/tmp/av/e2e.py`), at **8,100 chars — the largest `substance` a 4,096-token response can
  carry**:

  ```
  chemclaw_mcp_safety.app.ich_impurity_limit
    request=8,225B duration=1.466s response=33,021B amp=4.0x
    healthz probes n=2 max=1.361s median=1.3613s
  ```

  At the finding's stated 20,000 chars the server does **not** run for 300 s. It dies
  (`/tmp/av/crash.py`, real HTTP):

  ```
  client: RemoteProtocolError Server disconnected without sending a response.
  uvicorn returncode: -11
  healthz after: DEAD - ConnectError
  ```

  `-11` is SIGSEGV. Standalone confirms it and locates it (`/tmp/av/split.py`, `/tmp/av/seg.py`):
  `parse 0.038s atoms=20000` then `exit=139` — the crash is a stack overflow inside `MolToSmiles`,
  not the parser, at the default 8 MB stack. Boundary measured: 18,000 succeeds in 7.0 s;
  19,000 / 20,000 / 25,000 / 30,000 all `exit=139` in ~10 s. Nested-branch SMILES do **not**
  reproduce it (`"C"+"(C"*5000+")"*5000` writes in 0.504 s) — it needs a long linear chain.

- **Why**

  The mechanism is real and the docstrings are wrong, so the finding is not refuted. But both
  halves of its headline are off, in opposite directions, and the net is a smaller finding.

  *Reachability.* `"C"*20000` is 10,008 tokens against a 4,096-token ceiling. The largest reachable
  `substance` is ~8,100 characters, and even that assumes the model emits nothing else in the
  response — no preamble text, no second tool call. So the reachable cost is the **1.36 s** of
  blocked loop measured above, not 4.9 s and not "still running after 300 s". The finding's
  headline number is 2.4x outside what the only caller can produce.

  *Consequence.* The reporter's "TIMEOUT>300s" row is a measurement error that hid a worse outcome:
  above ~19,000 characters the process segfaults and takes every concurrent turn and the liveness
  endpoint with it. That is the finding I would have filed — and it is **also** unreachable at the
  shipped `llm_max_tokens=4096` (9,508 tokens needed). It is one config knob away, though: any
  deployment that raises `CHEMCLAW_LLM_MAX_TOKENS` above ~9,600 — an ordinary thing to do for a
  reasoning-heavy agent — makes a full crash of the shared `safety` server reachable from one chat
  turn. That is worth writing into the fix rationale, because it means the input bound is the
  load-bearing half of the fix and `asyncio.to_thread` is not: a thread hop makes the hang
  concurrent, and does nothing whatsoever about a segfault.

  *What a chemist is shown.* Nothing wrong. A bogus SMILES misses both tables and returns
  `limit: null` with the honest-miss verdict (measured: `limit=None` at every length). This is
  availability only — there is no path here to a fabricated ICH number.

  Medium rather than high: the reachable harm is ~1.4 s of one shared server's loop per call, with
  the answer still correct. Both proposed fixes are right and I would keep them, with the length
  guard prioritised over the thread hop for the reason above.

---

## `MAX_COMPONENTS` bounds the component count, not the response — 6 KB in, 29.6 MB out

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (as filed)

- **What I did**

  Reproduced the stated payload end-to-end over HTTP with the `/healthz` prober, and then again at
  the largest size the model can actually emit:

  ```
  ### F2 screen_hazards n=64   (the finding's payload; 4,298 tokens - just over the cap)
    request=6,100B duration=2.060s response=29,563,649B amp=4846.5x
    healthz probes n=6 max=1.656s median=0.0142s

  ### F2 screen_hazards n=60   (4,050 tokens - inside the 4,096 cap)
    request=5,606B duration=1.840s response=25,797,869B amp=4601.8x
    healthz probes n=6 max=1.276s median=0.0152s
  ```

  Verified the comment being challenged (`screen.py:70-72`) says "~1,000 pair flags and single-digit
  milliseconds", and `MAX_COMPONENTS = int(os.environ.get("CHEMCLAW_SAFETY_MAX_COMPONENTS", "64"))`
  at `screen.py:74` is indeed unvalidated.

  Then checked what the 26 MB does on the backend side. `src/chemclaw/agent/compaction.py:264-268`
  configures `ClearToolUsesEdit(keep=settings.agent_keep_last_tool_groups)`, default **2**
  (`core/config/agent.py:87`) — the newest tool results are kept **verbatim**. So compaction does
  not clip this.

- **Why**

  Everything survives. The trigger is producible by the real caller: dropping from 64 to 60
  components brings the payload inside the 4,096-token ceiling and costs only 13% of the
  amplification — still 25.8 MB out of 5.6 KB in, still 1.28 s of a shared single-loop server
  frozen. `service_rate_limit_per_minute` defaults to **0.0**, i.e. disabled
  (`core/config/service.py:147`), so nothing on the front door meters repetition; only
  `service_max_concurrent_turns=8` bounds it, and eight concurrent turns are enough to keep the
  loop saturated.

  One thing the reporter understated and one it overstated.

  Understated: "24,192 flags land in the agent's context window, which is not a result any model can
  report" is milder than what happens. Because `ClearToolUsesEdit` keeps the newest two tool results
  verbatim, the 26 MB is not compacted — it is persisted to the Postgres checkpointer and then sent
  to the provider as roughly six million tokens against a 200k window. The turn dies on a provider
  error. The chemist who asked for a hazard screen gets a failed turn, and the failure is on the
  *safety* path, which is the one where a user is most likely to read "it broke" as "nothing was
  found". That is worse than a merely unreportable result.

  Overstated: "a bandwidth amplifier against whatever sits in front of the server". The
  NetworkPolicy admits only the `chemclaw` pod and the monitoring namespace, so the 26 MB is
  intra-cluster backend↔safety traffic. It is not an external reflector, and there is no
  third-party victim. That is a paragraph to delete, not a severity change.

  The `CHEMCLAW_SAFETY_MAX_COMPONENTS` sub-point is real but is operator-set configuration, not
  attacker input — worth a `Field`-style bound, not part of the security case.

  High stands. Fix the output bound, not just the cap.

---

## `props.compare_solvents` takes an unbounded list — 700 KB in, 81.6 MB out, 15 s of frozen server

- **Verdict**: REFUTED
- **Severity I would assign**: low

- **What I did**

  The stated numbers reproduce exactly, so the mechanism is not in question:

  ```
  ### F3 compare_solvents n=100000
    request=700,117B duration=14.894s response=81,601,345B amp=116.6x
    healthz probes n=6 max=14.479s median=0.0062s
  ```

  Then checked whether anything can send it. Two independent blocks, either one sufficient:

  1. **The backend does not serve this connector.**
     ```
     $ ls /home/user/Chemclaw3/src/chemclaw/connectors/*/connector.yaml
     .../bo/  .../calc/  .../chem/  .../molfp/  .../qm/  .../rxnfp/  .../safety/
     ```
     No `props`. `grep -rn "props" .env.example deploy/helm/chemclaw/values.yaml` returns nothing,
     and `CHEMCLAW_CONNECTORS_DIR` is commented out at `.env.example:267`, so `connectors_dir`
     resolves to the shipped directory above. The manifest exists only in the MCP repo
     (`manifests/props/connector.yaml`), to be dropped in by a deployment that has chosen to. No
     shipped configuration puts `compare_solvents` in front of the model.

  2. **Even installed, the payload is 98x over the caller's ceiling.**
     `{"names":["dcm"]*100000}` is **400,011 tokens** against `llm_max_tokens=4096`. Measured at the
     largest reachable sizes:
     ```
     n=1000  (~4,000 tok)  request=7,117B duration=0.203s response=817,345B   healthz max=0.094s
     n=1350  (5,411 tok - already over cap)  duration=0.249s response=1,102,945B  healthz max=0.140s
     ```

- **Why**

  Every number in the headline is a number no caller can cause. "15 s of frozen server" is
  **0.09 s**; "81.6 MB out" is **0.8 MB**; "700 KB in" is **7 KB**. That is two orders of magnitude
  on the consequence, on a tool that is not in any shipped deployment's tool surface in the first
  place. The severity label was carrying the 100k figure, and the 100k figure cannot happen.

  What is left is true and small: `names` has no declared bound, so a caller-shaped-by-nothing list
  is echoed back at ~115x. If `props` is ever enabled, an 817 KB tool result is still an unpleasant
  thing to put in a context window, and the bound the finding proposes (the table holds 44 solvents;
  more than ~32 names is not a comparison) is the obviously correct code. Fix it — as hygiene, at
  low, alongside the `top_n` clamp.

  On that `top_n` sub-point: it is real. `servers/props/src/chemclaw_mcp_props/engine/selection.py:166`
  is `return scored[:top_n]`, so a negative `top_n` returns all-but-the-last-N. It is a correctness
  bug in `solvent_swap_candidates`, it is unrelated to the finding it is appended to, and it
  deserves its own row rather than to be refuted along with its host.

  Note also that this finding's premise sentence — "No authentication of *scale* exists: the bearer
  token is per-server, so any holder can repeat this" — is not a threat model. The token holder is
  the backend pod, and the NetworkPolicy admits no one else. An attacker who holds the props bearer
  token has the backend, at which point a slow solvent comparison is not the interesting outcome.
