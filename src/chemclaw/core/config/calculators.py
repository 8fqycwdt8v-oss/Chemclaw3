"""What this repository still decides about a calculation: orchestration, not physics.

The engine's own knobs (binaries, convergence thresholds, calibrations of the physics) belong to
`Chemclaw3-mcp`'s `servers/calc`, which reads them under the same `CHEMCLAW_` prefix in its own pod;
declaring them here would be a setting that changes nothing. `tests/test_config.py` fails on a
calculator field with no reader.
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
    # One standard error of the fit, reported beside every prediction.
    uncertainty: float
    # The experimental pKa span the fit covers; a prediction outside it is reported as
    # extrapolation.
    fitted_from: float
    fitted_to: float
    # The CREST search depth the reference set was measured at; depth moves the fitted free-energy
    # difference, so a deeper search is warned that the mapping is the quick one's.
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

    # Media a solvent screen evaluates at once (`connectors/calc/compose.py`). Default 1: with
    # `xtb_cli_threads = 0` each process sizes itself to the whole machine, so raising this must be
    # paired with pinning `xtb_cli_threads`.
    calc_screen_max_parallel: int = Field(default=1, ge=1)
    crest_effort: Literal["quick", "normal", "extensive"] = "quick"
    crest_max_members: int = 20
    # Ensemble members that get their own optimization and Hessian for free-energy-weighted
    # populations; cost is linear in it. `refined_population_covered` reports the truncation.
    ensemble_refine_top_n: int = Field(default=5, ge=1)
    # Solvent of every CREST search behind `microstate_pka`. Water, where both calibrations were
    # fitted; another medium gets a calibration warning rather than silent reuse.
    pka_ensemble_solvent: str = "water"
    # The two calibrations, each fitted through the exact pipeline that reads it (neutral conformer
    # search, microstate search, macrostate free energies); refitting is a measurement. Reference
    # set and statistics: `docs/decisions/D-2026-08-26-a-pka-is-a-macrostate-not-a-microstate.md`.
    # Acid: 19 neutral O-H/S-H acids, pKa 0.66-15.9.
    pka_ensemble_acid: PkaCalibration = PkaCalibration(
        slope=0.31221, intercept=-32.98637, uncertainty=1.31, fitted_from=0.66, fitted_to=15.9
    )
    # Base: 12 aromatic/aryl nitrogen bases, pKaH 0.72-9.11.
    pka_ensemble_base: PkaCalibration = PkaCalibration(
        slope=0.32316, intercept=-31.71601, uncertainty=1.05, fitted_from=0.72, fitted_to=9.11
    )
    # Distinct species one ranking may cover (tautomers, microstates, stereoisomers); each is a
    # separate CREST search.
    species_ranking_max: int = Field(default=8, ge=1)
    # Ceiling `science/calc/budget.py` refuses above, in remote primitive calls, which a composite
    # knows before it starts (unlike duration). 120 is about six species refined over five members.
    calc_max_primitive_calls: int = Field(default=120, ge=1)
    # Atom ceiling for a Hessian (6N single points), checked before dispatch so the refusal names
    # this system's alternatives (cheaper level, smaller molecule). The server's own ceiling stays
    # authoritative; this defaults to it but is independent.
    calc_hessian_max_atoms: int = Field(default=150, ge=1)
    # The GFN parametrization. Embedding (and its determinism) is the server's `embed_structure`; a
    # seed here would address nothing on the side that embeds.
    xtb_method: str = "GFN2-xTB"
    # Default number of atoms a site-reactivity ranking reports.
    xtb_fukui_top_n: int = 15

    # Start-to-close for one durable xTB job activity (four hours: multi-species reactions on
    # drug-sized molecules). Strictly above `calc_sampling_timeout_seconds`, held by a `Settings`
    # validator, so a sampler timing out surfaces its own error; the margin covers key probe, embed
    # and cache write. The heartbeat, not this, detects a dead worker.
    xtb_job_timeout_seconds: int = 15000
    # Heartbeat timeout for the durable job; longer than one species' optimization plus Hessian.
    xtb_job_heartbeat_timeout_seconds: int = 600
    # Thermochemistry conditions. 1 atm is the gas-phase reference pressure; the solution standard
    # state (1 mol/L) is derived by `science/calc/thermo.py` from the medium.
    xtb_thermo_temperature_k: float = 298.15
    xtb_thermo_pressure_pa: float = 101325.0
    # Quasi-RRHO damping frequency (cm^-1, Grimme 2012), where a mode's entropy is an equal mix of
    # harmonic and free-rotor. 50 is xtb's own `--sthr` default, so `xtb --ohess` agrees with what
    # this system reports. Not part of the Hessian cache key; free energies are recomposed on every
    # call.
    xtb_rrho_cutoff_cm: float = 50.0
    # A negative Hessian eigenvalue below this magnitude (cm^-1) is finite-difference noise; above
    # it the geometry is a saddle point and the thermochemistry says so.
    xtb_imaginary_threshold_cm: float = 25.0
    # Largest gradient component (Hartree/Angstrom) at which a geometry still counts as stationary;
    # frequencies at an unrelaxed geometry give a too-small ZPE. 5e-4 is the server optimizer's own
    # criterion, so a `relax_structure` output passes by construction.
    xtb_stationary_gradient_tolerance: float = 5e-4
    # Reported uncertainty on a semiempirical reaction free energy, in kcal/mol, attached to every
    # result.
    xtb_reaction_uncertainty_kcal: float = 3.0
    # Maximum points in a relaxed scan; each is a remote constrained optimization, composed here.
    xtb_scan_max_points: int = 24
    # A rotational profile's coarse step, in degrees; barrier heights are then refined because a
    # coarse grid steps over a maximum.
    xtb_rotation_step_degrees: float = Field(default=30.0, gt=0.0, le=120.0)
    # Extra points each maximum is resolved with, across the two coarse steps around it.
    xtb_rotation_refine_points: int = Field(default=4, ge=0)
    # Released minima closer than this (degrees) are the same rotamer; well below a three-fold
    # rotor's 60.
    xtb_rotation_merge_degrees: float = Field(default=15.0, gt=0.0, lt=60.0)
    # How far out of line one torsion-profile step must be, as a multiple of the profile's typical
    # step, to be reported as a jump into another basin. A ratio, not an energy, so steep real
    # barriers do not trip it; 4.0 clears the measured smooth profiles.
    xtb_rotation_discontinuity_ratio: float = Field(default=4.0, gt=1.0)
    # Attempts (and displacement, Angstrom) to push a saddle-point geometry along its imaginary mode
    # and re-optimize. Each costs an optimization plus a Hessian; more than two means the structure
    # is saying something real.
    xtb_minimum_refinement_attempts: int = Field(default=2, ge=0)
    xtb_imaginary_kick_angstrom: float = 0.3
    # Default number of IR bands a thermochemistry result reports, strongest first.
    xtb_ir_bands_top_n: int = 12

    # logD: the working pH when a caller names none (physiological 7.4).
    logd_default_ph: float = 7.4
    # Ionised fraction of the one site `calc.pka` reports, at or below which other unmodelled sites
    # can be dismissed; above it `calc.logd` refuses. The neglected shift is at most -log10(1 -
    # r**2) with r = f/(1-f): 0.0012 log units at 0.05, diverging toward 0.5.
    logd_negligible_ionised_fraction: float = Field(default=0.05, gt=0, lt=0.5)
    # Wildman-Crippen's published RMSE (log units), the dominant term of a logD error bar; combined
    # in quadrature with the pKa residual propagated through Henderson-Hasselbalch
    # (`science/calc/logd.py`).
    crippen_logp_uncertainty: float = Field(default=0.68, gt=0)

    # Reaction electronic energy (kcal/mol) at or below which a reaction is flagged for
    # thermal-hazard attention; a common screening threshold, advisory only.
    reaction_energy_exotherm_threshold_kcal: float = -20.0

    # Where the physics runs (`Chemclaw3-mcp`'s `servers/calc`); this repository keeps the cache,
    # the calibration ledger and orchestration. Plain configuration, not a connector bundle: its
    # manifest on `connectors_dirs` would put internal primitives in the prompt and could win the
    # `calc` name.
    calc_server_url: str = "http://127.0.0.1:8860/mcp"
    # Environment variable holding the server's bearer, the same name the server reads; read per
    # request. A missing value refuses the call.
    calc_server_token_env: str = "CHEMCLAW_CALC_TOKEN"
    # Bound on one remote calculation (minutes for a Hessian); a durable job's activity bounds the
    # same wait again.
    calc_server_timeout_seconds: float = Field(default=900.0, gt=0)
    # Bound for a CREST search, matched to the server's `crest_timeout_seconds`: a shorter client
    # bound saves nothing, since the server keeps computing and the answer is discarded.
    calc_sampling_timeout_seconds: float = Field(default=14400.0, gt=0)
    # Bound for calculations pinned to the `xtb` binary (`compute_atomic_descriptors`,
    # `compute_surface_potential`, or any once the server's engine is set to it), matched to the
    # server's `xtb_cli_timeout_seconds`. A shorter bound is a retryable abandonment that starts a
    # duplicate run beside the orphaned one.
    calc_atomic_timeout_seconds: float = Field(default=3600.0, gt=0)
    # How long a pod's claim on a calculation miss survives without a heartbeat. This is how fast a
    # waiter notices that the pod computing a result was killed (takeover = lease + one poll), so
    # shorter detects a crash sooner and tolerates a stalled event loop less; the pod refreshes it
    # three times per lease.
    calc_claim_lease_seconds: float = Field(default=30.0, gt=0)
    # Molecule `connectors/calc/remote.py::remote_version` asks a key for when reading a
    # calculator's version, which does not depend on the molecule. Acetic acid because every
    # calibrated calculator can enumerate it (it has an acidic O-H).
    calc_version_probe_smiles: str = "CC(=O)O"
