# Verification — `mcp-chem-rxn-kit--correctness`, lens: **does it actually reproduce?**

Scope: the two **high** findings. No critical findings were filed. Medium/low ignored.

Repo under review: `/workspace/chemclaw3-mcp` @ `9217011`, working tree clean.
Baseline re-measured myself: `uv run pytest -q servers/chem servers/rxnpredict packages/mcp_server_kit`
→ **371 passed, 1 warning in 2.28s**. Matches the reporter's stated baseline.

I did not run any of the reporter's `/tmp/audit/` scripts. All scripts are my own, under `/tmp/myaudit/`.

---

## A skipped beam gives a model gapped ranks, and the aggregator divides by the rank — flipping the consensus top-1

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

### What I did

**1. Cited symbols and line numbers are real and current.** Not taken from the finding:

```
$ sed -n '102p' servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/predictors/forward/reaction_t5.py
                    rank=i + 1,
$ sed -n '84p' servers/rxnpredict/src/chemclaw_mcp_rxnpredict/engine/meta/aggregator.py
            contribution = prior * p.score / p.rank
```

`grep -n "rank=\|continue\|enumerate"` over all six forward wrappers confirms the identical
`for i, ... enumerate(...)` / `continue` / `rank=i + 1` shape at chemformer.py:89, t5chem.py:92,
molecular_transformer.py:117, megan.py:82, graphrxn.py:70 — every line number in the finding is exact.

**2. I did not accept the reporter's hand-built prediction list.** I drove the **real**
`ReactionT5V2Forward.predict_sync` with a fake tokenizer/model (`/tmp/myaudit/r1_real.py`), so the
ranks come out of the shipped loop:

```
=== A: all beams parse ===
   beams decoded : ['CCOC(C)=O', 'CCO', 'CC(=O)O']
   emitted ranks : [1, 2, 3]
=== B: beam 0 is a malformed decode (RDKit refuses) ===
   beams decoded : ['!!!not-a-smiles!!!', 'CCOC(C)=O', 'CCO']
   emitted ranks : [2, 3]
   products      : ['CCOC(C)=O', 'CCO']
=== C: beam 0 decodes empty ===
   emitted ranks : [2, 3]
=== D: beams 0 and 1 both malformed ===
   emitted ranks : [3]
```

Gapped ranks reproduce from the shipped code, on both skip branches (`if not cleaned: continue` and
the `except ValueError: continue`), and the gap compounds — two skips push the survivor to rank 3.

**3. The flip, isolated to the rank alone.** My first end-to-end attempt conflated two effects
(the malformed beam also removes a candidate). So I re-ran with the predictions the real loop had
just emitted, changing *only* the rank field, through the real `aggregate_forward` with real shipped
`Settings` (real trust priors: `reaction_t5_v2=1.00`, `t5chem=0.92`) — `/tmp/myaudit/r1_isolate.py`:

```
SHIPPED (rank = raw beam index)
   rank=1 score=1.000 votes=2 product='CCO'
   rank=2 score=0.683 votes=1 product='CCOC(C)=O'
   >>> TOP-1 = 'CCO'

FIXED   (rank = emitted position)
   rank=1 score=1.000 votes=1 product='CCOC(C)=O'
   rank=2 score=0.753 votes=2 product='CCO'
   >>> TOP-1 = 'CCOC(C)=O'
```

Same products, same scores, same priors. The consensus top-1 changes because of a decode artefact in
a beam that contributed nothing. The flip window is wide, not a contrived corner: with
`reaction_t5_v2` at prior 1.00 against `t5chem` at 0.92, any `score_A` in
`(0.92·score_B, 1.84·score_B)` flips.

### Why

It reproduces from the shipped loop, the arithmetic is exactly as described, the line numbers are
current, and `ForwardPrediction.rank`'s own field description ("1-based rank within this model's
top-K") is violated with only `ge=1` enforced. Confirmed.

**Two things the reporter missed, one making it worse and one bounding it — both worth carrying:**

*Worse:* the gapped rank is not confined to the aggregator's internals. `tools.py` returns
`per_model` **verbatim** in `ForwardResponse`, and `predict_forward_single_model` returns the raw
`list[ForwardPrediction]` ("That model's ranked predictions, each with its own score and rank").
So a model whose best surviving prediction is labelled `rank=2` with no rank 1 present is shown to
the agent directly, in *any* deployment, single-model included.

*Bounding:* the consensus **flip** needs two forward predictors that actually load. I checked what
the shipped image has. `servers/rxnpredict/Containerfile` installs
`"chemclaw-mcp-rxnpredict[reaction_t5,rxn_insight]"`, which drags in `torch` and `transformers`;
the import guards are bare (`chemformer.py` guards on `import transformers`, `graphrxn.py` on
`import torch`), so **chemformer and graphrxn register** in the default image — but their `load()`
raises `FileNotFoundError` without `$CHEMFORMER_MODEL_PATH` / a checkpoint dir, and `tools.py`
gathers with `return_exceptions=True` and drops them. So the default image has one *loadable*
forward model, and I verified that with one model the ordering is preserved (score/rank stays
monotone; only `consensus_score` magnitudes shift: `CCO` 0.083 → 0.062). The flip therefore lands on
any deployment where an operator supplies a second checkpoint — which is the ensemble this server
exists to be. That is a reachability note, not a discount: the defect is unconditional, and the fix
is the one-line change the finding gives.

---

## Condition sets that every model agrees on are counted as separate one-vote candidates

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

### What I did

