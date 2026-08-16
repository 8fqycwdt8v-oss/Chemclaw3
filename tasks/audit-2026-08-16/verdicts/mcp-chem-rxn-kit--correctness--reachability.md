# Verdicts — `mcp-chem-rxn-kit--correctness`, reachability lens

Scope: the two **high** findings. The file contains no critical findings; medium and low ignored.

Lens: grant the mechanism, attack reachability and consequence.

**The one fact that governs both verdicts** (established before either section, so it is not
repeated as new evidence twice):

```
$ cd /workspace/chemclaw3-mcp && uv run python -c "... tools.list_available_models() ..."
forward reaction_t5_v2 no        conditions rxn_insight   no
forward t5chem         no        conditions parrot        no
forward molecular_transformer no conditions reagents_mt   no
forward megan          no        conditions two_stage_dnn no
forward graphrxn       no        conditions askcos_condition no
forward chemformer     no
```

Zero predictors — forward or conditions — are installed. Nothing in *either* repo installs them:
`Chemclaw3-mcp` has no `Dockerfile`/`Containerfile` at all (`find . -maxdepth 2 -iname 'Dockerfile*' -o -iname '*ontainerfile*'` → empty),
and CI runs `uv sync --frozen` with no extras (`.github/workflows/ci.yml:43`). The extras exist in
`servers/rxnpredict/pyproject.toml` but no build selects them.

On the caller side (`/home/user/Chemclaw3`):

- `rxnpredict` is **not in the shipped chart's connector set**:
  `python3 -c "import yaml; print(list(yaml.safe_load(open('deploy/helm/chemclaw/values.yaml'))['connectors']))"`
  → `['molfp', 'rxnfp', 'safety', 'chem', 'calc', 'bo', 'qm']`.
- The default `connectors_dir` is the shipped `src/chemclaw/connectors/`
  (`src/chemclaw/core/config/connectors.py:32`), which has no `rxnpredict` bundle.
- The **only** place the backend dials it is the live dev harness, which puts
  `$MCP_REPO/manifests` on the path (`infra/live/e2e-full-stack/up.sh:185`) and then pins the
  ensemble to deterministic doubles (`up.sh:91-92`):
  `CHEMCLAW_RXNPREDICT_ENABLED_FORWARD_MODELS=fake_a`, `..._CONDITIONS_MODELS=fake_c`.
  `FakeForwardPredictor.predict_sync` has **no `continue`** (`base_doubles.py:38-44`, ranks are
  contiguous by construction) and `FakeConditionsPredictor` emits **one** set at rank 1
  (`base_doubles.py:79-87`) — one model cannot split a vote with itself.

So both tools are genuinely *served* and *called* by the backend, but the defective code path in
each is unreachable in every configuration that exists in either repository today. No backend-side
pydantic model or validator is relevant here — neither trigger is caller input; both are internal
model/backend behaviour. That is what moves both findings, not any flaw in the mechanism.

---

## A skipped beam gives a model gapped ranks, and the aggregator divides by the rank — flipping the consensus top-1

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

### What I did

The cited code is exactly as described. All six forward predictors take the rank from the raw beam
index across a `continue`:

```
$ for f in reaction_t5 chemformer t5chem molecular_transformer megan graphrxn; do
    grep -n "rank=\|continue\|enumerate" .../forward/$f.py; done
reaction_t5:  82: for i, seq in enumerate(sequences)   86/90: continue   102: rank=i + 1
chemformer:   74: for i, seq in enumerate(...)              79: continue    89: rank=i + 1
t5chem:       77: ...                                       82: continue    92: rank=i + 1
molecular_transformer: 105: ...                            110: continue   117: rank=i + 1
megan:        68: ...                                    72/76: continue    82: rank=i + 1
graphrxn:     55: ...                                    61/65: continue    70: rank=i + 1
```

