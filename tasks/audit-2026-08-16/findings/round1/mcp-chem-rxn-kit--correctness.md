# Round 1 — `Chemclaw3-mcp`: `servers/chem`, `servers/rxnpredict`, `packages/mcp_server_kit`, `manifests/`, Makefile + pyproject

Lens: **correctness** — wrong answers, crashes, lost work, silently dropped data.

Baseline: `uv run pytest -q servers/chem servers/rxnpredict packages/mcp_server_kit` → **371 passed**.
Every finding below is reproduced with a script under `/tmp/audit/`; the printed output is quoted
inline. The working tree was left clean (`git status --porcelain` empty).

Two findings are high: both are in the ensemble aggregation, and both make the consensus that
`rxnpredict` sells as its whole value — "four of five models agree" — report the wrong answer or the
wrong agreement. Nothing in this slice is critical: no data is persisted, no work is lost, and no
input I could construct crashed a shipped server.

---

## A skipped beam gives a model gapped ranks, and the aggregator divides by the rank — flipping the consensus top-1

- **Severity**: high
- **Location**: `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/predictors/forward/reaction_t5.py:102` (`ReactionT5V2Forward.predict_sync`, `rank=i + 1`), and identically `forward/chemformer.py:89`, `forward/t5chem.py:92`, `forward/molecular_transformer.py:117`, `forward/megan.py:82`, `forward/graphrxn.py:70` — i.e. **every** forward predictor. Consumed at `engine/meta/aggregator.py:84` (`contribution = prior * p.score / p.rank`).
- **Trigger**: any forward call where a model's beam 0 (or any earlier beam) decodes to a string RDKit refuses, or to an empty string. `predict_sync` loops `for i, seq in enumerate(sequences)`, `continue`s on the unparseable one, and stamps `rank=i + 1` from the **raw beam index**. The surviving best prediction is therefore emitted at rank 2, not rank 1. Beam search returning at least one malformed SMILES in a top-5 is routine, which is exactly why the `continue` is there.
- **Consequence**: the aggregator's Borda weight is `prior * score / rank`, so that model's genuine top hit contributes **half** the weight it should (a third if two beams are skipped). A competing model's product can overtake it. The consensus product a chemist is shown changes because of a decode artefact in an unrelated beam.
- **Evidence**:

  `/tmp/audit/repro_rank.py` — builds exactly what `reaction_t5.predict_sync` builds when beam 0 is unparseable:

  ```
  model_a emitted ranks: [2, 3] products: ['CCOC(C)=O', 'CCOC(C)=O']
  ```

  Note the ranks start at 2 — `ForwardPrediction.rank`'s own description is "1-based rank within
  this model's top-K" (`schemas.py:22`), and `ge=1` is the only thing enforced, so nothing catches it.

  `/tmp/audit/repro_rank2.py` — the same skip flips which product wins, with two models under equal
  trust priors:

  ```
  beam 0 parsed      (rank 1): top-1 = 'CCOC(C)=O'  -> [('CCOC(C)=O', 1.0), ('CCO', 0.889)]
  beam 0 skipped     (rank 2): top-1 = 'CCO'        -> [('CCO', 1.0), ('CCOC(C)=O', 0.562)]
  ```

  The strong model's ethyl acetate (score 0.9) loses rank 1 to the weak model's ethanol (score 0.8)
  purely because a beam the strong model did not even use failed to decode.
- **Fix**: rank by position in the emitted list, not by the source index. In each `predict_sync`,
  replace `rank=i + 1` with `rank=len(preds) + 1` (computed before the `append`), so ranks are
  always contiguous from 1. Add `ForwardPrediction`-level enforcement in the aggregator or a test
  that asserts `[p.rank for p in preds] == list(range(1, len(preds) + 1))` for every predictor —
  `base_doubles.FakeForwardPredictor` already satisfies it, which is why the suite never noticed.

---

## Condition sets that every model agrees on are counted as separate one-vote candidates

