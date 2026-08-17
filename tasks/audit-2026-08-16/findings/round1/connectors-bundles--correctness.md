# Round 1 — `src/chemclaw/connectors/{calc,bo,qm,chem,safety,molfp,rxnfp}` — CORRECTNESS

Six findings. Two produce a wrong scientific number with nothing raising anywhere; both are
reproduced below. Everything asserted here was run — scripts are under
`/tmp/claude-0/-home-user-Chemclaw3/41f2465f-44e8-5661-9ba7-5183da558c73/scratchpad/`, with
Postgres and Temporal up (`make up`, `make db-migrate`).

Two findings turn on the contract with `8fqycwdt8v-oss/Chemclaw3-mcp` (`servers/calc`), since the
physics left this repo. I read that server's **code** (not its docs) at
`92170117d8f25cc5588ad2a014c98a1bc14cdb04` to settle them rather than guessing.

---

## `predict_site_reactivity` re-ranks a set the server already truncated by the *other* mode

- **Severity**: high
- **Location**: `/home/user/Chemclaw3/src/chemclaw/connectors/calc/server/tools.py:782-797`
  (`predict_site_reactivity`), against
  `Chemclaw3-mcp:servers/calc/src/chemclaw_mcp_calc/tools.py::predict_site_reactivity`
- **Trigger**: any `predict_site_reactivity(smiles, mode="nucleophilic")` (or `"radical"`) on a
  molecule with more than 15 atoms including hydrogens — i.e. essentially every drug-sized
  molecule. Ibuprofen (33 atoms) is enough.
