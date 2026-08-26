"""What this repository still decides about a calculation — orchestration, not physics.

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).

**The engine's own knobs are not here and must not come back.** After
`D-2026-08-16-the-physics-leaves-the-cache-stays` the binaries, the convergence thresholds, the
finite-difference step and the pKa/solubility calibrations belong to `Chemclaw3-mcp`'s
`servers/calc`, which reads them under the *same* `CHEMCLAW_` prefix from its own pod. Twenty-four
such fields were left declared here when the physics left, and the failure was silent in the worst
way available: an operator setting `CHEMCLAW_XTB_OPT_MAX_STEPS` on this deployment changed
nothing at all, while the identically-named setting on the server was what actually decided the
calculation. `tests/test_config.py` now fails on a calculator field with no reader, which is what
makes that checkable rather than remembered.
"""

from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class PkaCalibration(BaseModel):
    """The linear map from a computed deprotonation free energy to a pKa, and its fitted domain.

    **One model rather than five flat fields, because these five numbers are one measurement.** A
    slope moved without its intercept is a different calibration silently claiming to be this one,
    and an uncertainty or a domain left behind describes a fit that no longer exists. Overridden
    together as JSON (`CHEMCLAW_PKA_ENSEMBLE_ACID='{"slope": ..., "intercept": ...}'`) or not at
    all.

    Unlike the calculator knobs this module refuses to hold, this one is genuinely **this**
    repository's: `connectors/calc/compose.py::microstate_pka` is the composite that reads it, and
    the pKa it produces is arithmetic performed here over ensembles the server sampled. Nothing on
    `Chemclaw3-mcp` can see it, and its own `predict_pka` calibration — a different pipeline, fitted
    separately — is unaffected by anything set here.
    """

    slope: float
    intercept: float
    # One standard error of the fit, reported beside every prediction. A semiempirical pKa is for
    # ranking related compounds; the number without its spread invites a decision it cannot carry.
    uncertainty: float
    # The experimental pKa span the fit was made over. A prediction outside it is extrapolation and
    # says so — the map is linear and the physics behind it is not, so the residual off the end of
    # the reference set is unknown rather than merely larger.
    fitted_from: float
    fitted_to: float
    # The CREST search depth the reference set was measured at. It belongs to the fit for the same
    # reason the solvent does: a deeper search finds lower members on both sides of the equilibrium,
    # so it moves the free-energy difference the slope was fitted against. A caller who pays for a
    # better ensemble gets the better ensemble and a warning that the *mapping* is the quick one's.
    fitted_effort: Literal["quick", "normal", "extensive"] = "quick"


