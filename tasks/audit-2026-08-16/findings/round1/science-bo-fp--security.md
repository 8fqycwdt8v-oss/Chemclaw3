# science/bo + science/fingerprints — security and hardening

Slice: `src/chemclaw/science/bo/` (problem, engine, objectives, featurize, progress, campaign,
campaign_record, campaign_record_store, benchmarks) and `src/chemclaw/science/fingerprints/`
(molfp, rxnfp, store).

Reachability was established by reading the MCP tool boundaries that call into this slice
(`connectors/bo/server/tools.py`, `connectors/molfp/server/tools.py`,
`connectors/rxnfp/server/tools.py`) — the untrusted input is the tool-call arguments the model
emits, and `connectors/bo/connector.yaml` declares `auth: mode: none` for that pod. All scripts
below were run with `uv run` against the live venv; the Postgres one against `make up` + `make
db-migrate`.

---

## A NaN `threshold` escapes `find_matches`' clamp and turns a populated index into a "genuine negative result"

- **Severity**: high
- **Location**: `src/chemclaw/science/fingerprints/store.py:475` (`find_matches`), consumed by
  `store.py:146-174` (`FingerprintSearch.verdict`), reached from
  `connectors/molfp/server/tools.py:36` (`similar_molecules`) and
  `connectors/rxnfp/server/tools.py` (`similar_reactions`)
- **Trigger**: a tool call with `threshold` set to a NaN. Two spellings both work: the JSON
  *string* `"nan"` (ordinary, standard JSON — FastMCP's pydantic lax-mode coercion turns it into
  `float('nan')`), or the bare `NaN` literal, which Python's `json.loads` accepts by default.
  `top_k` and `smiles` can be anything valid.
- **Consequence**: `t = min(max(t, 0.0), 1.0)` is not a clamp for NaN — every comparison against
  NaN is `False`, so both `max` and `min` return NaN unchanged. NaN then lands in the similarity
  comparison, where nothing can be `>= NaN`, so **zero rows come back from a fully populated
  index**. `index_is_empty` is then asked, correctly answers `False`, and
  `FingerprintSearch.verdict` renders:

  > "No indexed molecule matched this query. The molecule fingerprint index holds records and was
  > searched, so this is a genuine negative result."

  That sentence is the single thing this module exists to get right — its own docstring cites a
  live run where a chemist was told "we have never made anything like this" over an unsearched
  corpus, and `verdict` is a `computed_field` specifically so the caveat reaches the model. A NaN
  threshold reproduces exactly that harm while defeating every guard built against it: `index_empty`
  is false, `scan_truncated` is false, `hits_truncated` is false. It is a "no precedent exists"
  answer manufactured from one tool argument.
- **Evidence**: the docstring at `store.py:467-470` asserts the property that is missing —
  "`threshold` is equally model-supplied and lands in the SQL similarity comparison, so it is
  clamped to `[0, 1]` ... outside it, a negative value blesses disjoint structures as neighbors and
  >1 silently returns 'no precedent' instead of an exact match." `-inf` and `+inf` *are* clamped;
  NaN is not, and NaN produces the exact failure the sentence names.

  `/tmp/t_nan.py`:
  ```
  clamped threshold for NaN input -> nan isnan: True
  clamped for -inf -> 0.0
  clamped for +inf -> 1.0
  ```
  `/tmp/t_mcp_nan2.py` — the value survives the MCP boundary, so the trigger is a plain tool call:
  ```
  threshold='nan' -> nan isnan=True
  threshold='NaN' -> nan isnan=True
  ```
  `/tmp/t_verdict.py` — the in-memory backend, five indexed molecules:
  ```
  normal: 2 hits | 2 indexed molecule(s) matched this query.
  NaN   : 0 hits | index_empty: False
  VERDICT: No indexed molecule matched this query. The molecule fingerprint index holds records
           and was searched, so this is a genuine negative result.
  ```
  `/tmp/t_pg_nan.py` — the production `PostgresFingerprintStore` against live Postgres, four rows
  inserted under the current definition:
  ```
  threshold=   0.3 -> 3 hits, is_empty=False
  threshold=   nan -> 0 hits, is_empty=False
  ```