- **Severity**: high
- **Location**: `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/meta/aggregator.py:111-122` (`_canon_set`), used to build the vote key at `aggregator.py:172-176`
- **Trigger**: two condition predictors return the same solvent/catalyst/reagent spelled differently. `_canon_set` calls `canonical_smiles(x)` and, on `ValueError`, appends the **raw string** (line 120-121). The condition predictors do not agree on a spelling: `rxn_insight._as_list` and `parrot._as_list` pass whatever the backend emits straight through (names included), while `askcos_condition._as_list` splits on `.` because "ASKCOS sometimes returns dot-separated SMILES strings" (`askcos_condition.py:76`). Nothing in this server resolves a reagent name to a structure — that capability lives in the `chem` server's `resolve_compound` and is never called here.
- **Consequence**: the voting unit is the whole `(catalysts, solvents, reagents, temp_bucket)` tuple, so one differing spelling makes a wholly separate candidate. Unanimous agreement is reported as `vote_count=1` on each of N candidates. `predict_reaction_conditions`' docstring tells the agent that "the voting unit is the whole condition set … so near-agreement between models counts as agreement instead of being split across three almost-identical suggestions", and `tools.py`'s module docstring says `contributing_models` exists "precisely so the agent can say 'four of five models agree'". Here five-of-five agreement reads as no agreement at all, and the agent is being told to quote that number.
- **Evidence**: `/tmp/audit/repro_canonset.py`

  ```
  _canon_set(['THF'])             -> ('THF',)
  _canon_set(['C1CCOC1'])         -> ('C1CCOC1',)
  _canon_set(['tetrahydrofuran']) -> ('tetrahydrofuran',)

  three models, all saying 'THF at 25 C':
    rank=1 score=1.000 votes=1 solvents=['C1CCOC1']        models=['askcos_condition']
    rank=2 score=1.000 votes=1 solvents=['THF']            models=['rxn_insight']
    rank=3 score=1.000 votes=1 solvents=['tetrahydrofuran'] models=['parrot']
  ```

  Honest caveat: no condition backend is installed in this checkout, so I drove the aggregator
  directly with the shapes the three wrappers construct. The defect is in the aggregator's key, not
  in a backend — the raw-string fallback exists *because* non-SMILES arrives, and nothing normalises
  it.
