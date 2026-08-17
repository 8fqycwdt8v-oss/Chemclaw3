# Reachability/consequence verdicts — `science-calc--design.md`

Lens: is the trigger reachable, and is the consequence what is claimed?

In scope: the two findings marked **high**. No finding in the file is marked critical. The five
`medium` and one `low` finding are out of scope and were not judged (the `find_calculations`
refusal one is referenced below only as a calibration point for the severity scale the reporter
used).

---

## Five request models in `models.py` have no reference anywhere in the repository

- **Verdict**: OVERSTATED
- **Severity I would assign**: low

### What I did

Static reachability, with the venv and `docs/` excluded so only real code counts:

```
$ grep -rn "\b\(XtbInput\|PkaInput\|SolubilityInput\|DescriptorInput\|LogdInput\)\b" \
    --include=*.py --include=*.yaml --include=*.yml --include=*.json --include=*.toml --include=*.md \
    src/ tests/ knowledge/ skills/ data/ deploy/ pyproject.toml
src/chemclaw/science/calc/models.py:160:class XtbInput(BaseModel):
src/chemclaw/science/calc/models.py:182:class PkaInput(BaseModel):
src/chemclaw/science/calc/models.py:212:class SolubilityInput(BaseModel):
src/chemclaw/science/calc/models.py:237:class DescriptorInput(BaseModel):
src/chemclaw/science/calc/models.py:266:class LogdInput(BaseModel):
```

Grep only proves textual absence, so I closed the dynamic routes too. Imported **every** module in
the `chemclaw` package (`pkgutil.walk_packages`, 0 import failures) and then scanned every loaded
module's `__dict__` for the class objects themselves, every loaded pydantic model's
`model_fields` annotations, and `__subclasses__()`:

```
XtbInput referenced by other modules: []          (only my own loop variable)
PkaInput referenced by other modules: []
SolubilityInput referenced by other modules: []
DescriptorInput referenced by other modules: []
LogdInput referenced by other modules: []
field-annotation hits: [ ... bofire's ContinuousDescriptorInput / CategoricalDescriptorInput only ... ]
subclasses: {all five: []}
```

The one manifest-driven route that could name a model by string is `params_model`, resolved by
`connectors/jobs.py:89 resolve_params_model`. Every value an operator could reach:

```
$ grep -rn "params_model" src/chemclaw/connectors/*/connector.yaml | awk -F'params_model: ' '{print $2}' | sort -u
chemclaw.connectors.calc.specs:{Complex,Ensemble,Reaction,Scan,SolventScreen}JobSpec
chemclaw.connectors.qm.specs:QmJobSpec
chemclaw.science.bo.problem:CampaignSpec
```

`models.py` has no `__all__`, and `science/calc/__init__.py` is 27 lines of docstring with no
imports, so there is no re-export either.

Deletion experiment, in a sandbox copy of `HEAD` (`git archive HEAD` into a scratch dir, run with
`PYTHONPATH` ahead of the editable `.pth` — the shared checkout was never mutated):

```
baseline (full suite, -x):  1 failed, 2703 passed in 660s
                            (the one failure is tests/test_migrations_are_additive.py, which reads
                             git history the `git archive` copy does not have — a sandbox artifact,
                             and -x stopped the run there)
mutant (all five classes deleted + structural_domain/_ORGANIC_ELEMENTS renamed away;
        test_migrations_are_additive deselected, tests/test_uncertainty.py ignored):
                            1 failed, 3934 passed, 1 skipped, 123 deselected in 1067s
                            the single failure is tests/test_reizman.py::test_bo_campaign_finds_
                            high_yield, a wall-clock TIMEOUT the harness itself prints as "not
                            evidence about the code under test" — nothing to do with calc models
```

Cross-repo check: all five models exist and are *live* in `Chemclaw3-mcp`
(`servers/calc/src/chemclaw_mcp_calc/engine/{xtb,pka,solubility,descriptors,logd}.py`), with
identical fields and constraints.

### Why

The mechanism is exactly right and I could not find a single reachable path to any of the five —
not a caller, not a re-export, not a discriminated union, not a manifest string, not a subclass.
Removing them changes nothing the suite can see. So the *deadness* is CONFIRMED and the fix is
safe.

What does not hold is the severity. There is no trigger at all: nothing a real caller can do —
HTTP request, tool call, manifest, CLI — reaches this code, because it is not code that runs. The
consequence is entirely a reading cost, and even that is smaller than the finding says. The
finding's harm claim is *"a reader adding a tool will reasonably assume `PkaInput` is the contract
and wire against it"*. I checked what that reader would get: the five classes here are field-for-
field identical to the five the calculation server actually validates against (`XtbInput`:
`smiles: str = Field(min_length=1)`, `charge: int = 0` in both trees; `DescriptorInput`'s bare
`smiles: str` matches too, min-length gap included). A reader who wired against them would produce
a duplicate, not a wrong contract — the trap is a maintenance cost, not a defect that can emit a
wrong number.