- **Fix**: make the clamp total. In `find_matches`:
  ```python
  t = threshold if threshold is not None else settings.fingerprint_similarity_threshold
  if math.isnan(t):
      raise FingerprintError("threshold must be a number in [0, 1]; got NaN")
  t = min(max(t, 0.0), 1.0)
  ```
  Refusing rather than substituting is the right half here: a NaN threshold is a malformed request,
  and silently rewriting it to the default would answer a different question than the one asked.
  The same `isnan` guard belongs on `top_k`'s float siblings anywhere else a model-supplied float
  reaches a comparison. A test asserting `-inf/+inf/nan` (not just `-1`/`2`) would have caught it.

---

## `campaign_progress` enumerates 2^k cells on the connector's event loop — a 2.8 KB tool call wedges the whole process

- **Severity**: high
- **Location**: `src/chemclaw/science/bo/problem.py:1048-1081` (`discrete_candidate_count`), called
  from `src/chemclaw/science/bo/progress.py:238` (`design_space=discrete_candidate_count(problem)`),
  reached from `connectors/bo/server/tools.py:638` — which calls `read_progress(...)` **directly,
  not through `asyncio.to_thread`**, unlike every other BO tool in that file.
- **Trigger**: one `campaign_progress` call whose `problem` declares N binary categorical
  parameters and **one** `ExcludeConstraint`, plus a single observation. N=40 is a 2,799-byte
  payload. The exclusion is what matters: without one, `discrete_candidate_count` returns the
  product and stops; with one, it takes the `product(*options)` branch and walks every cell.
