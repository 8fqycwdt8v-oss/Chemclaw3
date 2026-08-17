# Verification — `connectors-bundles--correctness.md`, lens: does it reproduce?

Two findings in scope (`high`); the other four are medium/low and were not examined.

Everything below was re-derived from source. Chemclaw3-mcp was read at the same commit the reporter
names (`git -C /workspace/chemclaw3-mcp rev-parse HEAD` → `92170117d8f25cc5588ad2a014c98a1bc14cdb04`),
and — the part that decided the first verdict — its xTB engine was **run**: `tblite` is installed in
`/workspace/chemclaw3-mcp/.venv`, so the Fukui numbers below are real GFN2-xTB output, not a
stand-in's arithmetic. `git status --porcelain` shows no modification under `src/` in this checkout;
every line number cited by the reporter is current.

---

## `predict_site_reactivity` re-ranks a set the server already truncated by the *other* mode

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium

- **What I did**

  1. Read both sides. The client (`src/chemclaw/connectors/calc/server/tools.py:782-797`) does send
     only `{"smiles": smiles}`, and the server body
     (`Chemclaw3-mcp servers/calc/src/chemclaw_mcp_calc/tools.py:328-333`) is
     `sites[:top_n or settings.xtb_fukui_top_n]` after
     `compute_fukui(...)` has already `sites.sort(key=<mode field>, reverse=True)`
     (`engine/xtb_props.py:255-256`), with `xtb_fukui_top_n: int = 15` (`engine/config.py:77`) and
     `fukui_inputs` carrying neither `mode` nor `top_n` into the key (`engine/xtb_props.py:297-304`).
     So the *mechanism* the finding describes is exactly what the two repositories do.

  2. Ran the real engine rather than a stand-in. `/tmp/v_fukui_real.py` and `/tmp/v_fukui_batch.py`
     compute the full 3-SCF Fukui set with `tblite` and ask: is the true `f_plus` maximum inside the
     15 atoms that top the `f_minus` ranking?

     Ibuprofen — the molecule the finding names — printed:

     ```
     total_atoms: 33 sites: 33
     true nucleophilic winner : idx 11 O f+=0.1393
     winner after truncation  : idx 11 O f+=0.1393
     WRONG ANSWER: False
     rank of true f+ winner in f- ordering: 0 (0-based; cut at 15)
     pearson corr(f-, f+): 0.8006
     ```

     Over 22 real molecules with more than 15 atoms (`/tmp/v_fukui_batch.py`, `/tmp/v_fukui_big.py`
     — ibuprofen, aspirin, paracetamol, caffeine, nicotine, 4-nitroanisole, benzamide,
     acetophenone, styrene oxide, N,N-dimethylaniline, methyl benzoate, indole-3-carbaldehyde,
     diethyl malonate, sulfanilamide, diclofenac, warfarin, chlorpromazine, nitrofurantoin,
     sulfamethoxazole, phenytoin, furosemide, naproxen):

     ```
     molecules >15 atoms: 14; nucleophilic winner wrong: 0; radical wrong: 0
     mean corr(f-, f+) = +0.667  min=+0.300 max=+0.904        [first batch]
     diclofenac  heavy=19 total=30  nuc true idx0(O,0.0771) got idx13(Cl,0.0759) WRONG  true-nuc-top5 kept: 3/5
     warfarin/chlorpromazine/nitrofurantoin/sulfamethoxazole/phenytoin/furosemide/naproxen: ok
     furosemide  heavy=21 total=32  ... ok  true-nuc-top5 kept: 2/5
     ```

     **1 wrong top site in 22.** `corr(f⁻, f⁺)` was **positive in every single molecule**
     (+0.30 … +0.90).

  3. Drove this repo's real `predict_site_reactivity` end to end (`/tmp/v_e2e_fukui.py`): my own
     fake session reproducing the server body (sort-by-mode, then `[:15]`), serving the **real**
     diclofenac Fukui array, with `chemclaw.connectors.calc.remote.calc_session` patched and an
     `InMemoryStore`:

     ```
     molecule O=C(O)Cc1ccccc1Nc1c(Cl)cccc1Cl  total_atoms=30
       electrophilic  -> top idx 18 (Cl) f-=0.1069 f+=0.0754 f0=0.0911; sites returned 15
       nucleophilic   -> top idx 13 (Cl) f-=0.0998 f+=0.0759 f0=0.0878; sites returned 15
       radical        -> top idx 18 (Cl) f-=0.1069 f+=0.0754 f0=0.0911; sites returned 15
       arguments this repo sent: [{'smiles': 'O=C(O)Cc1ccccc1Nc1c(Cl)cccc1Cl'}]
       TRUE f+ maximum: idx 0 (O) f+=0.0771
       top_n=100 asked -> 15 of 30 sites returned
     ```

     One entry in the sent-arguments list across four calls, so the cache-poisoning half is real
     too: the truncated payload is stored once under the mode-independent key and every later mode
     is re-ranked out of it.

