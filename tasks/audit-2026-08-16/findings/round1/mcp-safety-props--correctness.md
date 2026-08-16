# Round 1 — `servers/safety/`, `servers/props/` — CORRECTNESS

Repo: `/workspace/chemclaw3-mcp`. Baseline: `uv run pytest -q servers/safety/tests servers/props/tests`
→ **286 passed**. Every finding below is on green code, and every reproduction was run.

---

## A multi-fragment SMILES silently defeats every pair rule and over-fires every counted rule

- **Severity**: critical
- **Location**:
  - `servers/safety/src/chemclaw_mcp_safety/engine/screen.py:426` (`screen_reaction`, `matches = [(a, b) for a in left for b in right if a != b]`)
  - `servers/safety/src/chemclaw_mcp_safety/engine/screen.py:393` (`screen_structure`, `len(molecule.GetSubstructMatches(...)) >= rule.min_matches`)
  - `servers/safety/src/chemclaw_mcp_safety/engine/genotox.py:201-217` (`screen_genotoxic_alerts`, formation pairs)
  - `servers/safety/src/chemclaw_mcp_safety/engine/chem.py:48` (`require_molecule` — accepts any number of dot-separated fragments)
- **Trigger**: any call where one list entry carries more than one species, e.g.
  `screen_hazards(["[H-].[Na+].ClCCl"])` — sodium hydride in dichloromethane.
- **Consequence**: **a documented runaway hazard screens completely clean**, with the payload
  sentence the module was built to make impossible:

```
screen_hazards(["[H-].[Na+].ClCCl"]) ->
{"flags": [], "screened": ["ClCCl.[H-].[Na+]"],
 "verdict": "No rule in the hazard table matched. This is not a safety assessment."}
```

  Written as two entries the same chemistry flags correctly:

```
NaH + DCM (2 comps)  -> ['saline-hydride-with-chlorinated-solvent']
NaH.DCM  one comp    -> []
OO + acetone         -> ['peroxide', 'peroxide-with-ketone']
OO.acetone one comp  -> ['peroxide']            # TATP formation rule silent
NaN3 + DCM           -> ['azide-with-dichloromethane', 'non-carbon-azide']
NaN3.DCM one comp    -> ['non-carbon-azide']    # diazidomethane rule silent
```

  The genotoxicity screen has the identical hole:

```
amine + nitrite (2 entries)     -> ['nitrosatable-amine-with-nitrosating-agent']
'CN(C)C.[Na+].[O-]N=O' (1 entry) -> []          # verdict: "No structural alert in the table matched."
```

  The same defect runs the *other* way for `min_matches` rules, because those count over the whole
  parsed molecule rather than per fragment — two ordinary mononitroarenes in one string are reported
  as an explosive:

```
nitrobenzene                              -> []
'O=[N+]([O-])c1ccccc1.O=[N+]([O-])c1ccccc1' -> ['polynitro-aromatic']   # severity high, "archetypal explosive motif"
```