- **Consequence**: the returned "most susceptible site" is not the most susceptible site. The
  ranking is drawn from the 15 atoms that happened to top the **electrophilic** ranking, and
  f⁻ / f⁺ are near-anticorrelated by construction (an electron-rich site is a poor acceptor), so
  the nucleophilic winner is systematically among the atoms that were discarded. This is a
  confidently wrong regiochemistry answer — the exact failure `SiteReactivityResult.ranked_for`'s
  own docstring says it exists to prevent ("a confidently wrong regiochemistry answer, with nothing
  raising anywhere"). It is also a *poisoned cache row*: the truncated payload is persisted under
  the mode-independent `xtb.fukui` key and, per D-011, is never recomputed.

- **Evidence**: the call sends neither `mode` nor `top_n`:

  ```python
  # connectors/calc/server/tools.py:782-793
  payload, _ = await cached_remote(
      default_store(),
      "predict_site_reactivity",
      # … "`top_n` is left off for the same reason in the other direction: the row
      # holds every atom, so asking for more sites re-slices a cached result …"
      {"smiles": smiles},
  )
  result = SiteReactivityResult.model_validate(payload).ranked_for(mode)
  ```

  **"the row holds every atom" is false.** The server's tool body truncates *after* ranking:

  ```python
  # Chemclaw3-mcp servers/calc/.../tools.py::predict_site_reactivity
  result = xtb_props.compute_fukui(*xtb_props.fukui_inputs(smiles), mode)
  limit = top_n if top_n > 0 else settings.xtb_fukui_top_n     # engine/config.py: 15
  return result.model_copy(update={"sites": result.sites[:limit]})
  ```

  With no `mode` and no `top_n` sent, that is `mode="electrophilic"` (sorted by `f_minus`) and
  `limit=15`. The half the comment gets *right* is that the key ignores `mode` — I confirmed
  `fukui_inputs` returns `XtbSpec(task="fukui", solvent=…)` with no mode — which is precisely why
  the truncation is invisible: the nucleophilic request never reaches the server to be re-truncated.

  Driving the real `predict_site_reactivity` against a stand-in that reproduces the server's
  truncation (`scratchpad/probe_fukui_truncation.py`):

  ```
  ibuprofen has 33 atoms; server-side xtb_fukui_top_n = 15
  arguments Chemclaw3 sent: [{'smiles': 'CC(C)Cc1ccc(cc1)C(C)C(=O)O'}]
  electrophilic top site : idx 0 (f-=1.0)  [correct]
  nucleophilic  top site : idx 14 (f+=0.4242)
                   truth : idx 32 (f+=0.9697)
  WRONG ANSWER: True
  total_atoms reported   : 33; sites returned: 15
  top_n=100 asked, sites returned: 15 of 33   ("pass a larger number to see the whole molecule")
  ```

  Note the second, milder half: `top_n` is documented as "pass a larger number to see the whole
  molecule" and cannot do so — 15 is a hard ceiling once the row is written.

  `tests/calc_server_fake.py` cannot catch this. Its `_predict_site_reactivity` returns **every**
  atom, so the fake's own promise ("a fake that got them wrong would make the tests pass on a
  design that fails in production") does not hold for this property, which is not one of the three
  it reproduces.

- **Fix**: send the arguments that make the claim true — `{"smiles": smiles, "top_n": 0}` will not
  do it, so pass an explicit unbounded request. The server honours `top_n` verbatim, so
  `{"smiles": smiles, "top_n": <a value ≥ any molecule's atom count>}` is the minimal change;
  cleaner is to add a server-side "all atoms" spelling. Either way the argument must **not** move
  the key — it does not today (`fukui_inputs` ignores both `mode` and `top_n`), and
  `tests/calc_server_fake.py::_KEYED` already models `predict_site_reactivity` with an empty param
  tuple, so adding it is safe. Then make the fake truncate exactly as the server does, and assert
  that the nucleophilic top site for a >15-atom molecule is the true f⁺ maximum. Every already
  stored `xtb.fukui` row is truncated and must be invalidated (bump `CALCULATION_EPOCH`, or scope a
  delete to `calc_type='xtb.fukui'`).

---

## `parse_qm_output` silently truncates a scientific-notation energy instead of raising

- **Severity**: high
- **Location**: `/home/user/Chemclaw3/src/chemclaw/connectors/qm/activities.py:45-46,146-163`
  (`_ENERGY_RE`, `_CONVERGED_RE`, `parse_qm_output`)
- **Trigger**: `hpc_launch_interface="nextflow"`, i.e. the real cluster path (F5). `fetch_artifacts`
  returns whatever `qm_output.txt` the pipeline wrote; the pipeline is not this repo's code and QM
  programs print energies in scientific notation as a matter of course.
- **Consequence**: `re.compile(r"energy=(-?\d+\.\d+)")` is a `search` for a *mantissa*. It matches
  the leading digits of `-1.5423156E+02` and discards the exponent, so a DFT total energy comes back
  **two orders of magnitude wrong**, `converged=True`, no exception. That value is then
  (a) persisted by `persist_qm_result` into `calculation_results`, which `durable/retention.py`
  never prunes and D-011 says is never recomputed, and (b) published through the PR-gate as a
  `job-result` note whose `Estimate` carries `in_domain=True` and `uncertainty=None`
  (`connectors/qm/knowledge.py::qm_energy_estimate`). The docstring's claim — "raises on
  unparseable output so a corrupt result never silently becomes a `converged=False`, energy-0
  record" — is exactly inverted: the regexes do not raise on these inputs, they return a
  plausible number. A multi-cycle SCF log is the same failure by a different route, because
  `search` takes the *first* match, not the last.
- **Evidence** (`scratchpad/probe_parse.py`, run against the real activity):

  ```
  mock shape                   -> energy=-12.3        converged=True
  scientific notation          -> energy=-1.5423156   converged=True     # was -1.5423156E+02
  no decimal point             -> RAISED ValueError: unparseable QM output: 'energy=-154 converged=True'
  SCF trace, final line last    -> energy=-153.1       converged=False    # cycle 1, not cycle 2
  fortran D exponent           -> energy=-1.542315    converged=True     # was -1.542315D+02
  ```

  The mock never exposes this: `_MOCK_OUTPUT_TEMPLATE` is `"{energy:.6f}"`, which is always a plain
  decimal, so CI and every local run see the one shape that works. Note also that `energy=-154`
  (an integer-valued energy) *raises*, after an hours-long cluster run and with nothing persisted
  before the parse — the whole run is lost.
- **Fix**: anchor the pattern and accept the numeric forms QM codes emit, then require exactly one
  match. Concretely: `re.compile(r"^\s*energy\s*=\s*(-?\d+(?:\.\d*)?(?:[eEdD][-+]?\d+)?)\s*$", re.M)`
  with `findall`, refusing when the count is not 1, and normalising a Fortran `D` exponent to `E`
  before `float()`. Same treatment for `converged`. Add a test table of the five strings above.

---

## `report_measurement` accepts any `property_name` and reports the row as scorable

- **Severity**: medium
- **Location**: `/home/user/Chemclaw3/src/chemclaw/connectors/calc/server/tools.py:133-181`
  (`report_measurement`), vs `_CALIBRATED` / `_calibrated` at lines 421-453
- **Trigger**: `report_measurement("pKa", "CCO", 4.2)` — the natural spelling, and the one this
  module itself uses as the pKa **unit** string (`_CALIBRATED["pka"] = ("predict_pka", "pKa")`).
  Also `"logP"`, `"solubility "` with a trailing space, or any other string.
- **Consequence**: the value is written into `measurements` under a `property` no calculator ever
  writes, so `_RECONCILE_FROM_MEASUREMENT` (which joins `p.calc_type = m.property`) can never match
  it, and the chemist is told: *"the measurement is kept and the next prediction of it will be
  scored against this value."* It never will. `calculator_trust`/`calculator_outliers` reject an
  unknown property by name — the module's own comment brags about it ("asking about an uncalibrated
  one is an error that names what does exist") — and the *write* side, which is the one that can
  lose data, has no such check.
- **Evidence** (`scratchpad/probe_report_measurement.py`, live against the compose Postgres with
  `CHEMCLAW_CALIBRATION_ENABLED=true`):

  ```
  property_name='pKa'         -> Recorded for CCO. Nothing had predicted pKa for it yet, …
  property_name='logP'        -> Recorded for CCO. Nothing had predicted logP for it yet, …
  property_name='solubility ' -> Recorded for CCO. Nothing had predicted solubility  for it yet, …
  measurements row: ('pKa', 'CCO', 4.2, '')
  measurements row: ('logP', 'CCO', 4.2, '')
  measurements row: ('solubility ', 'CCO', 4.2, '')
  ```

  (The empty fourth column is a second, smaller miss: the call passes no `unit`, so every
  chemist-reported measurement is stored with `unit=''`.)
- **Fix**: validate against the same table the read side uses — `if property_name not in _CALIBRATED:
  raise ValueError(...)`, with the same "known: …" message `_calibrated` already produces (extract
  that check into one function, since it is now three call sites). Pass the unit from `_CALIBRATED`
  into `record_observation(..., unit=unit)`.

---

## The caller's `temperature_k` never reaches the CREST search, though it keys and changes it

- **Severity**: medium
- **Location**: `/home/user/Chemclaw3/src/chemclaw/connectors/calc/compose.py:365-409`
  (`conformer_ensemble`), reached from `_species_energy` at `level="thorough"` (line 605) and from
  `activities.py:151` (`sample_conformers`)
- **Trigger**: any `thorough` reaction/solvent job with a non-default `temperature_k`, e.g.
  `ReactionJobSpec(..., level="thorough", temperature_k=350.0)`; or any deployment whose calc-server
  pod has a different `CHEMCLAW_XTB_THERMO_TEMPERATURE_K` from this one.
- **Consequence**: the remote arguments are `{"structure", "search", "effort", "solvent"}` — no
  temperature — so the server samples at *its own* configured default and Chemclaw3 then Boltzmann-
  reweights that sample at the caller's temperature, reporting the result as the ensemble at that
  temperature. `compose.conformer_ensemble`'s docstring states the premise — "populations and the
  conformational entropy depend on a temperature **the search never saw**" — and the collaborator's
  code contradicts it: `crest_cli.run` does `argv += ["--temp", str(temperature_k or
  settings.xtb_thermo_temperature_k)]`, and `EnsembleSpec` carries `temperature_k` as a **keyed**
  field. So the temperature both moves the sampling and is part of the server's key; the client
  simply cannot vary it. The conformational entropy and `ensemble_correction_kcal` that feed
  `_species_energy`'s ΔG are computed over a sample drawn at the wrong temperature, with no warning.
  Because the two pods read the *same* env var name, a config drift between them makes the sampling
  and weighting temperatures disagree silently.
- **Evidence**: `compose.py:387-399` sends four arguments and no fifth;
  `Chemclaw3-mcp:servers/calc/.../engine/crest_search.py::EnsembleSpec` —
  `temperature_k: float = Field(default_factory=lambda: settings.xtb_thermo_temperature_k, gt=0)`
  with "Every field enters the key through `model_dump()`"; and
  `engine/crest_cli.py::run` passing it to `--temp`. `Chemclaw3`'s own
  `science/calc/models.py::EnsemblePayload` docstring says the opposite of the server's config
  comment about the same field.
- **Fix**: pass it — add `"temperature_k": temperature_k or settings.xtb_thermo_temperature_k` to
  the `search_conformer_ensemble` arguments (the server tool already accepts it and defaults `0.0`
  to its own setting), and thread the caller's temperature through `sample_conformers` /
  `EnsembleJobSpec`, which currently has no temperature field at all. Then correct
  `conformer_ensemble`'s and `EnsemblePayload`'s docstrings: a second temperature is a second
  search, not a free re-weight. If the cheap re-weight is wanted deliberately, it has to be *said*
  in the result (the sampling temperature belongs on `ConformerEnsemble` beside the weighting one).

---

## `ConformerEnsemble.lowest` is `conformers[0]`, and nothing on this side checks that it is lowest

- **Severity**: low (latent — the current server does sort)
- **Location**: `/home/user/Chemclaw3/src/chemclaw/connectors/calc/compose.py:400-409`
  (`conformer_ensemble`) and `/home/user/Chemclaw3/src/chemclaw/connectors/calc/compose.py:605-613`
  (`_species_energy`, `structure = ensemble.lowest`);
  `/home/user/Chemclaw3/src/chemclaw/connectors/calc/activities.py:162-165`
  (`ensemble.conformers[0].population`)
- **Trigger**: an `EnsemblePayload` whose `members` are not already in ascending energy order —
  which the schema permits (`EnsembleMember` has no ordering constraint and nothing here sorts).
- **Consequence**: `ConformerEnsemble.lowest` returns the *first* member, not the lowest-energy one;
  `ensemble_from_members` truncates to `crest_max_members` in the same unsorted order, so the true
  lowest can be dropped entirely; and the job summary quotes the first member's population as "the
  lowest". `_species_energy` at `thorough` then optimizes the wrong conformer and reports its ΔG.
  Reproduced by handing `compose.conformer_ensemble` a highest-first payload:

  ```
  relative_kcal per conformer, in returned order: [2.51, 1.255, 0.0]
  conformers[0].relative_kcal = 2.51   /  min relative_kcal = 0.0
  ensemble.lowest IS the lowest-energy structure: False
  job summary would quote population: 1%  (the lowest conformer's is 88%)
  ```

  The invariant *is* currently upheld, but only in the other repository:
  `Chemclaw3-mcp:.../engine/crest_cli.py::run` ends with
  `return sorted(paired, key=lambda member: member.energy_hartree)` and a comment naming this exact
  hazard by name ("Chemclaw3's `ConformerEnsemble.lowest` is `conformers[0]` … would silently drop
  the lowest conformer"). So this is a cross-repository invariant with a guard on one side and a
  docstring assertion on the other. That it is worth closing is argued by this module itself:
  `compose.interaction` reads the **same** `EnsemblePayload` shape one function away and refuses to
  trust the order — `best = min(modes.members, key=lambda member: member.energy_hartree)`.
- **Fix**: one line, in the place that already knows the energies —
  `members = sorted(payload.members, key=lambda m: m.energy_hartree)` at the top of
  `ensemble_from_members` — which makes `lowest`'s docstring true by construction and makes the
  `max_members` truncation keep the members it claims to. `tests/test_upstream_surface.py` is the
  established home for the alternative (assert the shape upstream never promised); this one is
  cheaper to just enforce.

---

## Isotope labels are erased at the balance gate and in the structure identity

- **Severity**: low
- **Location**: `/home/user/Chemclaw3/src/chemclaw/connectors/calc/compose.py:508-515`
  (`_composition`, `Counter(atom.GetSymbol() …)`) and `518-557` (`check_balance`); interacting with
  `science/calc/models.py::Structure`, whose `structure_id` hashes `elements` (atomic numbers) only
- **Trigger**: any isotopically labelled SMILES — a KIE question (`[2H]`), a tracer study (`[13C]`).
- **Consequence**: two of them.
  1. `check_balance(["[2H]O[2H]"], ["O"])` **passes** as atom-balanced, so the gate that exists to
     stop "a number that is meaningless rather than merely imprecise" admits an isotopic-exchange
     equation. `GetSymbol()` returns `"H"` for deuterium.
  2. `[13CH4]` and `C` embed to identical `elements`/`positions` and therefore share one
     `structure_id` — measured `st_c6319ec4ee1d1d44` for both — so they share one cached relaxation
     and one cached Hessian. Since `science/calc/thermo.py::_atomic_masses` reads
     `GetAtomicWeight(<atomic number>)`, the ZPE, enthalpy and entropy reported for the labelled
     molecule are the unlabelled molecule's, exactly, with no flag.
- **Evidence** (`scratchpad/probe_isotope.py`):

  ```
  'C' -> 'C'  vs  '[13CH4]' -> '[13CH4]'
     elements equal: True  positions equal: True
     structure_id: st_c6319ec4ee1d1d44 == st_c6319ec4ee1d1d44 -> True
     Structure fields: ['charge', 'elements', 'multiplicity', 'origin', 'positions', 'smiles']
  check_balance([2H]O[2H] -> O): PASSES (treated as balanced)
  ```

- **Fix**: the honest, cheap move is refusal rather than support, since the pipeline has no mass
  channel at all. Add an isotope check beside the balance check in `compose.py` — reject any species
  whose molecule has an atom with `GetIsotope() != 0`, with a message saying isotopic substitution is
  not modelled (`Structure` carries atomic numbers, and the RRHO arithmetic uses standard weights).
  Supporting it properly is a `Structure` field plus a mass override in `_atomic_masses` plus a
  re-addressing of every geometry — a separate decision.

---

## Also checked, and clean

Recording these so the absence of a finding is a result rather than a gap.

- **`compose.py` reaction arithmetic.** Signs and units check out against the primary quantities:
  `ΔE/ΔH/ΔG` are products-minus-reactants with `HARTREE_TO_KCAL` applied once (`_difference`,
  `delta_e`); the conformational term is added to G only and converted the right way
  (`+ ensemble_correction / HARTREE_TO_KCAL`, and `ensemble_correction_kcal` is
  `-T·S_conf/1000`, i.e. already negative — so it lowers G, correctly);
  `interaction_energy_kcal = (complex − Σ monomers)` is negative for binding; `solvent_comparison`
  sorts ascending on ΔG-else-ΔE so `effects[0]` is genuinely the most product-favouring and
  `spread = ranking(last) − ranking(first)` is non-negative. Mixed ΔG/ΔE ranking is unreachable
  because `unstated` is a function of the shared `symmetry_numbers` map.
- **The saddle-point refinement loop** (`relax_to_minimum`): bounded by
  `xtb_minimum_refinement_attempts + 1` with `ge=0` on the setting, returns the last result intact
  when it does not settle, and `cached` is the AND across every iteration.
- **`_ordered` / `interaction`**: canonicalises then sorts, so A-with-B and B-with-A are one entry;
  `interaction` picks the best binding mode by `min(...)` rather than by position.
- **`remote.py`**: the `connected` flag correctly separates a connection failure from the caller's
  own exception travelling back through the `yield`, so a Postgres error inside `cached_remote` is
  not relabelled retryable; `_call`'s JSON-RPC code split is right; the epoch is folded into
  `params_hash` on both the remote path (`remote_key`) and the local one (`CalculationKey.build`),
  with no collision between the two spellings.
- **`fetch_artifact`**: the `max(1, min(...))` guard does prevent a negative `max_chars` slicing
  from the end (the comment's claim is true), and the UTF-8 decode really does refuse a `.npy`
  (magic byte `0x93` is an invalid UTF-8 lead byte).
- **Manifest partitions**: `calc` 10 state-changing + 5 read-only = the 15 tools the server
  actually decorates; `bo` 2 + 3 = 5; `molfp` 2; `rxnfp` 1. No tool is served-but-undeclared or
  declared-but-unclassified in any of the seven manifests. `bo`'s classification of
  `campaign_progress` as read-only matches the body (pure arithmetic, no featurisation).
- **`bo` durable loop**: `continue_as_new` fires after a completed round only, carries the full
  `CampaignCarryOver`, and keeps the workflow id so `record_campaign_run`'s idempotency key is
  stable across the carry-over; `best_of` cannot see an empty history because `CampaignSpec.n_initial`
  has `ge=MIN_SEED_OBSERVATIONS`; a multi-objective spec is refused at launch, so `best_of`'s
  multi-objective raise is unreachable from the durable path.
- **`bo` inline count handling**: `count=0` / `count=-3` are refused by BoFire with a plain
  `ValueError` the connector forwards ("Candidate_count has to be at least 1 but got 0") — measured,
  not assumed.
- **`_only_matching`'s timeout**: RDKit releases the GIL, so `to_thread` + `wait_for` does bound the
  wait as claimed — measured 99 event-loop ticks/s during a saturating match, worst stall 11 ms.
  (The worker thread does keep running past the timeout; that is the documented `to_thread`
  behaviour and not a wrong answer.)
- **`_poll_nextflow`'s error budget** absorbs `hpc_poll_max_consecutive_errors − 1` failures and
  raises on the Nth, where the docstring says "up to N in a row" — an off-by-one in the prose, not
  in a number anyone reads.
