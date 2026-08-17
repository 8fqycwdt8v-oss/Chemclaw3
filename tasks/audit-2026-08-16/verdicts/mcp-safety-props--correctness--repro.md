# Repro verdicts — `servers/safety/`, `servers/props/` — CORRECTNESS

Lens: does it actually reproduce? Every reproduction below is my own script, written from the
source, under `/tmp/vfy/`. Scope: the one `critical` and the two `high` findings.

Primary regulatory data used to settle the chemistry claims — fetched, not recalled:

- ICH Q3C(R8) Step 4, 22 April 2021 — `https://database.ich.org/sites/default/files/ICH_Q3C-R8_Guideline_Step4_2021_0422.pdf`
- ICH Q3C(R9) Step 5, EMA/CHMP/ICH/82260/2006 — `https://www.ema.europa.eu/en/documents/scientific-guideline/ich-q3c-r9-guideline-impurities-guideline-residual-solvents-step-5_en.pdf`

Both extracted to text with `pypdf` and quoted verbatim below.

All cited line numbers and symbols in all three findings were checked and are real and current
(`screen.py:426`, `:393`; `chem.py:48`; `ich.py:177`, `:286`; `genotox.py:201-217`;
`selection.py:141`; `records.csv:22`, `:40`).

---

## A multi-fragment SMILES silently defeats every pair rule and over-fires every counted rule

- **Verdict**: CONFIRMED
- **Severity I would assign**: high (one notch down from the filed `critical` — see the last paragraph)

**What I did.** Wrote `/tmp/vfy/f1.py` from scratch against `chemclaw_mcp_safety.tools.screen_hazards`
and `engine.genotox.screen_genotoxic_alerts` — not the reporter's script — and ran it under the
safety server's own `uv` project:

```
$ cd /workspace/chemclaw3-mcp/servers/safety && uv run python /tmp/vfy/f1.py
NaH + DCM 2 entries            -> ['saline-hydride-with-chlorinated-solvent']  screened=['[H-].[Na+]', 'ClCCl']
NaH.DCM 1 entry                -> []                                          screened=['ClCCl.[H-].[Na+]']
peroxide+acetone 2             -> ['peroxide', 'peroxide-with-ketone']
peroxide.acetone 1             -> ['peroxide']
NaN3 + DCM 2                   -> ['azide-with-dichloromethane', 'non-carbon-azide']
NaN3.DCM 1                     -> ['non-carbon-azide']
nitrobenzene                   -> []
2x nitrobenzene one string     -> ['polynitro-aromatic']
2x nitrobenzene two entries    -> []

VERDICT for NaH.DCM one entry: No rule in the hazard table matched. This is not a safety assessment.

genotox 2 entries -> ['nitrosatable-amine-with-nitrosating-agent']
genotox 1 entry   -> []
```

Every one of the finding's seven transcript lines reproduces, including the payload sentence and the
polynitro false positive in the opposite direction.

I then checked the finding's *premise* — that multi-fragment strings are the required form for this
server's own species — rather than taking it from the test file it cites. `/tmp/vfy/f1b.py` over the
vendored reagent CSV:

```
reagent rows: 61
multi-fragment single-species reagents: 17
    potassium carbonate  [K+].[K+].[O-]C([O-])=O
    sodium hydroxide     [Na+].[OH-]
    sodium hydride       [Na+].[H-]
    ...
legit reagents whose whole-mol flags exceed per-fragment flags: 0
```

**Why.** The mechanism is exactly as described. `parse_components` (`screen.py:327-334`) keys on the
caller's string, so `matches = [(a, b) ... if a != b]` at `:426` can only ever pair *entries*, never
fragments — two species inside one entry are structurally unable to form a pair. `tools.py:85-87`
routes `len(smiles) == 1` to `screen_structure`, which evaluates no pair rule at all. And `:393`
counts `GetSubstructMatches` over the whole parsed mol, so `min_matches: 2` (the only counted rule,
`polynitro-aromatic`, `rules.yaml:109`) counts across fragments. Nothing upstream prevents any of
this: `require_molecule` (`chem.py:48-80`) refuses whitespace, non-ASCII and empty strings, and says
nothing about fragment count.

The premise holds too — 17 of 61 vendored reagents are legitimately dot-joined, so "one structure per
list entry" is genuinely ambiguous and the server has no way to tell a salt from a mixture.

Two things pull it below `critical`, and I state them because they bound the fix rather than excuse
the defect:

1. **No legitimate input is affected.** All 61 reagent-table species screen identically whole-mol and
   per-fragment (0 over-fires, measured above), and correct usage — `["[Na+].[H-]", "ClCCl"]` — flags
   correctly. The defect fires only when a caller merges two species into one entry.