- **Evidence**: `screen_reaction` matches pair arms across the *component-list keys* (`a != b` where
  `a`/`b` are the caller's strings), so two species inside one string can never form a pair; and
  `tools.screen_hazards` routes `len(smiles) == 1` to `screen_structure`, which does not evaluate
  pair rules at all. Meanwhile `GetSubstructMatches` counts across all fragments of the parsed mol.
  This is not an input the code may simply reject: multi-fragment strings are the *required* form
  for the salts the rule table is written around — `[N-]=[N+]=[N-].[Na+]`, `[H-].[Na+]`, the
  hydrazinium salts — and `servers/safety/tests/test_canonicalization_contract.py:55-57` says so
  explicitly ("Salts: every fragment kept … hydrazinium salts are all multi-fragment, and every one
  of them is a rule's reference molecule"). So the server cannot tell "one salt" from "two species",
  and the caller has no way to know the distinction matters. Every module docstring in the package
  ("an empty result must never read as a clearance", `require_screenable_size` refusing rather than
  truncating) is defeated by this one input shape.
- **Fix**: split with `Chem.GetMolFrags(mol, asMols=True)` and make fragment identity the unit of
  matching, not the caller's string.
  1. In `parse_components`, expand each entry to its fragments and key pair matching on
     `(entry_index, fragment_index)` — an ion pair's fragments must stay excluded from each other
     (test `[H-].[Na+]` alone still yields no pair flag), which a charge check on the fragment set
     gives you, but two *neutral* fragments in one entry must be paired.
  2. In `screen_structure`, evaluate `min_matches` per fragment (`max` over fragments) rather than
     over the whole molecule, so two mononitroarenes stay unflagged and a nitro-phenolate salt still
     counts correctly.
  3. Failing (1), refuse an entry whose fragments are not a single charge-balanced salt, with a
     message telling the caller to pass one species per entry — a refusal is the one outcome this
     package says is acceptable, and it is far better than the clean screen above.

---

## `ich_impurity_limit("CO")` returns cobalt's PDE — `CO` is methanol, and the fleet's own props server emits exactly that string

- **Severity**: high
- **Location**: `servers/safety/src/chemclaw_mcp_safety/engine/ich.py:177` (`_fold`) and
  `ich.py:286-292` (`impurity_limit` — the name index is consulted *before* the SMILES fallback)
- **Trigger**: `ich_impurity_limit("CO")`.
- **Consequence**: the elemental-impurity PDE for **cobalt** is returned for the SMILES of
  **methanol**, a Q3C-listed solvent the tables *do* carry (Class 2, PDE 30 mg/day, 3000 ppm), under
  a genuine ICH Q3D citation:

```
{"query": "CO",
 "limit": {"substance": "Cobalt (Co)", "guideline": "ICH Q3D(R2) …",
           "limit_class": "Class 2A",
           "limits": [{"basis": "oral PDE", "value": 50.0, "unit": "µg/day"},
                      {"basis": "parenteral PDE", "value": 5.0, "unit": "µg/day"},
                      {"basis": "inhalation PDE", "value": 3.0, "unit": "µg/day"}],
           "citation": "ICH Q3D(R2) …, Table A.2.1"},
 "verdict": "Cobalt (Co): ICH Q3D(R2) …. Quote the citation with the number, …"}
```

  Correct answer for `"methanol"`: `PDE 30 mg/day, concentration limit 3000 ppm`. The returned
  number is off by three orders of magnitude *and* is about a different substance and a different
  guideline. `ImpurityLimitLookup.verdict` even instructs the model to quote the citation.
- **Evidence**: `_fold` lowercases, which is right for names and fatal for structures — SMILES
  distinguishes `CO` (methanol) from `Co` (cobalt) by case alone, and the tool docstring
  (`tools.py:138-139`) advertises that a SMILES is accepted: *"Accepts the guideline's spelling, an
  element symbol, an abbreviation a chemist writes, or a SMILES — `Pd`, `palladium`, `THF`,
  `2-MeTHF` and `C1CCOC1` all resolve."* This is not hypothetical input: the sibling server publishes
  methanol's SMILES as literally `CO` —

```
$ uv run python -c "from chemclaw_mcp_props.engine import records; print(records.find('methanol').smiles)"
CO
```

  so an agent chaining `solvent_properties` → `ich_impurity_limit` walks straight into it. A scan of
  every SMILES in both the props solvent table (44 rows) and the safety reagent table (61 structures)
  against the folded ICH index found exactly one collision, and it is methanol:

```
--- props solvent SMILES that fold onto an ICH key ---
  'methanol' smiles='CO' -> ICH row 'Cobalt (Co)' (ICH Q3D(R2))
--- reagent table smiles ---
  reagent 'methanol' smiles='CO' -> ICH row 'Cobalt (Co)'
```

  `_register`'s collision guard (`ich.py:187-202`, "whichever loaded second would silently win — the
  reader would get a limit for a different substance with a real citation attached to it … the one
  failure worse than a miss") cannot see this, because the collision is between a *name key* and a
  *structure*, not between two rows.
- **Fix**: try the structure interpretation first, or at least disambiguate before folding. Concretely,
  in `impurity_limit`, run `resolve_compound_name(substance)` *before* the folded name lookup when
  `substance` parses as a SMILES **and** its case differs from the matched key's case; simplest
  correct form: if `require_canonical_smiles(substance)` succeeds and resolves to a reagent-table
  row, prefer that row's name. Additionally index element symbols case-sensitively (a second,
  unfolded symbol map consulted only on an exact-case match) so `Co` still resolves and `CO` does not.

---

## `props` and `safety` disagree on triethylamine's ICH Q3C class and limit (5000 ppm vs 640 ppm)

- **Severity**: high
- **Location**: `servers/props/src/chemclaw_mcp_props/data/records.csv:40` (triethylamine),
  `records.csv:22` (cyclopentyl methyl ether); contradicted by
  `servers/safety/src/chemclaw_mcp_safety/data/ich_q3c/ich_q3c.yaml:233-237` and `:115-119`.
  Consumed by `servers/props/src/chemclaw_mcp_props/tools.py:228` (`solvent_properties`) and
  `servers/props/src/chemclaw_mcp_props/engine/selection.py:141-146` (the `max_ich_class` filter).
- **Trigger**: `solvent_properties("triethylamine")` vs `ich_impurity_limit("triethylamine")`.
- **Consequence**: two servers of one fleet return two different regulated numbers for one solvent,
  and the props one is **7.8× more permissive**:

```
solvent                       props class  props ppm   ICH(safety) class  ICH ppm
triethylamine                 3            5000.0      2                  640.0    <<<
cyclopentyl methyl ether      not_listed   None        2                  1500.0   <<<
```

  (Sweep over all 44 props rows against the safety Q3C/Q3D index; every other row that resolved
  agreed exactly.) `ich_q3c.yaml` is transcribed from Q3C(R9) and carries `Triethylamine, class "2",
  pde_mg_per_day: 6.4, concentration_limit_ppm: 640` and `Cyclopentyl methyl ether, class "2",
  pde 15.0, 1500 ppm`. The props CSV is the pre-R8 picture.

  It is not only a reporting mismatch: `selection.swap_candidates`'s `max_ich_class` filter reads the
  props column, so a caller asking for "nothing worse than Class 3" is handed triethylamine as
  compliant when the fleet's own guideline transcription puts it in Class 2.

  The props tool docstring also states the falsehood directly (`tools.py:213-215`): *"`ich_class` is
  `not_listed` for solvents ICH Q3C does not name (CPME, dimethyl carbonate, propylene carbonate)"* —
  the sibling server's Q3C table names CPME with a 15 mg/day PDE.
- **Evidence**: the sweep above, plus the two raw rows:

```
# servers/props/.../data/records.csv:40
triethylamine,TEA;Et3N,121-44-8,CCN(CC)CC,...,3,5000,hazardous,...
# servers/safety/.../data/ich_q3c/ich_q3c.yaml:233
  - name: Triethylamine
    synonyms: [TEA, Et3N]
    solvent_class: "2"
    pde_mg_per_day: 6.4
    concentration_limit_ppm: 640
```
- **Fix**: correct `records.csv` (triethylamine → `2`, `640`; CPME → `2`, `1500`), refresh
  `data/dataset.json`'s sha256, and drop CPME from the `not_listed` example list in
  `tools.py:213-215`. Then add a cross-server test asserting that for every props row whose name or
  alias resolves in the safety Q3C index, `ich_class` and `ich_limit_ppm` agree — the mismatch is
  mechanical and a test would have caught both rows. `servers/*/tests/test_fleet.py` already pins the
  reagent CSV byte-identical across two servers; this is the same class of invariant, unpinned.

---

## `boiling_point_at` has no upper bracket check and silently returns 400 °C for an unreachable pressure

- **Severity**: medium
- **Location**: `servers/props/src/chemclaw_mcp_props/engine/correlations.py:164-176`
  (`boiling_point_at`)
- **Trigger**: any `pressure_mbar` above the solvent's vapour pressure at
  `max(bp_c + 200, 400) °C`. For water that threshold is ~197,200 mbar.
- **Consequence**: bisection converges to the top of the bracket and the function returns it as a
  boiling point, with no error and with a confident `method: "antoine"` and its "good to about a
  percent" caveat attached:

```
target      1013.0 mbar -> T=100.444 C ; vp at that T =    1013 mbar (antoine)   OK
target    100000.0 mbar -> T=332.430 C ; vp at that T =   1e+05 mbar (antoine)   OK
target  10000000.0 mbar -> T=400.000 C ; vp at that T = 1.972e+05 mbar (antoine) WRONG, 50x low
target      1e+12 mbar  -> T=400.000 C ; vp at that T = 1.972e+05 mbar (antoine) WRONG
```

  The tool returns `BoilingPointResult(pressure_mbar=1e7, boiling_point_c=400.0, method="antoine",
  caveat="…good to about a percent near the fitted range…")` — a plausible-looking number that does
  not satisfy the equation it claims to solve. The low side *is* guarded (`correlations.py:165-169`
  raises "it freezes in the still before it boils at that vacuum"); the high side is not.
- **Evidence**: the loop at `correlations.py:170-176` never checks
  `vapour_pressure(solvent, high).pressure_bar >= target_bar`, so when the invariant fails the
  bisection is a no-op that returns `high`. Script output above.
- **Fix**: mirror the low-side guard —

```python
if vapour_pressure(solvent, high).pressure_bar < target_bar:
    raise ValueError(
        f"{solvent.name} does not reach {pressure_mbar} mbar below {high} °C; "
        "this server carries no supercritical or high-pressure data"
    )
```

  placed immediately after the existing low-side check.

---

## `swap_candidates` truncates away the blocked candidate its docstring promises to show

- **Severity**: medium
- **Location**: `servers/props/src/chemclaw_mcp_props/engine/selection.py:165-166`
  (`scored.sort(...)` then `return scored[:top_n]`)
- **Trigger**: any swap query where at least `top_n` candidates pass every constraint, e.g.
  `solvent_swap_candidates("N,N-dimethylformamide", exclude_peroxide_formers=True, max_ich_class="3")`
  with the default `top_n=5`.
- **Consequence**: the *closest* candidate in Hansen space is dropped from the answer entirely,
  taking its `blockers` with it — which is precisely the sentence both docstrings say is the most
  useful part of the result:

```
returned (default top_n=5):                nearest overall:
  dimethyl sulfoxide 3.54 ()                 N,N-dimethylacetamide 2.74 ('is ICH Q3C class 2',)  <-- dropped
  dimethyl carbonate 6.56 ()                 dimethyl sulfoxide    3.54 ()
  acetone            6.62 ()                 N-methylpyrrolidone   4.50 ('is ICH Q3C class 2',)  <-- dropped
  methyl ethyl ketone 8.27 ()                dimethyl carbonate    6.56 ()
  formic acid        8.35 ()                 acetone               6.62 ()
```

  `tools.py:323-327` states: *"candidates that fail a constraint are still returned, after the ones
  that pass, each with the constraint it failed named in `blockers`. That is deliberate — 'toluene is
  the closest match but it is reprotoxic cat 2' is usually the most useful sentence in the answer."*
  The sort puts blocked entries last and the slice then deletes them, so the guarantee holds only
  when fewer than `top_n` candidates pass — the case where the caller least needs it.
- **Evidence**: script output above; `selection.py:165` sorts on `(bool(blockers), hansen_distance)`
  and `:166` slices the combined list.
- **Fix**: slice the two groups separately — take `top_n` passing candidates and then append the
  nearest few blocked ones (e.g. `min(top_n, 3)`), so a blocked-but-closer candidate is always
  visible. Same call site should reject a non-positive `top_n`: `top_n=0` currently returns an empty
  list and `top_n=-1` returns 42 of 43 candidates (measured), because `scored[:top_n]` reads a
  negative bound as "all but the last N".

---

## The ICH lookup misses two Q3C solvents whose names the sibling `props` server emits

- **Severity**: medium
- **Location**: `servers/safety/src/chemclaw_mcp_safety/engine/ich.py:177-184` (`_fold`) —
  no `n-`/`o-`/`m-`/`p-` positional-prefix handling, and no synonym for the Q3C mixture row.
- **Trigger**: `ich_impurity_limit("n-butyl acetate")`, `ich_impurity_limit("p-xylene")` — both are
  the *canonical names* returned by `props.list_solvents()`.
- **Consequence**: the tool answers *"No entry … in the transcribed ICH Q3C/Q3D tables. That means
  this system does not carry the number"* about two solvents the transcription **does** carry:
  `ich_q3c.yaml:268` (`Butyl acetate`, Class 3, 5000 ppm) and `ich_q3c.yaml:238` (`Xylene`, Class 2,
  2170 ppm). `read_table`'s own docstring (`screen.py:216-218`) names this exact outcome as the thing
  the loader exists to prevent: *"an empty ICH index reports 'this system does not carry the number'
  for a substance it does."*

```
n-butyl acetate      MISS      # ich_q3c.yaml has "Butyl acetate", class 3, 5000 ppm
p-xylene             MISS      # ich_q3c.yaml has "Xylene",        class 2, 2170 ppm
butyl acetate        Butyl acetate
xylene               Xylene
n-heptane            Heptane   # resolves — via the reagent-table fallback
n-hexane             Hexane    # resolves — via the reagent-table fallback
```

- **Evidence**: script output above. `_fold` keeps only alphanumerics, so `n-butyl acetate` →
  `nbutylacetate` ≠ `butylacetate`; `n-heptane`/`n-hexane` survive only because the reagent table
  happens to carry those spellings. Direction of error is safe (a miss, not a wrong number), but the
  tool's whole purpose is to stop the model recalling a limit, and a miss on a solvent in the table
  removes the only alternative.
- **Fix**: add the spellings to the two YAML rows' `synonyms` (`n-butyl acetate`, `n-butylacetate`;
  `p-xylene`, `o-xylene`, `m-xylene`, `xylenes`) and refresh the `dataset.json` checksums. A test
  asserting that every `props` solvent name resolves in the ICH index or is genuinely absent from
  Q3C would keep the two servers' vocabularies aligned.

