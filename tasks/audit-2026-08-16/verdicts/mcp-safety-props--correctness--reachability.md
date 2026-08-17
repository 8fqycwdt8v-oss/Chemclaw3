# Verdicts — `servers/safety/`, `servers/props/` CORRECTNESS, lens: reachability + consequence

Repo under review: `/workspace/chemclaw3-mcp` @ `9217011`, working tree clean (`git status --short`
empty — no mutation markers, no diff against the pristine copy needed).
Baseline re-run: `uv run pytest -q servers/safety/tests servers/props/tests` → **286 passed**.

In scope: the one **critical** and the two **high** findings. The four medium/low findings are
ignored per the brief.

---

## A multi-fragment SMILES silently defeats every pair rule and over-fires every counted rule

- **Verdict**: CONFIRMED
- **Severity I would assign**: critical

### What I did

```
$ uv run --project servers/safety python /tmp/f1.py
NaH+DCM 2 entries              -> ['saline-hydride-with-chlorinated-solvent']
NaH.DCM 1 entry                -> []
   PAYLOAD: {"flags": [], "screened": ["ClCCl.[H-].[Na+]"],
             "verdict": "No rule in the hazard table matched. This is not a safety assessment."}
peroxide+acetone 2             -> ['peroxide', 'peroxide-with-ketone']
peroxide.acetone 1             -> ['peroxide']
NaN3+DCM 2                     -> ['azide-with-dichloromethane', 'non-carbon-azide']
NaN3.DCM 1                     -> ['non-carbon-azide']
nitrobenzene                   -> []
2x nitrobenzene 1 entry        -> ['polynitro-aromatic']
```

Every line the reporter published reproduces exactly, including the over-fire direction.

I then tried to find the boundary of the defect, because the finding does not state one:

```
$ uv run --project servers/safety python /tmp/f1b.py
2 entries, first multi-species: [] ['ClCCl.[H-].[Na+]', 'CCOC(C)=O']
genotox 1 entry:  []          # CN(C)C.[Na+].[O-]N=O
genotox 2 entries: nitrosatable-amine-with-nitrosating-agent
$ uv run --project servers/safety python /tmp/f1c.py
['[H-].[Na+].ClCCl', 'ClCCl']      -> ['saline-hydride-with-chlorinated-solvent']
['[H-].[Na+].ClCCl', '[H-].[Na+]'] -> ['saline-hydride-with-chlorinated-solvent']
```

So the rule fires again only when a *second* entry redundantly repeats one of the two arms. The
defect is not confined to a one-element list: `screen_reaction(["[H-].[Na+].ClCCl", <anything
else>])` is equally blind, which is broader than the finding's `len(smiles) == 1` framing suggests.

### Why

**Reachability — nothing upstream can stop this, and the server cannot refuse the input class.**
I traced the input back to the outermost entry point:

- `servers/safety/src/chemclaw_mcp_safety/tools.py:50` — the tool signature is bare
  `smiles: list[str]`. No `Field` constraint, no validator, no per-element pattern.
- `engine/chem.py` `require_molecule` (mirrored at `/home/user/Chemclaw3/src/chemclaw/core/chem.py:232`)
  rejects exactly three things: embedded whitespace, the empty string, and edge non-ASCII. A dot is
  none of them, and its own docstring enumerates the three.
- Backend side, `src/chemclaw/connectors/server.py:307-333` wraps `manager.call_tool` for identity
  and audit only. There is no argument schema, no SMILES normalisation, no fragment split anywhere
  between the model's tool call and the server. `grep -n "args\|schema" src/chemclaw/connectors/transport.py`
  returns nothing.
- `connectors/safety/connector.yaml` declares transport, auth and `read_only` — no argument policy.

So the trigger is produced by whatever string the LLM emits. The only thing standing in the way is
prose: the tool docstring's *"no two structures run together"* and the `safety-screening` skill.
Both are claims, not gates — and the docstring's own worked example of that rule is the **space**
case (`"CCO 1-azidopropane"`), not the dot case, so a model reading it has no reason to think
`A.B` is forbidden. Decisively, the server *must* accept dotted strings: `[H-].[Na+]`,
`[N-]=[N+]=[N-].[Na+]` and the hydrazinium salts are reference molecules of the rule table itself,
and the pair rule for NaH/DCM fires correctly only when the salt is its own entry. The server
therefore cannot distinguish "one salt" from "two species" and does not try.