- **Why**

  The plumbing claim is CONFIRMED and I even found a real molecule the reporter did not (diclofenac
  genuinely returns the wrong `f⁺` top site). What is OVERSTATED is everything the *severity* rests
  on:

  - **The named reproduction is false.** Ibuprofen, run with the actual engine, gives the *correct*
    nucleophilic winner — its true `f⁺` maximum is also the **top** `f⁻` site, rank 0 of 33. The
    reporter's `f+=0.4242 / f+=0.9697` are `14/33` and `32/33`: a linear ramp from a stand-in that
    was built anti-correlated, so the measurement measured the scaffolding. This is exactly the
    "reproduction that only works with the reporter's exact scaffolding" case.
  - **The stated mechanism is backwards.** "f⁻/f⁺ are near-anticorrelated by construction" is
    contradicted by every molecule I ran: r = +0.30 to +0.90, mean +0.67. Physically obvious in
    hindsight — hydrogens carry small values of *both* indices and heteroatoms large values of
    both, so the `f⁻` top-15 is essentially "the heavy atoms", which is also where the `f⁺` maximum
    lives. Hence "systematically among the atoms that were discarded" is false, and so is "every
    drug-sized molecule": a molecule needs **more than ~15 heavy atoms** before the cut can bite at
    all, which ibuprofen (13 heavy) never does.
  - The one real failure I found is between two near-degenerate sites (0.0771 vs 0.0759, 1.6 %), so
    even the confirmed instance is not the "confidently wrong regiochemistry answer" the finding
    advertises.

  What does survive, and is worth fixing: the returned *ranking* for a non-electrophilic mode is
  drawn from an electrophilically-selected pool — measured, only 2 of the true nucleophilic top-5
  survive for furosemide, 3 of 5 for diclofenac — and `top_n`'s documented "pass a larger number to
  see the whole molecule" is a hard 15-site ceiling once a row is written (`top_n=100 -> 15 of 30`),
  because the client slices a payload the server already sliced. The comment at
  `tools.py:790-792` ("the row holds every atom") is plainly wrong and should go. That is a
  medium: a silently incomplete ranking and a documented argument that does not work, not a
  systematically wrong answer.

---

## `parse_qm_output` silently truncates a scientific-notation energy instead of raising

- **Verdict**: CONFIRMED
- **Severity I would assign**: medium

- **What I did**

  Wrote my own table-driven probe (`/tmp/v_parse.py`) calling the real activity function
  `chemclaw.connectors.qm.activities.parse_qm_output` — no scaffolding beyond a `QMJobInput`:

  ```
  callable used: <function parse_qm_output at 0x7fb5edd16c00>
  mock shape               -> energy=-12.3 converged=True
  sci notation E+02        -> energy=-1.5423156 converged=True
  sci notation e-01        -> energy=-1.5423156 converged=True
  integer energy           -> RAISED ValueError: unparseable QM output: 'energy=-154 converged=True'
  fortran D exponent       -> energy=-1.542315 converged=True
  SCF trace 2 cycles       -> energy=-153.1 converged=False
  ```

  Every one of the reporter's five rows reproduces, on my inputs, to the digit. `-1.5423156E+02`
  really does come back as `-1.5423156` — a factor of 100 — with `converged=True` and no exception;
  the two-cycle log really does take cycle 1 and report `converged=False`; an integer-valued energy
  really does raise after the run.

  I then checked the consequence chain the finding asserts, since a wrong number only matters if
  nothing downstream catches it:

  - `activities.py:45-46` are the two regexes as quoted, `search`, mantissa-only, unanchored.
  - `QMJobResult.total_energy_hartree` is a bare `float` with no bound (`specs.py:98-110`).
  - `persist_qm_result` (`activities.py:206-243`) writes it straight into the calculation store with
    no sanity check.
  - `qm_energy_estimate` (`connectors/qm/knowledge.py:20-58`) sets `in_domain=result.converged` and
    `uncertainty=None`, so a 100×-wrong energy is published through the PR-gate flagged in-domain.
  - The mock path cannot expose it: `_MOCK_OUTPUT_TEMPLATE` is `"energy={energy:.6f} …"` and
    `poll_hpc_status`'s fake energy is `-1.0 * (hex % 1000) / 10.0` — always a plain decimal.
  - `tests/test_qm_workflow.py:161` only asserts the all-garbage case.

- **Why**

  The code does what the finding says, on the arguments it says, and the docstring at
  `activities.py:149-150` ("raises on unparseable output so a corrupt result never silently
  becomes …") is inverted relative to the behaviour — a partial match is not a refusal, and the
  regex is engineered to produce one. Nothing between the parse and the persisted, PR-gated,
  `in_domain=True` record inspects the magnitude. I would add one thing the reporter missed: the
  SCF-trace case is the more likely of the two, because it needs no exotic formatting at all — any
  pipeline that concatenates iterations into `qm_output.txt` yields the *first* line, and the
  `converged` flag it pairs with the energy comes from a different `search` entirely, so the two
  fields can be read out of different cycles.

  I mark severity medium rather than high only on reachability: the trigger is
  `hpc_launch_interface="nextflow"`, and no cluster exists — the output shape is set by a Nextflow
  pipeline that is not in this repository and has not been written, whose contract
  (`hpc/nextflow.py:143-145`) is the plain `energy=… converged=…` form the parser handles. The
  finding is honest about this. The defect is latent-but-certain: if that pipeline ever emits an
  exponent or more than one cycle, the failure is silent and permanent (D-011 never recomputes, and
  `durable/retention.py` never prunes `calculation_results`). It is cheap to fix now and
  uncorrectable later, which is why it should still be fixed.