---

## One substance written two ways is counted as two hazard flags, contradicting the dedup comment

- **Severity**: low
- **Location**: `servers/safety/src/chemclaw_mcp_safety/engine/screen.py:407` (docstring claim) and
  `:419-420` (`parse_components` keyed on the caller's spelling, then
  `flags = [flag for smiles in molecules for flag in screen_structure(smiles).flags]`);
  same shape at `genotox.py:189-200`.
- **Trigger**: `screen_hazards(["OO", "[H][O][O][H]"])` or `screen_hazards(["CN=[N+]=[N-]",
  "[N-]=[N+]=NC"])` — one substance, two spellings.
- **Consequence**: the flag list and the verdict double-count, while `screened` correctly reports one
  molecule, so the two halves of the same payload disagree:

```
screened: ['OO']  nflags 2
verdict:  "2 hazard rule(s) matched (most serious: high). …"
  peroxide OO
  peroxide [H][O][O][H]
```

  `screen_reaction`'s docstring says the opposite (`screen.py:407`): *"Structural flags from the
  components are deduplicated per (rule, molecule) so a reagent listed twice is reported once."*
  It deduplicates per (rule, **caller's string**), which the code beside it already knows is not the
  same thing — `screen.py:437-440` explicitly re-deduplicates `screened` after canonicalising, "because
  `molecules` is keyed on the caller's spelling". The flag list never got the same treatment. Same
  effect inflates `AlertResult.verdict`'s motif list in the genotoxicity screen.
- **Evidence**: script output above (run directly against `screen_reaction`).
- **Fix**: key `parse_components` results on the canonical SMILES while keeping the caller's spelling
  as the value used for `HazardFlag.matched` (first spelling wins), or deduplicate `flags` on
  `(rule_id, canonical(matched))` before `_sorted`. Note this interacts with the multi-fragment
  finding above — do the fragment split first, then dedup on the fragment set.

---

## Checked and clean (no finding)

- **Concurrency on the shared compiled SMARTS patterns.** `tools.py` offloads both screens to
  `asyncio.to_thread` over `lru_cache`d, process-shared RDKit query mols. 4,000 screens across 64
  threads after a `cache_clear()`, over five molecules with pre-computed expected flag sets:
  **0 mismatches, 0 errors.**
- **Antoine transcription.** For all 44 props solvents, `vapour_pressure(s, s.bp_c)` is within 5% of
  1013.25 mbar — no transposed constant, and the NIST bar/Kelvin form matches the stored columns.
- **The Trouton fallback's error is bounded and the caveat is honest.** Forcing the Trouton route on
  every solvent that has Antoine constants and comparing at 20 °C gives a worst case of 2.57×
  (2-propanol), 2.42× (water), 2.10× (ethanol) — hydrogen-bonding liquids, exactly the class
  `_caveat` names, and in the direction it names ("likely too high").
- **`verdict` survives serialization.** It is a `computed_field` on both result models and
  `servers/safety/tests/test_server.py` asserts it over a real MCP handshake.
- **Dataset checksums are genuinely verified on load** (`mcp_server_kit/datasets.py:96-102`), so the
  "a swapped table would be caught" claims in `screen.py` and `ich.py` hold.
- **`max_ich_class`'s inverted-looking comparison is correct.** `int(candidate.ich_class) <
  int(max_ich_class)` blocks *lower*-numbered (worse) Q3C classes, which is what the docstring says.