**1. The aggregator half is real, and I reproduced it** (`/tmp/myaudit/r2.py`, real
`aggregate_conditions`, real `Settings`). `_canon_set` at aggregator.py:111-122 does fall back to the
raw string, and divergent spellings do split:

```
   'THF'             -> ValueError (raw string kept)
   'tetrahydrofuran' -> ValueError (raw string kept)
   'C1CCOC1'         -> canonical 'C1CCOC1'

three condition models, ALL saying 'THF at 25 C' in different spellings:
   rank=1 votes=1 solvents=['tetrahydrofuran'] models=['parrot']
   rank=2 votes=1 solvents=['C1CCOC1']         models=['askcos_condition']
   rank=3 votes=1 solvents=['THF']             models=['rxn_insight']
```

**2. But the controls the reporter did not run show the guard that does exist.** Equivalent SMILES
merge correctly, which matters because it is the realistic multi-backend case:

```
all three emit SMILES (different but equivalent, C1CCOC1 / O1CCCC1 / C1OCCC1):
   rank=1 score=1.000 votes=3 solvents=['C1CCOC1'] models=['askcos_condition','parrot','rxn_insight']
all three emit the SAME name:
   rank=1 score=1.000 votes=3 solvents=['THF']     models=['askcos_condition','parrot','rxn_insight']
```

**3. I settled the backend premise against the primary source instead of assuming it.** The finding
asserts "`rxn_insight._as_list` and `parrot._as_list` pass whatever the backend emits straight
through (**names included**)". The reporter admits no backend was installed. I installed the real
one — `uv pip install rxn-insight` → **rxn-insight 0.1.3** — and read it:

```
$ python -c "from rxn_insight.utils import get_solvent_ranking; import inspect; print(inspect.getsource(...))"
    solvents = df["SOLVENT"].tolist()      # NAME column is just the SOLVENT field, verbatim
$ sed -n '158p;312p' .../rxn_insight/ord.py
            - SOLVENT: Concatenated solvent SMILES
        'SOLVENT': ".".join(list(set(solvents))),
```

Rxn-INSIGHT's `SOLVENT`/`CATALYST`/`REAGENT` are **dot-concatenated SMILES**, not names. The
finding's central illustration — `rxn_insight` → `'THF'` while `askcos_condition` → `'C1CCOC1'` —
is contradicted by the library it names.

**4. The wrapper cannot drive that backend at all** (`/tmp/myaudit/r2b.py`, real library, shipped
`_invoke_rxn_insight`):

```
real Reaction.suggest_conditions signature: (self, df: 'pd.DataFrame') -> 'dict[str, pd.DataFrame]'
module-level suggest_conditions?  False
--- shipped wrapper's adapter against the real library ---
RAISED TypeError: Reaction.suggest_conditions() got an unexpected keyword argument 'top_n'
```

The real method takes a required `df` DataFrame and returns a `dict` with **capitalised** keys
(`"Solvent"`, `"Catalyst"`, `"Reagent"`); the wrapper calls it as `suggest_conditions(top_n=top_k)`
and then iterates the result as a list of dicts with lowercase keys.

**5. Reachability in the shipped image.** Same Containerfile analysis as above: of five condition
wrappers, `askcos_condition` needs a manual MIT install, `reagents_mt` needs `onmt`; `parrot` and
`two_stage_dnn` register on a bare `import torch` but `load()` raises `FileNotFoundError` without a
checkpoint dir. So one condition predictor is nominally live — and with one model every candidate is
`vote_count=1` by construction, so there is no unanimity to under-report:

```
A) With ONE condition model, what is vote_count?
   rank=1 votes=1 solvents=['C1CCOC1']
   rank=2 votes=1 solvents=['CCO']
```

### Why

The mechanism in the aggregator is real and unguarded, and I reproduced the split — so this is not
REFUTED. But the finding's stated consequence, "five-of-five agreement reads as no agreement at all,
and the agent is being told to quote that number", does not hold as filed:

- it needs ≥2 condition backends with checkpoints, which no shipped configuration has;
- its worked example rests on a backend behaviour the primary source contradicts (rxn-insight emits
  SMILES, and the aggregator **correctly merges** equivalent SMILES — I measured `votes=3`);
- the one backend that does ship cannot be invoked by its own wrapper at all, so it produces no
  condition set to split.

"High" would have this changing the consensus a chemist reads today. It does not. Medium is right.

**What survives, and is the version worth fixing** — a real spelling divergence between two shipped
wrappers that needs no names at all. `rxn_insight._as_list` returns a string whole; `askcos_condition._as_list`
splits it on `.` (askcos_condition.py:76). Since rxn-insight really does emit dot-concatenated
SMILES, the same two solvents produce two different keys (`/tmp/myaudit/r2c.py`):

```
   _canon_set(['CCO.C1CCOC1'])   = ('C1CCOC1.CCO',)
   _canon_set(['CCO','C1CCOC1']) = ('C1CCOC1', 'CCO')
   rank=1 votes=1 solvents=['C1CCOC1', 'CCO']  models=['askcos_condition']
   rank=2 votes=1 solvents=['C1CCOC1.CCO']     models=['rxn_insight']
```

The one-line fix is to split on `.` in `_canon_set` itself, which is strictly better than the
synonym table the finding proposes and is grounded in what the backends actually emit.

**Out of scope but found while verifying, and worse than either finding:** the shipped
`rxn_insight` wrapper is incompatible with rxn-insight 0.1.3 (item 4 above). It is the only
condition predictor the image installs, so `predict_reaction_conditions` plausibly returns zero
successful models in the default deployment. Nothing in the 371-test suite covers it. Worth its own
round-2 finding.