`aggregator.py:84` is `contribution = prior * p.score / p.rank`, and nothing between the predictor
and the aggregator renumbers (`tools.py:194` passes `per_model` straight in).

I drove the **real** aggregator with the **real** default priors from
`engine/config.py:80-96` (`reaction_t5_v2: 1.00`, `chemformer: 0.85`) — `/tmp/verify/f1.py`:

```
priors: {'reaction_t5_v2': 1.0, 'chemformer': 0.85, 'megan': 0.8}
beam0 parsed  (rank 1):
   rank=1 score=1.000 votes=1 CCOC(C)=O  <- ['reaction_t5_v2']
   rank=2 score=0.756 votes=1 CCO        <- ['chemformer']
beam0 skipped (rank 2):
   rank=1 score=1.000 votes=1 CCO        <- ['chemformer']
   rank=2 score=0.662 votes=1 CCOC(C)=O  <- ['reaction_t5_v2']
```

The mechanism reproduces against production code and production priors, not a hand-built fixture.

### Why

**The mechanism is confirmed and the fix is correct.** Two things do not hold at *high*.

1. **The trigger is narrower than "routine".** The finding says "any forward call where a model's
   beam 0 (or any earlier beam) decodes to a string RDKit refuses". Only a failure at **beam 0**
   can move the consensus top-1: if beam 3 fails, beams 0–2 still carry ranks 1–3 and the top-1 is
   untouched — all that happens is a reweighting among lower candidates. "At least one malformed
   SMILES somewhere in a top-5 is routine" is plausible; "the single highest-confidence beam of a
   model with ~97.5% top-1 exact-match accuracy is undecodable" is the rarest beam to fail, and the
   finding offers no rate for it. The common case of this bug is a perturbation of ranks 2–5, not a
   flipped answer.

2. **No configuration that exists can produce it.** See the shared section above: no forward
   predictor is installed anywhere, `rxnpredict` is absent from the shipped chart, and the one
   deployment that dials it runs `fake_a`, whose `predict_sync` has no skip path at all. The
   flipped-top-1 consequence requires a deployment with the real T5 extras installed, which is a
   supported future state (`pyproject.toml` extras exist; `connector.yaml`'s 120 s timeout is sized
   for a checkpoint load) but is not a current one.

Worth adding, in the finding's favour, two things the reporter did not say:

- In the `raw_scores is None` fallback (`reaction_t5.py:94`, and the same shape in `megan`,
  `graphrxn`, `askcos_condition`) the **score** is also indexed by the raw `i`
  (`score = max(0.01, 1.0 - 0.1 * i)`), so a skipped beam penalises the survivor **twice** —
  once through `score` and once through `1/rank`. The 0.5× the finding quotes is the best case.
- The gapped rank is not only an internal weight: `per_model` is returned to the agent
  (`tools.py:195`), so the model is shown its strongest predictor's best product labelled "rank 2"
  with nothing at rank 1. That is a second, plainer wrongness.

Neither changes the reachability arithmetic. Medium: a real one-line defect, in six places, on a
path no shipped configuration executes.

---

## Condition sets that every model agrees on are counted as separate one-vote candidates

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

### What I did

