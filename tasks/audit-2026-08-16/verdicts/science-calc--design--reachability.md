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
                             git history the sandbox copy does not have — a sandbox artifact)
mutant (all five classes deleted + structural_domain renamed away):
                            2705 passed, 3 skipped in 1104s
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