For calibration inside the reporter's own file: the `find_calculations` refusal finding (an agent
told to retry with `calc_type="descriptors"`, which can never match a row, and then reporting "we
have never computed this" to a chemist as fact) is ranked **medium**. That is a wrong chemistry-
adjacent answer reaching a human. Dead pydantic classes that nothing imports cannot rank above it.
Low is the honest slot: real, worth deleting on sight per the repo's own dead-code rule, and
carrying no runtime risk either way.

---

## `uncertainty.structural_domain` is dead code kept alive by its own test

- **Verdict**: OVERSTATED
- **Severity I would assign**: low

### What I did

Same two-layer reachability check. Static:

```
$ grep -rn "structural_domain\|_ORGANIC_ELEMENTS" --include=*.py --include=*.md --include=*.yaml \
    src/ tests/ skills/ knowledge/ data/
src/chemclaw/science/calc/uncertainty.py:61:_ORGANIC_ELEMENTS = frozenset({...})
src/chemclaw/science/calc/uncertainty.py:161:def structural_domain(mol: Chem.Mol) -> ...
src/chemclaw/science/calc/uncertainty.py:190:    foreign = ... - _ORGANIC_ELEMENTS
tests/test_uncertainty.py:26,47,59,70,77
```

Dynamic (all `chemclaw` modules imported, every module `__dict__` scanned for the function object):
`structural_domain referenced by other modules: []`.

Deletion experiment: the same sandbox run above renamed `structural_domain` and `_ORGANIC_ELEMENTS`
out of existence and ignored `tests/test_uncertainty.py`. **3934 passed**, one unrelated wall-clock
timeout. Nothing outside that one test file depends on it.

Then I settled the hedge the finding left open — *"If the mcp server does not have its own copy,
that is a correctness gap in the mcp repo"*. It has one, and it runs:

```
$ grep -rn "structural_domain" /workspace/chemclaw3-mcp/servers/calc/src/chemclaw_mcp_calc/
engine/uncertainty.py:  __all__ = [..., "structural_domain"]   (byte-identical three checks)
engine/solubility.py:   from ...uncertainty import Estimate, structural_domain
engine/solubility.py:   in_domain, reasons = structural_domain(mol)
engine/solubility.py:   estimate=Estimate(..., in_domain=in_domain, domain_reasons=reasons)
```

And I traced what a chemist is actually shown, because this is a trust-of-a-number answer and
"the caller might catch it" would not be good enough. `connectors/calc/server/tools.py:660
predict_solubility` does `cached_remote(...)` → `SolubilityResult.model_validate(payload)` →
returns the whole model, `estimate.in_domain` and `estimate.domain_reasons` included. So a salt or
an organometallic handed to `predict_solubility` in production comes back with `in_domain=False`
and the reasons, rendered by `Estimate.render` as `... OUT OF DOMAIN — multi-component structure
(salt, co-crystal or mixture); ...`. The check the local dead copy implements is a check the
running system genuinely performs.

### Why

Deadness: CONFIRMED, and the fix is safe — proven by execution, not by reading.

Severity: not high. Two specific parts of the finding do not hold.

1. The consequence is stated as a *control* claim: *"This is precisely the shape `CLAUDE.md` names
   as a failure (`reject_widening`: 'a guard with no caller … is a claim that a control exists')"*.
   The analogy inflates it. `reject_widening`'s absence meant the control did **not** exist
   anywhere. Here the control exists, runs on every `predict_solubility` call, and its verdict
   reaches the agent and the chemist — one repository over, which is where the equation it guards
   also lives. What is dead is a *duplicate* of a live control, not a stand-in for a missing one.
   No molecule, no query and no manifest can produce a wrong or over-confident answer from this
   code, because it never executes.
2. There is no reachable trigger at all, so nothing here is worse than the file's own `medium`
   findings — one of which (`find_calculations` advising a `calc_type` no row can carry) ends with
   a model telling a chemist "we have never computed this" as a fact. Dead code ranked above that
   is a scale inversion.

Two things the reporter did not say, which are worth carrying into the fix:

- This repository has already done exactly this deletion once in this module, so the precedent is
  established rather than novel: `Estimate.method`'s `"conformal"` member and the split-conformal
  function behind it were removed for having no caller (`uncertainty.py`'s own module docstring;
  `tests/test_calc_payload_schemas.py:116-119` records the digest change). `structural_domain` is
  the same argument one function later.
- Deleting it also removes `from rdkit import Chem` (`uncertainty.py:49`) — it is the module's only
  rdkit user. That does not lighten the `connectors/calc/results.py` leaf path the reporter praises
  elsewhere, since `models.py` imports rdkit itself, so it is tidiness rather than a second win.
- A genuine adjacent gap the finding did not name, and which argues for a *different* ticket rather
  than for keeping this function: `tests/calc_server_fake.py:302` returns
  `"estimate": {"value": -2.13, "unit": "log S", "uncertainty": 0.75}` — no `in_domain`, no
  `domain_reasons`, no `method`. The fake therefore never exercises a populated domain verdict, so
  if the real server ever stopped setting one, every test here would stay green while
  `Estimate.render` silently downgraded to "applicability not assessed" in production. That is the
  cross-repo check the deleted local copy was never going to provide either.