**Consequence — as stated, and the payload's own integrity check passes.** This is what makes me
keep the reporter's severity rather than trim it. `ScreenResult.screened` exists (per its docstring
at `screen.py:112-120`) so "a reader confirms the screen is about the molecules they meant". For
`["[H-].[Na+].ClCCl"]` it returns `["ClCCl.[H-].[Na+]"]` — correct, complete, canonicalised. The
chemist is therefore shown, via the skill's mandated phrasing, *"No rule in the hazard table
matched. This is not a safety assessment."* about sodium hydride in dichloromethane, with the
payload actively confirming that both species were looked at. The standing disclaimer is the
mitigation the package is built on, and here it is doing the opposite of its job: it is attached to
a result that is not merely uninformative but *wrong about a rule the table carries*. "The caller
might catch it" is not available — there is nothing in the payload to catch.

The counted-rule direction is real too but narrower: `min_matches: 2` appears on exactly one rule
(`polynitro-aromatic`, `rules.yaml:109`), so the false-positive surface is one rule needing two
nitroarene fragments in one string. I would not have called that half critical on its own; the
false negative carries the severity.

**What the reporter missed, which makes it slightly worse**: the genotoxicity screen's blind spot
is not symmetric with the hazard one. `screen_hazards` at least still evaluates structural rules
across the merged molecule, so `NaN3.DCM` keeps `non-carbon-azide`. The nitrosamine formation alert
has *no* structural counterpart — `CN(C)C.[Na+].[O-]N=O` produces a completely empty
`AlertResult`, and the nitrosamine question is precisely the one the skill says to pass "the whole
route" for, i.e. the call shape most likely to concatenate.

---

## `ich_impurity_limit("CO")` returns cobalt's PDE — `CO` is methanol, and the fleet's own props server emits exactly that string

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

### What I did

```
$ uv run --project servers/safety python /tmp/f2.py
'CO'         -> Cobalt (Co)      | Class 2A | [('oral PDE', 50.0, 'µg/day'), ...]
'Co'         -> Cobalt (Co)      | Class 2A | ...
'methanol'   -> Methanol         | Class 2  | [('PDE', 30.0, 'mg/day'), ('concentration limit', 3000.0, 'ppm')]
'OC'         -> Methanol         | Class 2  | ...
'C1CCOC1'    -> Tetrahydrofuran  | Class 2  | ...

$ uv run --project servers/props python -c "...records.find('methanol')..."
'CO' methanol
```

The mechanism reproduces exactly as described. I re-ran the collision scan independently rather
than trusting the reporter's, folding every reagent-table SMILES through `_fold` against the built
ICH index and comparing to what the row's *name* resolves to:

```
$ uv run --project servers/safety python /tmp/f2c.py
reagent SMILES folding onto a DIFFERENT ICH row: [('methanol', 'CO', 'Cobalt (Co)')]
```

One collision in the whole fleet vocabulary, and it is methanol. The reporter's scan is honest.

I then pulled the two things the finding does not quote — the verdict string, and what the sibling
server already returns:

```
$ ... impurity_limit("CO").verdict
"Cobalt (Co): ICH Q3D(R2), Guideline for Elemental Impurities, ICH Step 4 (2022), Table A.2.1.
 Quote the citation with the number, and note that a limit is not a risk assessment."

$ ... solvent_properties('methanol')
SolventRecord(name='methanol', ..., smiles='CO', ich_class='2', ich_limit_ppm=3000.0, ...)
```

### Why

Three parts of the finding do not hold up, and together they cost it the "high".

**1. The wrong answer is loud, not silent.** This is the decisive difference from the multi-fragment
finding above. The payload's `limit.substance` is `"Cobalt (Co)"`, the `verdict` *opens* with
`"Cobalt (Co):"`, the guideline is `ICH Q3D(R2)` (elemental impurities) rather than `Q3C`
(residual solvents), the class is `2A` — a code that does not exist in Q3C — and the units are
µg/day rather than mg/day and ppm. A chemist who asked about methanol and is shown a cobalt PDE
under the elemental-impurity guideline is shown the contradiction, not hidden from it. The finding
leans on `_register`'s "the one failure worse than a miss" language, but the failure that guard
describes is a *silent* substitution where the returned row wears the queried substance's name.
This one does not: it names itself. That is materially less dangerous, and the finding does not
account for it anywhere.

**2. The named exploitation path is already short-circuited.** The finding's reachability argument
is "an agent chaining `solvent_properties` → `ich_impurity_limit` walks straight into it". I ran
`solvent_properties('methanol')`: it *already returns* `ich_class='2', ich_limit_ppm=3000.0` in the
same record as `smiles='CO'`. An agent on that chain has the answer without a second call, and if
it does make one it holds `name='methanol'` in the same object as the SMILES — passing the SMILES
instead of the name is a choice with no motivation. The residual reachable path is a *user* writing
methanol as `CO` in a route, which is real but is not the fleet-internal pipeline the finding
claims. The docstring at `tools.py:138-139` does advertise SMILES input, so the path exists; it is
just not the automatic one described.