- **Consequence**: the connector's event loop blocks for the whole enumeration. Not the calling
  task — the loop, so every other in-flight MCP request and every SSE stream that process is
  serving stalls with it. Measured rate is ~375,000 cells/s, so N=40 is ≈ 34 CPU-days in a single
  un-yielding Python loop. There is no timeout above it (the connector's `request_timeout: 120` is
  the *client's* deadline; the server keeps running), no cancellation point, and no factor cap
  anywhere: `OptimizationProblem.parameters` is `Field(min_length=1)` with no maximum and
  `CategoricalParameter.categories` is `Field(min_length=2)` with no maximum.

  `campaign_progress` is declared `read_only` in `connectors/bo/connector.yaml`, i.e. it sits behind
  the *weakest* authorization gate of the five BO tools — the tool least likely to be refused is the
  one that can stop the pod.
- **Evidence**: `/tmp/t_space.py` — cost of the enumeration alone:
  ```
  n= 18  cells=2^18=     262,144  feasible=     196,608     0.619s
  n= 20  cells=2^20=   1,048,576  feasible=     786,432     2.738s
  n= 22  cells=2^22=   4,194,304  feasible=   3,145,728    11.160s
  ```
  `/tmp/t_loopblock.py` — the real tool coroutine with a 10 ms heartbeat task alongside it,
  N=20 (a deliberately small N so the script finishes):
  ```
  campaign_progress returned in 2.59s, design_space=786432
  other-session heartbeat: 20 ticks, worst stall 2.59s
  ```
  20 ticks where ~280 were due: the loop was dead for the entire call.

  `/tmp/t_accept40.py` — nothing rejects the real payload:
  ```
  40-parameter problem ACCEPTED by every validator; payload is 2799 bytes
  cells discrete_candidate_count will enumerate: 1099511627776
  ```
  The comment at `problem.py:1060-1063` is the claim that fails: "this space is small by
  construction: it is the space a unique-seeding loop already walks one point at a time." That is
  true of the *seeding* caller, which reaches the function after `initial_candidates` has bounded
  `n` — it is not true of `campaign_progress`, which hands the function a caller-controlled problem
  and asks for the count as a display field.
- **Fix**: two changes, both cheap.
  1. Bound the enumeration in `discrete_candidate_count` itself, since it is the function that owns
     the exponential: compute `total` first (it already does) and refuse to enumerate above a cap —
     `if total > settings.bo_max_enumerated_cells: raise ValueError(...)` with a message naming the
     factor count. A space larger than ~10^6 cells is not one any of the three callers can act on
     anyway (`space_exhausted` and `_require_fresh_points_exist` are both about running out of
     points).
  2. Cap the declared factor count at the tool boundary the way `find_matches` caps `top_k`, and
     move `read_progress` onto `asyncio.to_thread` so `campaign_progress` matches the other four
     tools in that file — a CPU-bound call on an async server's loop is the defect independent of
     the cap.

---

## `generate_screening_design` builds a 2^k-row design with no factor cap — memory exhaustion from a ~2 KB call

- **Severity**: high
- **Location**: `src/chemclaw/science/bo/engine.py:831-853` (`_full_design`) and
  `engine.py:870-933` (`factorial_design`), reached from
  `connectors/bo/server/tools.py:671` (`generate_screening_design`, also `read_only`)
- **Trigger**: `generate_screening_design` with a problem declaring k two-level categorical
  parameters and no constraints. `factorial_design` validates `n_generators >= 0`,
  `n_center >= 0`, `n_repetitions >= 1` and the constraint refusal — and then hands the domain to
  BoFire's `FractionalFactorialStrategy`, which enumerates every corner. Nothing bounds k.
- **Consequence**: the full grid is 2^k rows, materialized as a pandas frame, then rebuilt as a
  Python `list[dict]` on `ScreeningDesign.runs`, then serialized whole into the MCP response. k=25
  is ~33.5 M runs; k=30 is ~10^9. The process is OOM-killed long before it answers, and the
  request that does it is a couple of kilobytes. `n_repetitions` multiplies it further with the same
  absence of a ceiling.

  This is the same class of unbounded-output hazard the fingerprint side treats as a named security
  control — `fingerprint_max_top_k` exists, per its own config comment, because "`top_k` reaches
  `find_matches` from the model ... so an arbitrarily large value would be an unbounded query"
  (SEC-4). The BO tool surface has no analogue.
- **Evidence**: `/tmp/t_factorial.py`, measured growth (baseline RSS ~990 MB is the imported
  torch/bofire stack):
  ```
  n=  8 factors ->        256 runs in    0.02s, peak RSS      990 MB
  n= 12 factors ->      4,096 runs in    0.31s, peak RSS      995 MB
  n= 15 factors ->     32,768 runs in    2.52s, peak RSS     1030 MB
  n= 17 factors ->    131,072 runs in   10.66s, peak RSS     1162 MB
  ```
  4x per two added factors, in both time and memory; k=25 extrapolates to ~45 GB and ~45 minutes,
  k=30 to certain OOM. Note also that k=17 already produces a ~30 MB JSON response aimed at the
  model's context.

  The nearest thing to a bound in the module is a comment, not code —
  `problem.py:566-571` (`_roman`) says it "Covers 1–39, which is every resolution a real design can
  have: ... a 40-factor two-level screen is not a thing anyone runs." That is a statement about
  chemists, not about callers.
- **Fix**: refuse the design before generating it. In `factorial_design`, compute the run count
  from the declared levels (`prod(len(p.categories) for categorical) * 2**n_continuous *
  n_repetitions`, adjusted for `n_generators`) and raise a `ValueError` naming the number when it
  exceeds a new `bo_max_design_runs` setting (a few thousand is generous — the tool's own docstring
  frames the useful range as "does it fit a 96-well plate"). The refusal is also the *better*
  answer: a 33 M-run screen is not something a human can run, so returning it was never useful.

---

## `count` / `points` at the BO tool boundary are unbounded, unlike `top_k` on the fingerprint boundary

- **Severity**: medium
- **Location**: `src/chemclaw/science/bo/engine.py:336` (`initial_candidates`) and
  `engine.py:387` (`propose_candidates`), reached from `connectors/bo/server/tools.py:546-548`
  with the raw `count: int = 1` argument; and `engine.py:528` (`_predictions_from`) reached from
  `predict_outcome` with the raw `points` list.
- **Trigger**: `suggest_next_experiment` with a continuous (or mixed) problem, no observations, and
  `count: 20000000`. The discrete path is protected (`n > space` is refused at `engine.py:352`);
  the continuous path is not, because `discrete_candidate_count` returns `None` and the code goes
  straight to `strategy.ask(n)`.
- **Consequence**: linear but uncapped memory and CPU, plus an uncapped response. Measured
  ~1.4 GB RSS and ~27 s per million candidates, and ~155 MB of response JSON per million. `count`
  of 2×10^7 is ~28 GB and ~9 minutes — an OOM kill from a single integer field. `predict_outcome`'s
  `points` has the same shape (`pd.DataFrame` of one row per point, then a GP posterior over all of
  them) with no length check; `_require_points_match` validates each point's *keys* and never the
  list's length.
- **Evidence**: `/tmp/t_count.py` and `/tmp/t_count2.py`:
  ```
  count=     10 ->      10 candidates in    0.01s
  count= 50,000 ->  50,000 candidates in    1.47s
  count=100,000 ->   3.01s, peak RSS  1129 MB (+141), response JSON ~15 MB
  count=500,000 ->  13.53s, peak RSS  1689 MB (+700), response JSON ~77 MB
  ```
  The asymmetry with the fingerprint side is the argument: `find_matches` clamps its model-supplied
  count to `[1, fingerprint_max_top_k]` at "the single chokepoint both entry points share", and the
  identical exposure on the BO tools has no chokepoint at all.
- **Fix**: clamp at the same place the fingerprint side does — a `bo_max_batch` setting applied
  inside `initial_candidates`/`propose_candidates` (so the durable workflow's `batch` is covered by
  the same bound, not only the inline tool), and a length check on `points` in
  `_require_points_match`. Clamping rather than refusing is right for `count`, matching
  `find_matches`; refusing is right for `points`, since silently dropping questions would misreport
  which conditions were answered.

---

## The durable campaign's "budget ceiling" bounds only `n_rounds`, so it refuses nothing

- **Severity**: medium
- **Location**: `src/chemclaw/science/bo/problem.py:703-725` (`require_rounds_within_ceiling`) and
  `problem.py:824-868` (`require_campaign_startable`, named as the job's `precondition` in
  `connectors/bo/connector.yaml`); the unbounded fields are `CampaignSpec.n_initial`
  (`problem.py:695`, `ge=MIN_SEED_OBSERVATIONS`, no maximum) and `CampaignSpec.batch`
  (`problem.py:697`, `ge=1`, no maximum).
- **Trigger**: `start_optimization_campaign` with `{"n_initial": 1000000000, "n_rounds": 500,
  "batch": 1000000000}`. `n_rounds` is at, not above, the configured `bo_max_rounds=500`, so the
  only check that runs passes.
- **Consequence**: the total evaluation budget is `n_initial + n_rounds * batch`, and the ceiling
  constrains exactly one of its three terms. `require_rounds_within_ceiling`'s own docstring states
  the property it is supposed to have — "What this ceiling refuses is a spec that would spend
  thousands of evaluations, which is a mistake worth catching before the first one is paid for" —
  and a spec for 5×10^11 evaluations is accepted. Each of those evaluations is a real objective call
  (`solubility_max` reaches an MCP calculator and writes `calculation_results`), so the accepted
  spec is an unbounded spend authorized at launch by a precondition that reports "startable".
- **Evidence**: `/tmp/t_count.py`:
  ```
  spec accepted: n_initial=1000000000 n_rounds=500 batch=1000000000 -> evaluations = 5.01e+11
  ```
  Also note the surrounding comment at `config/bo.py:34-39`, which correctly records that the
  ceiling's *stated* purpose was already once wrong ("It used to be described as protecting the
  event-history limit, and did not") — the replacement rationale is the budget one, and that is the
  one the code does not implement.
- **Fix**: bound the product, not one factor. In `require_campaign_startable`:
  ```python
  budget = spec.n_initial + spec.n_rounds * spec.batch
  if budget > settings.bo_max_evaluations:
      raise ValueError(f"this spec would spend {budget} evaluations; the ceiling is ...")
  ```
  Keep it in `require_campaign_startable` and **not** on `CampaignSpec` — that module's standing
  argument (validators re-run at Temporal replay, where a lowered ceiling must not fail an in-flight
  campaign) applies here unchanged and is correct.

---

## `substructure_matches`: the documented timeout does not free the event loop, because RDKit does not release the GIL

- **Severity**: medium
- **Location**: `src/chemclaw/science/fingerprints/molfp/search.py:168-177` (the
  `asyncio.wait_for(asyncio.to_thread(_scan_for_matches, ...))` guard) and its docstring at
  `search.py:134-140`; the same claim is restated in `config/fingerprints.py:60-69`.
- **Trigger**: any `substructure_matches` call whose RDKit work outlives the timeout — the case the
  docstring itself names ("a short but adversarial recursive SMARTS can still match for minutes").
- **Consequence**: the stated mitigation is that "the timeout releases the event loop and the
  caller — it cannot kill the RDKit thread, which holds one CPU". The first half is false. RDKit's
  Python bindings hold the GIL for the duration of a single C++ call, and a GIL-holding worker
  thread starves the event loop just as a synchronous call would. `asyncio.wait_for` cannot even
  fire its own timer: measured, a `wait_for(timeout=0.1)` over a single 0.84 s RDKit call returned
  after **0.84 s**, 8× its own bound. So `substructure_match_timeout_seconds` bounds neither the
  caller's wait nor "every other session's stream", which are the two things it is documented to
  bound; only `_SEC_SCAN_MAX_RECORDS`-style input bounds actually apply, and those do not bound a
  single pathological match.
- **Evidence**: `/tmp/t_timeout_lie.py` — one long RDKit call under a 0.1 s `wait_for`, with a 5 ms
  heartbeat task:
  ```
  caller released by wait_for after 0.84s (timeout=0.1s)
  event loop after release: worst single stall 0.84s over the whole run
  ```
  `/tmp/t_substruct_loop2.py` — the real `find_substructure_matches`, 5,000 in-memory records and a
  miss pattern (so the whole corpus is scanned):
  ```
  full-corpus miss scan: 0.99s, 0 hits
  event loop: 88 ticks over 1.19s (ideal ~237) = 37% of expected; worst stall 0.14s
  ```
  A routine scan already runs the loop at ~37% of its rate; the per-call stall is bounded only by
  the length of one RDKit call, which nothing in this repo bounds.

  **Honest limit on this finding**: I could not construct a multi-second single SMARTS match within
  the audit budget — RDKit's matcher prunes wildcard chains by degree, and it caches recursive
  (`$(...)`) environments per molecule, so neither of the two obvious pathological forms was slow
  (`/tmp/t_slow_smarts.py`, `/tmp/t_rdkit_gil2.py`: all under 3 ms at 500-character patterns). So the
  *mechanism* is measured and the docstring's claim is measurably false; the *worst-case stall* rests
  on the module's own stated threat model rather than on a pattern I demonstrated.
- **Fix**: either correct the claim or make it true. Correcting it is one sentence: the timeout
  releases the caller's *await* only after the GIL comes back, and it does not protect other
  sessions — so the real control is the record cap and the query-length cap. Making it true means a
  subprocess (`ProcessPoolExecutor` with a hard kill on timeout), which the config comment already
  identifies as the only real answer and defers. At minimum, do not let a second such call in while
  one is outstanding: `asyncio.to_thread` uses the *shared default* executor, so repeated timed-out
  calls accumulate GIL-holding threads that no later timeout can reclaim — a bounded, dedicated
  executor for this one call site would keep the damage from compounding.

---

## `resume_campaign` fetches by a derivable id with no ownership scoping and returns another principal's identifier

- **Severity**: low
- **Location**: `src/chemclaw/science/bo/campaign_record.py:442-477` (`read_campaign_thread`),
  `campaign_record_store.py:49-52` (`_SELECT_CAMPAIGN`, keyed on `campaign_id` alone), returned by
  `connectors/bo/server/tools.py:642` (`resume_campaign`)
- **Trigger**: a `resume_campaign` call with any `campaign_id`. The id is not a capability: it is
  `f"campaign-{stable_hash(identity)}"` over the canonicalized decision space
  (`campaign_record.py:126-167`), with an unkeyed SHA-256 (`core/ids.py:34`), so any caller who can
  describe the same optimization space — the deliberate design, since "Two chemists optimizing the
  same space converge on the same row" — can compute another chemist's campaign id offline and read
  it.
- **Consequence**: the returned `CampaignThread` carries that campaign's observations (measured
  yields from someone else's lab work), the candidates last proposed, and `opened_by` — the actor
  string written from `X-Chemclaw-Actor`, i.e. an Entra `oid` or a `unverified:`-prefixed claim.
  Neither the store query nor the tool consults the caller's identity, so this is authorization by
  tool name only, never by resource. The codebase demonstrates it knows the other shape: both
  `kg/proposal_store.py:76` and `agent/turn_cost_store.py:51` carry a self-disabling
  `AND (%s = '' OR actor = %s)` arm on their reads.
- **Evidence**: `_SELECT_CAMPAIGN` is `... FROM bo_campaigns WHERE campaign_id = %s` with no second
  predicate; `read_campaign_thread(campaign_id)` takes no actor and calls no
  `require_actor()`-equivalent; `CampaignThread.opened_by` (`campaign_record.py:438`) is populated
  at `campaign_record.py:475`.

  **Weighed against the deployment's declared posture**, which is why this is low rather than
  higher: `api/routes/jobs.py:29-33` states the same table-level position for `job_records`
  explicitly — "Not owner-scoped, and that is the deployment's existing position rather than an
  oversight ... a read that the agent can make on a chemist's behalf is not one to withhold from
  the chemist." Campaign records are consistent with that. What is *not* covered by that argument
  is `opened_by`: the jobs rationale is about the work, and returning a second principal's
  identifier to the model that composes an answer is a different disclosure than returning the
  runs.
- **Fix**: drop `opened_by` from `CampaignThread`, or replace it with a boolean
  (`opened_by_you`) computed against the caller. The field has no consumer that needs the raw
  identifier — the tool docstring never mentions it and the resume flow does not read it — so
  removing it costs nothing and closes the only part of this read that is a principal disclosure
  rather than a shared scientific record. If per-actor scoping of the whole row is ever wanted,
  add the same self-disabling `AND (%s = '' OR opened_by = %s)` arm the two stores above use, and
  decide it once at the tool rather than inside `read_campaign_thread`.

---

## Checked and found clean (through this lens)

Recorded so the negative results are visible:

- **SQL injection**: every statement in `campaign_record_store.py` and every statement in
  `PostgresFingerprintStore` is parameterized. The two interpolations in
  `PostgresFingerprintStore.__init__` are `table` (guarded by `isidentifier()`, and supplied only by
  `default_molecule_store`/`default_reaction_store` as literals) and `width` (unguarded, but typed
  `int` and supplied only from `settings.ecfp_bits`/`drfp_bits`, both `int` pydantic fields). No
  untrusted caller exists for either; the docstring's claim that the identifier check "enforces that
  trust boundary against any future caller" is true of `table` and silent about `width`, which is
  worth a one-word note but is not a finding.
- **Secrets in logs**: the only `exc_info=True` in the slice
  (`campaign_record.py:412`) can surface a psycopg error, and `core/db.py:66-80` redacts the DSN
  password before it reaches any message. No credential, token or DSN reaches a log line here.
- **Unbounded reads**: `PostgresFingerprintStore.all_records(limit=None)` is a full-table `SELECT`
  with no cap, but its one caller (`molfp/search.py:159`) always passes `cap + 1`.
  `suggestions_for(campaign_id, limit)` puts `limit` in a bound parameter and its one caller passes
  `1`.
- **Unsafe deserialization / dynamic import**: none. `objectives._REGISTRY` is a literal dict; the
  only name-to-callable resolution (`get_objective`, `registered_direction`) is a dict lookup that
  raises on an unknown name and never imports. `campaign_store()` branches on
  `settings.session_store == "postgres"` — a config literal, not a caller value.
- **Path traversal / command construction**: no filesystem or subprocess use in the slice; the one
  file read is `benchmarks/reizman_suzuki.py:29`, a module-relative constant.
- **SSRF**: no outbound client. `featurize.PropertiesFor` and `objectives.LogSFor` are injected
  callables, which is what keeps the network client out of this layer.
- **Timing-unsafe comparison**: no secret comparison anywhere in the slice.
- **TOCTOU**: `PostgresCampaignStore.record` correctly wraps both writes in one transaction, and the
  `DO NOTHING` → `SELECT` read-back at `campaign_record_store.py:145-151` is inside it.