2. **Nothing in the fleet constructs that input.** `grep -rn "'\.'\.join"` across `chemclaw3-mcp`
   returns only `no_egress.py` and vendored packages; no caller in `/home/user/Chemclaw3/src` builds
   a dot-joined screen argument. The trigger is a model writing it, against a docstring
   (`tools.py:68-72`) that explicitly says "no two structures run together".

That is still a real, silent, dangerous-direction failure reachable from ordinary model output, which
is why it is `high` and not lower. The fix direction the finding gives (`GetMolFrags`, fragment
identity as the matching unit, per-fragment `min_matches`) is correct; its option (3) — refuse an
entry whose fragments are not one charge-balanced salt — is the cheaper correct answer and I would
take it first.

---

## `ich_impurity_limit("CO")` returns cobalt's PDE — `CO` is methanol, and the fleet's own props server emits exactly that string

- **Verdict**: CONFIRMED
- **Severity I would assign**: high

**What I did.** `/tmp/vfy/f2.py`, my own, against `engine.ich.impurity_limit`:

```
$ cd /workspace/chemclaw3-mcp/servers/safety && uv run python /tmp/vfy/f2.py
CO           -> 'Cobalt (Co)'      ICH Q3D(R2)  [('oral PDE', 50.0, 'µg/day'), ('parenteral PDE', 5.0, ...), ('inhalation PDE', 3.0, ...)]
Co           -> 'Cobalt (Co)'      ICH Q3D(R2)  ...
methanol     -> 'Methanol'         ICH Q3C(R9)  [('PDE', 30.0, 'mg/day'), ('concentration limit', 3000.0, 'ppm')]
C1CCOC1      -> 'Tetrahydrofuran'  ICH Q3C(R9)  [('PDE', 7.2, 'mg/day'), ...]

VERDICT CO: Cobalt (Co): ICH Q3D(R2), Guideline for Elemental Impurities, ICH Step 4 (2022),
            Table A.2.1. Quote the citation with the number, and note that a limit is not a
            risk assessment.
```

Confirmed the sibling server emits that exact string:

```
$ cd servers/props && uv run python -c "from chemclaw_mcp_props.engine import records; print(records.find('methanol').smiles)"
CO
```

I did not reuse the reporter's collision scan; I wrote my own over the props `all_solvents()` dump
plus the reagent CSV, folding each SMILES through `ich._fold` and comparing against `index()`:

```
--- props SMILES that fold onto an ICH key ---
  'methanol' smiles='CO' -> ICH row 'Cobalt (Co)'
--- reagent SMILES ---
  reagent 'methanol' smiles='CO' -> ICH row 'Cobalt (Co)'
```

Exactly one collision across both tables, and it is methanol — the same result the finding reports.

**Settled against primary data.** ICH Q3C(R9) Table 2 (Class 2 solvents), extracted verbatim from the
EMA PDF: `Methanol 30.0 3000`. So the correct answer for `CO` is PDE 30 mg/day / 3000 ppm under
Q3C(R9); the tool returns 50 µg/day under Q3D(R2) for a different element. Three orders of magnitude,
different substance, different guideline, real citation attached.

**Why.** `_fold` (`ich.py:177-184`) lowercases before indexing, which destroys the only thing that
distinguishes the SMILES `CO` from the element symbol `Co`. `impurity_limit` (`ich.py:286-292`)
consults the folded name index *first* and only falls back to `resolve_compound_name` on a miss — so
`CO` never reaches the structure path at all. The tool docstring (`tools.py:138-139`) advertises
SMILES input explicitly, and `_register`'s collision guard cannot see this because the collision is
name-key vs structure, not row vs row. Reachable by any agent chaining `solvent_properties` →
`ich_impurity_limit`. Nothing I found mitigates it. `high` is right.

---

## `props` and `safety` disagree on triethylamine's ICH Q3C class and limit (5000 ppm vs 640 ppm)

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

**What I did.** Ran my own cross-server sweep — dumped all 44 props solvents to JSON from the props
venv, then resolved each name and alias through `safety.engine.ich.impurity_limit` in the safety venv:

```
solvent                            props  pppm |  safety  sppm
cyclopentyl methyl ether      not_listed  None |       2  1500.0  <<< MISMATCH
triethylamine                          3  5000 |       2   640.0  <<< MISMATCH
n-butyl acetate               props=3 but SAFETY MISS
p-xylene                      props=2 but SAFETY MISS
total mismatches: 2 of 44
```

Same two rows the finding reports, and every other row agrees. The disagreement is real and
reproduces.

**Then I settled it against the guideline instead of against either server.** ICH Q3C(R9), Table 2
(Class 2 solvents), extracted verbatim:

```
Cyclohexane 38.8  3880
Cyclopentyl methyl ether2 15.0 1500
...
Xylene* 21.7 2170
```

**Triethylamine does not appear in Table 2.** It appears in Table 3 (Class 3), in both R8 and R9:

```
Table 3.   Class 3 solvents which should be limited by GMP or other quality-based requirements.
...
Formic acid Propyl acetate
 Triethylamine8
```

and Part V states the number outright:

> "The calculated PDE for TEA based upon the NOEL of the rat sub-chronic inhalation study is **62.5
> mg/day**. Since the proposed PDE is greater than 50 mg/day it is recommended that TEA be placed
> into **Class 3** ("solvents with low toxic potential") in Table 3 …"

**So the finding has the blame inverted on its own headline case.** `records.csv:40`
(`triethylamine, … 3, 5000`) is **correct** — Class 3, and 5000 ppm is the guideline's own Class 3
general limit. `ich_q3c.yaml:233-237` (`solvent_class: "2", pde 6.4, 640 ppm`) is a **fabricated row**:
that class/PDE pair exists nowhere in Q3C(R8) or R9. The finding's proposed fix — *"correct
`records.csv` (triethylamine → `2`, `640`)"* — would delete the one correct value in the fleet and
propagate the wrong one into a second server.

The claimed consequence collapses with it. I ran the filter:

```
$ uv run python -c "... swap_candidates(records.require('tetrahydrofuran'), max_ich_class='3', top_n=50)"
  cyclopentyl methyl ether    ich_class=not_listed  blockers=()
  2-methyltetrahydrofuran     ich_class=2           blockers=('is ICH Q3C class 2',)
  triethylamine               ich_class=3           blockers=('greenness band worsens to hazardous',)
```

Triethylamine is not blocked by the ICH filter — and it should not be, because it *is* Class 3. The
finding's sentence "a caller asking for 'nothing worse than Class 3' is handed triethylamine as
compliant when the fleet's own guideline transcription puts it in Class 2" is a statement about a
wrong transcription, not about the guideline.

**What survives, and what the reporter missed that is worse.** The CPME half is right: Q3C(R9) Table 2
carries `Cyclopentyl methyl ether 15.0 1500`, `records.csv:22` says `not_listed`, so the filter never
blocks it (`blockers=()` above) and the `tools.py:213-215` docstring naming CPME as unlisted is false.
That is a genuine, correctly-directed defect — one row, medium.

Worse than either: I validated **every** Class-2 row of `ich_q3c.yaml` against the real R9 Table 2 and
found the safety server carries **two** wrong rows, not one —

```
--- safety Class-2 rows not matching ICH Q3C(R9) Table 2 ---
   ('NOT IN REAL CLASS-2 TABLE', '2-Methyltetrahydrofuran', 5.0, 500)
   ('NOT IN REAL CLASS-2 TABLE', 'Triethylamine',           6.4, 640)
   ('NOT IN REAL CLASS-2 TABLE', 'Trichloroethylene',       0.8, 80)   <- naming only; real table
                                                                          says "1,1,2-Trichloroethene
                                                                          0.8 80". Not a defect.
```

Q3C(R9) Part VI on 2-MeTHF: *"The calculated PDE for 2-MTHF is 50 milligrams per day … it is
recommended that 2-MTHF be placed into class 3."* Both wrong rows are ~10× low on the PDE with the
class flipped to 2 — a decimal-shift transcription error.

And `records.csv:18` says `2-methyltetrahydrofuran, … 2, 500` — **both servers agree, and both are
wrong.** The consequence is live in the direction that matters: `swap_candidates` blocks 2-MeTHF as
"is ICH Q3C class 2" (measured above), so the standard green replacement for THF is excluded from
solvent-swap answers on a limit the guideline does not impose.

This is also the reason the finding's proposed remedy is insufficient on its own terms: a cross-server
consistency test would pass on the 2-MeTHF row, because the two servers agree with each other and
disagree with ICH. `ich_q3c.yaml`'s own header comment claims *"An adversarial review verified all 62
values"*; two of them are wrong, and both satisfy the ppm = PDE × 100 identity the file offers as its
self-check, which is exactly why the check did not catch them.

Verdict OVERSTATED rather than REFUTED: a real inconsistency, reproduced independently, but the
finding names the wrong server as the defective one on the case in its title, its stated consequence
does not hold for that case, and its fix would introduce a regulatory error. The correct action is
to fix `ich_q3c.yaml` (triethylamine → Class 3; 2-MeTHF → Class 3), fix `records.csv` for CPME and
2-MeTHF, and pin both tables against the published guideline — not against each other.