`_canon_set` (`aggregator.py:111-122`) is as quoted — `canonical_smiles(x)` with the raw string
appended on `ValueError`. The three quoted docstring claims are accurate: `tools.py:210-213`
("the voting unit is the whole condition set … so near-agreement between models counts as
agreement") and `tools.py:11-13` ("`contributing_models` come back … precisely so the agent can say
'four of five models agree'").

I drove the **real** `aggregate_conditions` with real default priors — `/tmp/verify/f2.py`:

```
A) 3/3 models agree on THF@25, each spelling it its own way:
   rank=1 sc=1.000 votes=1 solv=['tetrahydrofuran'] T=25.0 <- ['parrot']
   rank=2 sc=0.895 votes=1 solv=['C1CCOC1']         T=25.0 <- ['askcos_condition']
   rank=3 sc=0.737 votes=1 solv=['THF']             T=25.0 <- ['rxn_insight']
```

The finding stops there. It is worse than that, and I think this is the part that matters — with a
majority present, the split does not merely under-report agreement, it **changes the recommendation**:

```
B)  3x THF (three spellings) vs 1x DMF from parrot:
   rank=1 sc=1.000 votes=1 solv=['CN(C)C=O']        <- ['parrot']       <-- DMF wins
   rank=2 sc=0.947 votes=1 solv=['tetrahydrofuran'] <- ['two_stage_dnn']
   rank=3 sc=0.895 votes=1 solv=['C1CCOC1']         <- ['askcos_condition']
   rank=4 sc=0.737 votes=1 solv=['THF']             <- ['rxn_insight']

B') identical inputs, THF spelled identically by all three:
   rank=1 sc=1.000 votes=3 solv=['C1CCOC1'] <- ['askcos_condition','rxn_insight','two_stage_dnn']
   rank=2 sc=0.388 votes=1 solv=['CN(C)C=O'] <- ['parrot']
```

A chemist would be shown "DMF, 1 model" as the top recommendation where three of four models said
THF, with no signal in the response that this happened — `vote_count=1` on every row looks like
four models that simply disagreed, which is a legible state, not a broken one.

I also found a split path the reporter **missed**, and it removes the finding's dependence on the
one thing it could not prove (that a backend emits chemical names). `askcos_condition._as_list`
splits on `.` and no other wrapper does — `/tmp/verify/f2b.py`, with pure SMILES and no name
anywhere:

```
raw = "C1CCOC1.O"          # THF/water
askcos  _as_list: ['C1CCOC1','O']  -> key ('C1CCOC1','O')
parrot  _as_list: ['C1CCOC1.O']    -> key ('C1CCOC1.O',)
rxnins  _as_list: ['C1CCOC1.O']    -> key ('C1CCOC1.O',)
same key? False
```

So co-installing ASKCOS with any other conditions predictor splits the vote on **every**
multi-component solvent or reagent field — routine for a coupling's base/ligand pair — with no
name-resolution question involved at all.

### Why

**The mechanism is confirmed, reproduced against production code, and is worse than reported.**
What does not hold at *high* is reachability, on two counts:

1. **The finding's own trigger is unproven.** Its honest caveat — no condition backend installed —
   is load-bearing. The claim needs at least one real backend to emit a name where another emits a
   structure. `_canon_set`'s raw-string fallback is *evidence the author expected* that, but an
   expectation in a fallback branch is a claim, not a measurement, and the finding measures nothing
   about backend output. (My dot-split path above is a strictly better argument than the one filed,
   and it is provable from shipped code — but it needs `askcos` plus one other, which brings us to 2.)

2. **Two condition predictors have to be running, and zero are.** See the shared section: none of
   the five is installed, `rxnpredict` is not in the shipped chart's connector set, and the only
   deployment that dials it pins `CHEMCLAW_RXNPREDICT_ENABLED_CONDITIONS_MODELS=fake_c` — a single
   predictor emitting a single set at rank 1. A vote cannot be split across one voter.

On the "what would a chemist be shown" test the lens asks for: this is a recommendation tool, not a
safety or impurity-limit answer, and the finding does not claim otherwise. The backend does not
post-process the response — it is an MCP tool result rendered into the model's context — so nothing
downstream catches it, and the docstring actively instructs the agent to quote `vote_count`. If two
real condition predictors were ever installed I would raise this to high without hesitation, and I
would raise it above finding 1, because it changes the answer rather than reweighting it. Today it
is medium: a confirmed defect on a surface that ships with no voters.

**Note for whoever fixes it:** fix the `askcos_condition` dot-split divergence at the same time.
It is one line, it needs no synonym table, and it is the half of this finding that is provable from
shipped code alone.