**3. The error direction is conservative.** Cobalt's 50 µg/day against methanol's 30 mg/day is
~600× (the finding says "three orders of magnitude"; it is 2.8, minor). More to the point it is
600× *stricter*. A chemist acting on the wrong number over-controls a batch; nobody is exposed to
more methanol. For an impurity-limit answer that is the benign direction, and the finding does not
distinguish it.

What survives, and is worth fixing: `_fold`'s `.lower()` genuinely destroys the only information
that separates `CO` from `Co`, the element index genuinely takes precedence over the structure
fallback (`ich.py:287` before `:289`), and the collision guard genuinely cannot see a name-vs-structure
clash. That is a real defect with a real reachable trigger. It is a medium — a wrong-but-self-labelling
answer on a single-row collision reachable only when a caller volunteers a SMILES it did not need to
volunteer — not a high.

---

## `props` and `safety` disagree on triethylamine's ICH Q3C class and limit (5000 ppm vs 640 ppm)

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

### What I did

I rebuilt the sweep from scratch rather than reusing the reporter's, resolving every props row by
name and then by each alias against the safety ICH index:

```
$ uv run --project servers/safety python /tmp/f3b.py
MISMATCH ('cyclopentyl methyl ether', 'not_listed', '', '2', 1500.0)
MISMATCH ('triethylamine', '3', '5000', '2', 640.0)
total rows 44 mismatches 2
```

Exactly two, exactly the two named, and all 42 other resolving rows agree to the digit. Raw sources:

```
# servers/props/.../data/records.csv:40
triethylamine,TEA;Et3N,121-44-8,CCN(CC)CC,...,3,5000,hazardous,...
# servers/props/.../data/records.csv:22
cyclopentyl methyl ether,CPME,5614-37-9,COC1CCCC1,...,not_listed,,problematic,...
# servers/safety/.../data/ich_q3c/ich_q3c.yaml:233 / :115
Triethylamine  solvent_class "2"  pde 6.4   ppm 640
Cyclopentyl methyl ether  solvent_class "2"  pde 15.0  ppm 1500
```

And the `max_ich_class` consequence, which I ran rather than read:

```
$ uv run --project servers/props python -c "...swap_candidates(require('dichloromethane'), max_ich_class='3', top_n=40)..."
cyclopentyl methyl ether class not_listed ppm None blockers ()
triethylamine            class 3          ppm 5000.0 blockers ()
```

Both come back with an **empty `blockers` tuple** under a "nothing worse than Class 3" request.

### Why

**Reachability is trivial and needs no argument.** `solvent_properties` and `ich_impurity_limit`
are both in the agent-callable surface (`connectors/safety/connector.yaml:46`, and props' own
manifest); either can be asked about triethylamine in one turn. There is no gate, no cross-check,
and — I checked — no test anywhere that compares the two tables. `grep -rn "ich_class\|ich_q3c"
servers/*/tests tests/` finds only per-server self-consistency checks (`props/tests/test_dataset.py:123`
validates the class is one of the allowed strings; `safety/tests/test_dataset.py` validates the
ppm = PDE × 100 identity *within* the YAML). The invariant the finding proposes is genuinely unpinned.

**Consequence is as stated and I found one more.** 5000/640 = 7.81×, correct. The `max_ich_class`
claim is not a code-reading inference — it is the measured output above, and it is the sharper half
of the finding: `blockers` is the field both docstrings describe as the most useful part of the
answer, and here it is empty for a solvent the fleet's own guideline transcription puts in Class 2.
The `tools.py:213-215` docstring falsehood is real: it names CPME as an example of a solvent "ICH
Q3C does not name" while the sibling server carries CPME with a 15 mg/day PDE.

**What the reporter missed.** The props dataset does not merely carry stale values — it carries a
stale value under a provenance string that *claims* the newer revision:

```
$ grep -o "ICH Q3C(R[0-9])" servers/props/.../data/dataset.json
ICH Q3C(R8)
```

`dataset.json` declares "ICH Q3C(R8) classes and residual-solvent limits", and that string is
echoed verbatim into every `SolventRecord.source` the tool returns (I saw it in the
`solvent_properties('methanol')` dump). Triethylamine at Class 3 / 5000 ppm is the pre-R8 figure.
So the answer a chemist is shown attributes an out-of-date number to the revision that superseded
it — the dataset's own label is false, not just old. That is exactly the "a wrong number wearing a
real citation" failure the safety server's `_register` guard exists to prevent, arriving through
the sibling server instead.

I did not attempt to adjudicate which table matches the published guideline — the finding does not
depend on it. The defect is that one fleet returns two regulated numbers for one solvent and
silently filters on the more permissive one; whichever row is corrected, the disagreement is the
bug. High stands.