- **Fix**: normalise the vote key through one name/structure resolution before hashing. Either fold
  a small synonym table into this server (the `chem` server's `data/records.csv` is the same corpus)
  or, minimally, lowercase-and-strip the raw fallback and key on that — `('THF',)` and
  `('thf',)` merging is strictly better than nothing. Whatever is chosen, the `vote_count` the tool
  docstring instructs the agent to quote must not be able to under-report unanimity.

---

## `BodySizeLimit`'s streaming counter never produces a 413 on `/mcp` — the only route it guards

- **Severity**: medium
- **Location**: `packages/mcp_server_kit/src/mcp_server_kit/auth.py:161-179` (`BodySizeLimit.counting_receive` / `except _BodyTooLarge`)
- **Trigger**: a chunked POST to `/mcp` (no `content-length`) whose running total exceeds `max_bytes`. `counting_receive` raises `_BodyTooLarge` from inside the MCP streamable-HTTP transport, which runs its request handling inside an anyio task group. The task group wraps the exception in an `ExceptionGroup`, so `except _BodyTooLarge` at line 173 never matches.
- **Consequence**: instead of the documented 413, the request dies with an unhandled `ExceptionGroup`. The class docstring states the opposite in bold — "**Both halves are needed, and the first is not an optimisation** … the running total still guards the chunked case where no such declaration exists" — and records that this class already shipped once with a test that "passed for the wrong reason". It has shipped again in the mirror image: the declared-`content-length` half works, the counting half is dead on the mounted app. `/healthz` and `/metrics` take no body, so `/mcp` is the only route this cap protects.
- **Evidence**: `/tmp/audit/repro_bodycap.py`, driving the real `chemclaw_mcp_chem.app`:

  ```
  declared content-length oversize -> 413 'request body too large'
  chunked oversize                 -> RAISED ExceptionGroup unhandled errors in a TaskGroup (1 sub-exception)
  ```

  `/tmp/audit/repro_bodycap3.py` shows the counter *does* work on a plain FastAPI route, and shows
  the second half of the docstring's own reasoning still holds there:

  ```
  plain route, handler reads body=True:  -> 413 'request body too large'
  plain route, handler reads body=False: -> 200 '{"ok":true}'
  ```

  The only test for this class (`packages/mcp_server_kit/tests/test_auth.py:113-121`) posts a body
  with a declared `content-length` at a plain `@app.post("/mcp")` route — it exercises neither the
  chunked path nor the mount.
- **Fix**: do not signal through an exception raised inside the wrapped app. Have
  `counting_receive` return `{"type": "http.disconnect"}` once the cap is passed and set the
  `refused` flag, then, after `await self._app(...)` returns, send the 413 if nothing was sent yet
  — or catch `BaseExceptionGroup` and walk it for `_BodyTooLarge`. Add a test that posts a chunked
  oversize body **through `connector_app`'s mounted MCP app**, not through a bare route.

---

## Temperature bucketing splits near-agreement at every bin edge

- **Severity**: medium
- **Location**: `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/meta/aggregator.py:125-134` (`_temperature_bucket`)
- **Trigger**: two condition predictors propose temperatures straddling a multiple of 10 — e.g. 29 °C and 31 °C, or −1 °C and +1 °C.
- **Consequence**: fixed 10 °C bins merge suggestions up to 9 °C apart and split suggestions 2 °C apart. `predict_reaction_conditions`' docstring promises the opposite: bucketing exists "so near-agreement between models counts as agreement instead of being split across three almost-identical suggestions". The code comment defends the choice of `floor` over rounding ("25 and 28 would otherwise land in different bins") — that argument is about rounding mode and does not address the bin edge, which is where the promised property actually fails.
- **Evidence**: `/tmp/audit/repro_canonset.py`

  ```
  29.0 C vs 31.0 C -> 2 candidate(s): T=29.0 votes=1, T=31.0 votes=1
  20.0 C vs 29.0 C -> 1 candidate(s): T=24.5 votes=2
  -1.0 C vs 1.0 C  -> 2 candidate(s): T=-1.0 votes=1, T=1.0 votes=1
  ```
- **Fix**: quantisation cannot deliver "near-agreement counts as agreement" — merge on proximity
  instead. Aggregate the non-temperature part of the key first, then cluster the temperatures inside
  each `(catalysts, solvents, reagents)` group with a ±10 °C tolerance (single-linkage over the
  sorted temperatures is enough), and report the group mean. If the bins are kept, the docstring
  must stop claiming near-agreement is preserved.

---

## `rxn_insight` silently discards a 0 °C suggestion

- **Severity**: medium
- **Location**: `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/predictors/conditions/rxn_insight.py:55` — `temperature_c=_as_float(sug.get("temperature") or sug.get("temperature_c"))`
- **Trigger**: the backend returns `{"temperature": 0.0}` — an ice bath, which is the single most common non-ambient temperature in process chemistry (acid-chloride acylations, diazotisations, LDA additions at 0 °C). `0.0` is falsy, so `or` falls through to the absent `temperature_c` key and yields `None`.
- **Consequence**: the condition set reaches the agent with `temperature_c=null`, and `predict_reaction_conditions`' docstring instructs the agent to read that as "the model offered none". A recommendation to run at 0 °C is reported as a recommendation with no temperature. Downstream it is worse: the `None` bucket is a different vote key from the `0` bucket, so a 0 °C suggestion from `rxn_insight` cannot reinforce an identical 0 °C suggestion from any other model.
- **Evidence**: `/tmp/audit/repro_temp0.py`

  ```
  model said      0.0  ->  ConditionsPrediction.temperature_c = None
  model said        0  ->  ConditionsPrediction.temperature_c = None
  model said    -78.0  ->  ConditionsPrediction.temperature_c = -78.0
  model said     25.0  ->  ConditionsPrediction.temperature_c = 25.0

  if 0 degC survived : [(0.0, 2, ['parrot', 'rxn_insight'])]
  as shipped (dropped): [(None, 1, ['rxn_insight']), (0.0, 1, ['parrot'])]
  ```

  Two models unanimously recommending an ice bath become two one-vote candidates, one of which
  claims no temperature at all.
- **Fix**: `sug["temperature"] if "temperature" in sug else sug.get("temperature_c")`, or a small
  `_first_present(sug, "temperature", "temperature_c")` helper. `parrot.py:65` and
  `askcos_condition.py:63` already read the temperature key directly and are correct; only
  `rxn_insight` uses the `or` chain for it.

---

## `BasePredictor._loaded` is a check-then-act: concurrent cold turns each load the model

- **Severity**: medium
- **Location**: `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/predictors/base.py:57-59` (`BaseForwardPredictor.predict`) and `:86-88` (`BaseConditionsPredictor.predict`)
- **Trigger**: two chat turns hit a cold server. `if not self._loaded: await asyncio.to_thread(self.load); self._loaded = True` — the flag is only written *after* the await, so a second turn arriving during the first turn's load sees `False` and starts a second `load()`. The prediction cache does not prevent it: different reactants are different keys, and both requests miss on a cold cache anyway.
- **Consequence**: N concurrent first-requests run `load()` N times. For `reaction_t5_v2` / `chemformer` / `t5chem` that means N full T5 checkpoints materialised into memory simultaneously before N−1 are dropped — on a pod with a memory limit that is an OOM kill of the whole server, and even when it survives it multiplies the cold-start latency the manifest's 120 s timeout is sized for. There is a narrower window in which one turn's `predict_sync` can run against attributes another turn's `load()` has just re-assigned (`reaction_t5.load` re-binds `self._tokenizer`, then `self._model`, then `self._device`); I did not reproduce that ordering and do not claim it, but the duplicate load is certain.
- **Evidence**: `/tmp/audit/repro_load_race.py` — two `predict()` calls on one predictor instance, distinct reactants so the cache cannot serve either:

  ```
  load() calls: 2
    result: [ForwardPrediction(product_smiles='CCO', score=1.0, rank=1, source_model='slow')]
    result: [ForwardPrediction(product_smiles='CCO', score=1.0, rank=1, source_model='slow')]
  ```

  `/tmp/audit/repro_load_race2.py` (turn B arriving 0.1 s into turn A's load) prints the same
  `load() calls: 2`.
- **Fix**: give `BasePredictor` an `asyncio.Lock` and do the check inside it — `async with
  self._load_lock: if not self._loaded: await asyncio.to_thread(self.load); self._loaded = True`.
  One lock per predictor instance, so distinct predictors still load in parallel under
  `asyncio.gather`.

---

## `make type` does not type-check `servers/rxnpredict`

- **Severity**: medium
- **Location**: `Makefile:4` — `SRC := packages/mcp_server_kit/src servers/props/src servers/chem/src servers/safety/src servers/calc/src`
- **Trigger**: any type error introduced anywhere under `servers/rxnpredict/src`. `servers/rxnpredict/src` is absent from `SRC`, while the `type` target's own help text reads "mypy --strict over every server and the shared kit" and `pyproject.toml`'s `mypy_path` does list rxnpredict. CI runs `make check`, which is `lint type test`.
- **Consequence**: the largest and most intricate server in the repo — six forward wrappers, five condition wrappers, the aggregator, the cache — is outside the `--strict` gate. Every one of those wrappers is full of `Any`-typed backend adapters where a strict check is worth the most.
- **Evidence**: appended `def _audit_probe(x: int) -> str: return x` to `engine/preprocessing.py`, then:

  ```
  --- make type (SRC as shipped) ---
  Success: no issues found in 59 source files
  --- mypy including rxnpredict ---
  servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/preprocessing.py:70: error:
      Incompatible return value type (got "int", expected "str")  [return-value]
  Found 1 error in 1 file (checked 28 source files)
  ```

  The probe was reverted; `git status --porcelain` is empty. Note the file counts: the gate checks
  59 files, the repo has 87. `uv run mypy servers/rxnpredict/src` on the unmodified tree is clean,
  so this is a gap in coverage rather than a backlog of hidden errors — today.
- **Fix**: add `servers/rxnpredict/src` to `SRC`. Better, derive it: `SRC := packages/*/src
  servers/*/src`, so the next server added cannot be forgotten the same way.

---

## `charge_table` validates `volumes` but not `equivalents`, and will tell a chemist to weigh out a negative mass

- **Severity**: low
- **Location**: `servers/chem/src/chemclaw_mcp_chem/engine/stoichiometry.py:98-101` (`charge_table` argument checks)
- **Trigger**: `stoichiometry_table(basis="THF", basis_mass_g=100, reagents=["DIPEA","DMF"], equivalents=[-1.2, 0.0])`. `basis_mass_g <= 0` is refused and `any(volume <= 0 for volume in volumes)` is refused; `equivalents` is checked only for list length.
- **Consequence**: the table comes back with a row instructing a −215 g charge, presented with the same authority as every other row. `green_metrics` then refuses the resulting mass list, so the agent sees a self-consistent-looking table and a downstream error it has no obvious way to connect to the cause. A zero equivalent produces a row with a real name and 0 mass — a species "charged" in no amount.
- **Evidence**: `/tmp/audit/repro_chem.py`

  ```
    basis    tetrahydrofuran              equiv=   1.0 mmol=  1386.828 mass_g=  100.0000
    reagent  N,N-diisopropylethylamine    equiv=  -1.2 mmol= -1664.193 mass_g= -215.0920
    reagent  N,N-dimethylformamide        equiv=   0.0 mmol=     0.000 mass_g=    0.0000
    unresolved: []
    green_metrics refused: input masses must not be negative
  ```
- **Fix**: add `if any(equiv <= 0 for equiv in equivalents): raise ValueError("every entry of
  equivalents must be positive")` beside the identical `volumes` check three lines above. As an
  `InvalidSmilesError`-family `ValueError` it reaches the model verbatim, which is the point.

---

## The classifier lets one molecule satisfy every reactant pattern of a rule

- **Severity**: low
- **Location**: `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/meta/classifier.py:176-184` (`_rule_matches`) with `_any_mol_matches:187`
- **Trigger**: `classify_reaction(reactants="NCC(=O)O")` — glycine, a single bifunctional reactant. Each of the rule's `reactant_smarts` is tested independently against the *whole set* of reactant molecules, so one molecule carrying both a carboxylic acid and a primary amine satisfies the "carboxylic acid **+** amine" rule on its own. The `_Rule` docstring says only "All `reactant_smarts` patterns must match at least one reactant molecule", which is exactly what it does — the rules are written as if the patterns must match *different* molecules.
- **Consequence**: `classify_reaction` is a served tool, so this is a wrong answer reaching the chemist directly: a lone amino acid is reported as `amide_formation`. Internally it also picks the per-class trust priors used to weight the ensemble. That second half is inert today — `data/trust_priors.json` ships `{}` on purpose — so the blast radius is currently the tool's own answer.
- **Evidence**: `/tmp/audit/repro_smarts.py` (all rule SMARTS compile; none is silently disabled):

  ```
  unparseable SMARTS: none
    glycine alone (one molecule matching BOTH acid and amine patterns) -> amide_formation
    acetic acid + methylamine -> amide                                 -> amide_formation
    acetic acid + methanol -> ester                                    -> esterification
    Suzuki                                                             -> suzuki_coupling
  ```
- **Fix**: require a matching over distinct molecules — for each rule, check that the patterns can
  be assigned to different reactants (for two- and three-pattern rules a greedy assignment over the
  match sets is enough). Alternatively make single-reactant self-match explicit in the rule, for the
  cases where intramolecular chemistry is genuinely intended.

---

## `_normalise_product`'s comment claims a guard that does not exist

- **Severity**: low
- **Location**: `servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/meta/aggregator.py:44-48`
- **Trigger**: a `ForwardPrediction` whose `product_smiles` RDKit cannot parse. The comment reads `return smiles  # leave malformed strings as-is; they'll get bottom rank by score`. Nothing in `aggregate_forward` treats a fallback string differently: it is keyed by its raw text and accumulates `prior * score / rank` exactly like a valid product.
- **Consequence**: a malformed string can be the consensus rank-1 product with `consensus_score=1.0`.
- **Evidence**: `/tmp/audit/repro_malformed.py`

  ```
    rank=1 score=1.000 votes=1 product='PRODUCT:CC(=O)OCC'
    rank=2 score=0.947 votes=1 product='CCOC(C)=O'
  ```

  Reported honestly: all six shipped forward wrappers call `canonical_smiles` before constructing a
  `ForwardPrediction`, so today nothing reaches this branch. The finding is that the comment asserts
  a safety property (`they'll get bottom rank`) that is not implemented, so the first predictor that
  stops canonicalising — or any future caller building `ForwardPrediction` directly — inherits a
  guarantee nobody wrote.
- **Fix**: either implement the claim (drop the candidate, or floor its contribution) or delete the
  half-sentence and say what the code does: unparseable product strings are treated as opaque
  identities and vote normally.

---

## Checked and found sound

Recorded so the next round does not re-spend the time:

- **Per-tool-call caller rebinding** (`mcp_server_kit/app.py:58-85`) works over a real MCP session.
  Driven against a live uvicorn server (`/tmp/audit/repro_caller.py`): handshake as `alice`, then
  a `tools/call` with `X-Chemclaw-Actor: bob` on the same session — the tool body read
  `actor='bob' session='s2'`. The failure the docstring describes is genuinely fixed.
- **Tool-error sanitisation** (`app.py:88-110`) behaves as documented against the installed
  `mcp` (`Tool.run` does `raise ToolError(...) from e`, so `__cause__` is the original):
  `render_structure("CCO junk")` returned `invalid SMILES (empty or contains whitespace): 'CCO junk'`
  verbatim; an injected `KeyError("s3cret-internal-detail")` returned `an internal error occurred`
  with no leakage (`/tmp/audit/repro_errs.py`).
- **`manifests/*/connector.yaml`** are symlinks to `servers/*/connector.yaml`, not copies — no
  drift possible.
- **Prediction cache round-trip** is lossless: `ForwardPrediction`/`ConditionsPrediction` are frozen
  pydantic models, stored via `model_dump()` and rebuilt via `model_validate()`.
- **`egress.arm()`** is idempotent and re-reads the allowlist; `no_egress.network_imports` is a real
  AST walk, not a grep.
- The classifier's ten SMARTS patterns all compile — none is silently disabled by
  `_any_mol_matches`' `MolFromSmarts(...) is None` fallback.