class CalculatorSettings(BaseSettings):
    """How this repository orchestrates, budgets and caches a calculation it no longer runs.

    Grouped because these knobs decide what the *orchestration* does: how long a durable job may
    run, how often it heartbeats, how many points a composed scan takes, where the calculation
    server is and how long to wait for it.

    **None of them enters a cache key**, and that is the reversal worth stating rather than
    discovering. They used to: the key was built here, from these values. It is now derived by the
    server from the server's own settings and transported as four fields, so changing anything here
    invalidates nothing and recomputes nothing. A knob that a reader believes is "a deliberate
    recompute" but that no key can see is the most expensive kind of stale comment, which is why
    this paragraph replaced it.
    """

    # How many media a solvent screen evaluates at once (`connectors/calc/compose.py`).
    #
    # **Default 1 — today's behaviour exactly — and that is a measurement waiting to be taken
    # rather than caution.** A screen is one reaction per solvent and they genuinely serialize, so
    # the latency is there to win; the reason not to take it by default is directly above. With
    # `xtb_cli_threads = 0` the binary "uses the machine", which is right for a worker pod running
    # one job at a time and wrong the moment two branches run at once — six solvents would be six
    # processes each sized for the whole box, which is the oversubscription that comment warns
    # about. Raising this is therefore paired with pinning `xtb_cli_threads`, and which pair wins
    # depends on the pod's cores. So the knob exists, costs nothing at 1, and lets a deployment
    # with headroom answer it by measuring instead of by argument.
    calc_screen_max_parallel: int = Field(default=1, ge=1)
    crest_effort: Literal["quick", "normal", "extensive"] = "quick"
    crest_max_members: int = 20
    # How many members of an ensemble get their own optimization and Hessian when a caller asks for
    # free-energy-weighted populations. Five, because the cost is linear in this and a Hessian is
    # the most expensive thing after the search itself — measured, a 33-atom conformer search is
    # ~19 minutes and there were 13 members, so refining all of them is the search again several
    # times over. The result carries `refined_population_covered` so a truncation says so.
    ensemble_refine_top_n: int = Field(default=5, ge=1)
    # The solvent every CREST search behind a `microstate_pka` runs in. Water, because the pKa a
    # chemist means is the aqueous one and both calibrations below were fitted there; a caller may
    # ask for another medium and gets the ensembles and the free energy, with the calibration
    # warned about rather than silently reapplied.
    pka_ensemble_solvent: str = "water"
    # **The two calibrations, each fitted through the exact pipeline that reads it** — a CREST
    # conformer search of the neutral, a CREST `--deprotonate`/`--protonate` microstate search, and
    # the macrostate free energy of each side. Refitting is a measurement, not a tuning: see
    # `docs/decisions/D-2026-08-26-a-pka-is-a-macrostate-not-a-microstate.md` for the reference set
    # and the statistics these numbers came from.
    #
    # Acid: 19 neutral O-H/S-H acids spanning pKa 0.66-15.9. R^2 0.911, RMSE 1.31, Spearman 0.940,
    # worst residual 2.54 (2,2,2-trifluoroethanol).
    pka_ensemble_acid: PkaCalibration = PkaCalibration(
        slope=0.31221, intercept=-32.98637, uncertainty=1.31, fitted_from=0.66, fitted_to=15.9
    )
    # Base: 12 aromatic/aryl nitrogen bases spanning pKaH 0.72-9.11. R^2 0.798, RMSE 1.05,
    # Spearman 0.888, worst residual 2.47 (2-chloropyridine, where the ortho chlorine's steric and
    # inductive effect on the cation is what a continuum sees least well).
    pka_ensemble_base: PkaCalibration = PkaCalibration(
        slope=0.32316, intercept=-31.71601, uncertainty=1.05, fitted_from=0.72, fitted_to=9.11
    )
    # How many distinct species one ranking may cover — tautomers, microstates, stereoisomers.
    # RDKit will happily enumerate twenty tautomers of a purine; each one is a separate CREST
    # search, so this is the difference between a question and a project.
    species_ranking_max: int = Field(default=8, ge=1)
    # The ceiling `science/calc/budget.py` refuses above, counted in remote primitives rather than
    # in seconds — the call count is what a composite knows before it starts, and duration depends
    # on a molecule this layer cannot see. 120 is roughly six species refined over five members
    # each; past that the request wants narrowing rather than a longer timeout.
    calc_max_primitive_calls: int = Field(default=120, ge=1)
    # xTB semiempirical calculator (plan step 1c.2). Method is the GFN parametrization
    # (latest: GFN2-xTB). `xtb_embed_seed` fixes RDKit 3D embedding so results are
    # reproducible; it is part of the cache key so changing it recomputes.
    xtb_method: str = "GFN2-xTB"
    # `xtb_geometry_decimals` was here and is now `science.calc.models._GEOMETRY_DECIMALS`. It
    # rounds the coordinates the *server* derives `input_hash` from, so a deployment that set it
    # did not re-address a local cache as this comment used to claim — it missed every remote
    # calculation forever, silently, and diverged from every other deployment.
    # Default number of atoms a site-reactivity ranking reports. Enough to see the
    # ordering of a ring plus its substituents without flooding the agent's context.
    xtb_fukui_top_n: int = 15

    # Start-to-close budget for one durable xTB job activity. Four hours, because the
    # workload is drug-sized molecules: one 76-atom species takes ~5 minutes to optimize
    # and take a Hessian, so a multi-species reaction at 100+ atoms is genuinely hours.
    # The store makes a retry cheap rather than a restart from zero, and the heartbeat
    # below — not this timeout — is what detects a dead worker.
    xtb_job_timeout_seconds: int = 14400
    # How long the durable job may go without a heartbeat before Temporal declares the
    # worker dead and retries. Comfortably longer than the slowest single unit of work
    # (one species' optimization plus Hessian on a large molecule), short enough that a
    # crash is noticed in minutes rather than at the start-to-close budget.
    xtb_job_heartbeat_timeout_seconds: int = 600
    # Thermochemistry conditions. 298.15 K and 1 atm are the reference state every
    # tabulated thermodynamic quantity is quoted at.
    xtb_thermo_temperature_k: float = 298.15
    xtb_thermo_pressure_pa: float = 101325.0
    # Quasi-RRHO damping frequency (cm^-1, Grimme 2012): below it a vibration is treated
    # as a free rotor for the entropy, because a harmonic oscillator's entropy diverges
    # as the frequency goes to zero and low modes are exactly where the harmonic
    # approximation fails. 25 cm^-1 is the published value and what xtb itself uses.
    xtb_rrho_cutoff_cm: float = 25.0
    # A negative Hessian eigenvalue below this magnitude (cm^-1) is numerical noise from
    # the finite differences, not a real imaginary mode. Above it the geometry is a
    # saddle point and the thermochemistry says so.
    xtb_imaginary_threshold_cm: float = 25.0
    # Reported uncertainty on a semiempirical reaction free energy, in kcal/mol.
    # Attached to every result, like `pka_uncertainty` — GFN2 reaction energies are
    # useful for comparison and poor as absolute numbers.
    xtb_reaction_uncertainty_kcal: float = 3.0
    # Maximum number of points in a relaxed scan. Each point is a constrained geometry
    # optimization on the calculation server, so this bounds the cost of a single agent call —
    # and it is read *here* because the scan is composed here, one remote point at a time.
    xtb_scan_max_points: int = 24
    # A rotational profile's coarse step, in degrees. 30 covers a full turn in twelve constrained
    # optimizations and resolves every well of an ordinary torsion; the barrier *height* it gives
    # is then refined, because a coarse grid steps over a maximum rather than landing on it —
    # which is precisely what `skills/conformational-analysis` asks a human to notice and rescan.
    xtb_rotation_step_degrees: float = Field(default=30.0, gt=0.0, le=120.0)
    # How many extra points each maximum is resolved with, spread across the two coarse steps
    # around it. Four puts a point every fifth of a step there, which is where the barrier is read.
    xtb_rotation_refine_points: int = Field(default=4, ge=0)
    # Two released minima closer than this (degrees) are the same rotamer. Well below the smallest
    # real separation between torsional minima (60 degrees for a three-fold rotor) and well above
    # the spread of an optimizer settling into one basin from two neighbouring start points.
    xtb_rotation_merge_degrees: float = Field(default=15.0, gt=0.0, lt=60.0)
    # How far out of line one step of a torsion profile has to be, as a multiple of that profile's
    # own typical step, before it is reported as a point that relaxed into a different basin.
    #
    # **A ratio and not a kcal/mol threshold, because measurement said so.** This was
    # `xtb_reaction_uncertainty_kcal` (3.0), which fires on any barrier steep enough to matter: on
    # the live GFN2 server N,N-dimethylacetamide steps 8.8 kcal/mol between two 30-degree points
    # while climbing an ordinary 18 kcal/mol amide barrier, and was warned about — so the check
    # fired on precisely the hindered rotations the capability exists for and stayed quiet on the
    # freely-rotating ones. A discontinuity is a step *out of line with its neighbours*, not a
    # large step. Calibrated against three measured smooth profiles, whose largest step was 3.5x,
    # 2.7x and 2.5x their own median; 4.0 clears all three.
    xtb_rotation_discontinuity_ratio: float = Field(default=4.0, gt=1.0)
    # How many times a geometry that lands on a saddle point may be displaced along its
    # imaginary mode and re-optimized, and how far (Angstrom, the largest atom's motion).
    # One attempt clears the ordinary case — a force field's eclipsed methyl held by
    # symmetry through a Cartesian optimization; more than two means the structure is
    # saying something real that another kick will not fix. Each attempt costs a full
    # optimization *and* a full Hessian, which on a 100-atom substrate is tens of
    # minutes — so on large molecules the refinement, when it triggers, dominates the
    # job. Measured: sildenafil (63 atoms) does not reach a clean minimum on the first
    # pass, so this is not a rare path at drug size.
    xtb_minimum_refinement_attempts: int = Field(default=2, ge=0)
    xtb_imaginary_kick_angstrom: float = 0.3
    # Default number of IR bands a thermochemistry result reports, strongest first.
    # A measured spectrum is compared on its strong bands; the weak modes between them
    # carry no information for that comparison and cost context.
    xtb_ir_bands_top_n: int = 12

    # There were two `calibration_conformal_*` settings here, in the present tense, describing how
    # split-conformal intervals "replace the reported constant above whenever there is enough
    # evidence". Nothing read either of them — no module in `src/`, no test, no chart value — and
    # `science/calc/uncertainty.conformal_uncertainty`, the function taking exactly those two
    # parameters, has no caller. The paragraph described a mechanism the deployment did not have,
    # while `.env.example` published the knobs for an operator to set and see nothing change.
    #
    # Deleted rather than left: a setting with no reader is configuration in appearance only, and
    # this repository has fixed that class of defect often enough to name it. The *function* stays,
    # tested and correct; wiring it to a predictor is a capability decision with its own backlog
    # row, and these two come back with the caller that needs them (their values were argued for —
    # 0.9 because the guarantee needs `ceil((n+1)·coverage) ≤ n` samples to exist at all, 20 because
    # nine residuals give a valid interval one unusual compound sets — and that is in `git log`).

    # logD (calc.logd, D-092): the working pH used when a caller does not name one.
    # 7.4 (physiological pH) is the conventional analytical-chemistry default.
    logd_default_ph: float = 7.4
    # The ionised fraction of the *one* site `calc.pka` reports, at or below which further
    # unmodelled sites of the same kind can still be dismissed; above it `calc.logd` refuses
    # rather than report a single-equilibrium number for a polyprotic molecule.
    #
    # The bound is arithmetic, not taste. `calc.pka` reports the *most* ionisable site, so with
    # r = f/(1-f) its ionisation ratio, every other site's is at most r, and the species sum the
    # single term omits is bounded by the geometric series: the neglected shift is at most
    # -log10(1 - r**2). At f = 0.05 that is 0.0012 log units, three orders below the +/-1.6 the
    # result already carries. The bound diverges as f approaches 0.5, where the unseen sites can
    # contribute as much as the modelled one (~0.3 log units for a diprotic acid) — which is why
    # this is a small number and not "is the pH past the pKa".
    logd_negligible_ionised_fraction: float = Field(default=0.05, gt=0, lt=0.5)

    # Reaction energetics (calc.reaction, D-098): a reaction electronic energy at or
    # below this threshold (kcal/mol, negative = exothermic) is flagged for thermal-hazard
    # attention. -20 kcal/mol is a conservative, commonly cited screening threshold for a
    # "strongly exothermic" flag; advisory only, like the structural hazard screen (D-080).
    reaction_energy_exotherm_threshold_kcal: float = -20.0

    # **Where the physics runs.** The engines moved to `Chemclaw3-mcp`'s `servers/calc`
    # (`D-2026-08-16-the-physics-leaves-the-cache-stays`); this repository keeps the D-011 cache,
    # the calibration ledger and the orchestration, and reaches the compute over MCP.
    #
    # Not a connector bundle, deliberately. A bundle's manifest is what the *agent's* tool surface
    # is built from, and this server is never on it — the agent still calls this repository's own
    # `calc` tools, which now happen to be cache-and-compose wrappers. Putting the server's manifest
    # on `connectors_dirs` would put seventeen orchestrator-facing primitives into a prompt and let
    # a partial port win the `calc` name collision, taking six read tools and every durable job off
    # the surface with no error. So the address is plain configuration read by one client module.
    calc_server_url: str = "http://127.0.0.1:8860/mcp"
    # The environment variable holding the bearer the server enforces on `/mcp`. Named rather than
    # carried, read per request, and the same name the server itself reads — the shape every
    # out-of-release connector uses (`D-2026-08-09-a-connector-we-do-not-run`). A missing value is a
    # refused call, not an open one.
    calc_server_token_env: str = "CHEMCLAW_CALC_TOKEN"
    # How long one remote calculation may take. Far above a connector's 30 s because these are the
    # calculations themselves: a Hessian on a large substrate is minutes, and the fleet's own
    # guidance now says duration is not the property it promises. A durable job's activity bounds
    # the same wait again with its own timeout and heartbeat.
    calc_server_timeout_seconds: float = Field(default=900.0, gt=0)
    # **The same bound for a CREST search, which is a different order of cost.** 900 s was
    # unreachable while the binary shipped in no image: every sampling call refused in
    # milliseconds. It ships now (`D-2026-08-26-a-sampler-nobody-ships-is-a-refusal-with-a-manual`)
    # and the numbers no longer fit — this repository's own measurement of a 33-atom conformer
    # search is **1142 s**, past the bound above, while the server allows one 14400 s
    # (`CHEMCLAW_CREST_TIMEOUT_SECONDS`). A client bound shorter than the server's does not save
    # anything: the server keeps computing, the answer is discarded, and the caller is told the
    # service timed out. Matched to the server's own ceiling so the *server* is what bounds a
    # search, with `crest_timeout_seconds` the one number to change.
    calc_sampling_timeout_seconds: float = Field(default=14400.0, gt=0)
    # The molecule `connectors/calc/remote.py::remote_version` derives a key *for* when it asks the
    # server what version a calculator is on. `calculation_key` answers an identity, and an identity
    # is of something — but the `calc_version` it reports is a property of the programs and the
    # calibration behind the calculator, not of the molecule, so any parseable input gives the same
    # answer. Acetic acid because it is the one molecule every calibrated calculator here can
    # enumerate: it has an acidic O-H, so the pKa predictor's acid branch is well defined, and ESOL
    # takes anything. Configurable rather than inlined so it is one visible fact instead of a
    # literal repeated at each call site — and so a deployment whose calibrated set changes can move
    # it without a code change.
    calc_version_probe_smiles: str = "CC(=O)O"
