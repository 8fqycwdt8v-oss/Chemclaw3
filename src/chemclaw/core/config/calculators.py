"""The fast local calculators: xTB, the pKa predictor, and the solubility model.

One domain section of the composed ChemClaw `Settings`. The package `__init__.py` flattens
every section into the one config object and owns the env prefix, the `.env` loading and the
cross-section validators; fields, env names and defaults are exactly as they were when all
sections shared a single module (D-072 mixins, split per D-156).
"""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings


class CalculatorSettings(BaseSettings):
    """The fast local calculators: xTB, the pKa predictor, and the solubility model.

    Grouped because these knobs define the calculators' scientific parameters, and most of them
    enter the calculation cache key — changing one is a deliberate recompute, never a silent
    drift.
    """

    # Which backend runs an xTB task (plan X5). "tblite" is the in-process library;
    # "xtb" is the binary, which brings ANCopt (measured 9-11x faster on drug-sized
    # molecules) and GFN-FF. "auto" prefers the binary when it is installed and falls
    # back, so a deployment without it still works — the *resolved* name goes into the
    # cache key, never "auto", so two deployments never share an entry they disagree on.
    xtb_engine: Literal["auto", "tblite", "xtb"] = "auto"
    xtb_binary: str = "xtb"
    # Numerical accuracy passed to the binary (xtb's `--acc`; lower is tighter) and the
    # wall-clock ceiling on one invocation.
    xtb_cli_accuracy: float = 1.0
    xtb_cli_timeout_seconds: int = 3600
    # xtb's optimization convergence level. "vtight" (2e-4 Hartree/Bohr) is the first one
    # that satisfies `xtb_opt_gradient_tolerance`; the default "normal" stops around
    # 1e-3 and the geometry is then rejected by our own check, which wastes the run.
    xtb_cli_opt_level: str = "vtight"
    # Threads for the binary and its OpenMP runtime. 0 leaves xtb's own default, which
    # uses the machine — correct for a dedicated worker pod running one job at a time,
    # and worth measuring before changing: pinning to 1 cost a factor of ~4 on a 76-atom
    # Hessian. Set it to 1 only where many activities share a pod, to stop them
    # oversubscribing each other.
    xtb_cli_threads: int = 0
    # CREST conformer/tautomer/protomer sampling (plan X6). GPL-3.0 and optional: absent,
    # the ensemble tasks say so and everything else works. `crest_effort` is the default
    # search depth, `crest_max_members` caps how many members a result reports (the
    # search finds dozens; only the populated ones are readable), and the timeout is
    # generous because this is the most expensive calculation in the system.
    crest_binary: str = "crest"
    crest_effort: Literal["quick", "normal", "extensive"] = "quick"
    crest_max_members: int = 20
    crest_threads: int = 0
    crest_timeout_seconds: int = 14400
    # xTB semiempirical calculator (plan step 1c.2). Method is the GFN parametrization
    # (latest: GFN2-xTB). `xtb_embed_seed` fixes RDKit 3D embedding so results are
    # reproducible; it is part of the cache key so changing it recomputes.
    xtb_method: str = "GFN2-xTB"
    xtb_embed_seed: int = 42
    # Decimal places coordinates are rounded to before a `calc.structure.Structure` is
    # hashed. 4 decimals = 0.1 pm, far below any chemical significance, so run-to-run
    # float noise cannot fork the cache; it is part of the structure id, so changing it
    # re-addresses every structure and therefore recomputes.
    xtb_geometry_decimals: int = 4
    # Wiberg bond order above which a pair of atoms is reported as bonded. 0.5 keeps
    # real bonds (a single bond is ~1.0) and drops the long-range tail.
    xtb_bond_order_threshold: float = 0.5
    # Default number of atoms a site-reactivity ranking reports. Enough to see the
    # ordering of a ring plus its substituents without flooding the agent's context.
    xtb_fukui_top_n: int = 15

    # Geometry optimization (plan X3). Convergence is on the largest absolute gradient
    # component in Hartree/Angstrom; 5e-4 is ~2.6e-4 Hartree/Bohr, tighter than xtb's
    # own "normal" setting because the finite-difference Hessian is only as clean as
    # the stationary point under it. Both enter the cache key.
    xtb_opt_gradient_tolerance: float = 5e-4
    xtb_opt_max_steps: int = 1500
    # Trust radius (Angstrom): the furthest one Cartesian coordinate may move in a
    # single bounded L-BFGS-B leg. Without it the optimizer's first step on a strained
    # geometry is large enough to collapse a bond and leave the SCF unconvergeable.
    xtb_opt_trust_radius: float = 0.35
    # Curvature (Hartree/Angstrom^2) assumed for the directions the ANC preconditioner's
    # pairwise model cannot see — bends and torsions, which on ibuprofen is 37% of them
    # (`calc.anc`). Not a safety floor: it is the stand-in for the missing terms, and the
    # true Hessian's median curvature is ~0.4. Swept against measured step counts, it
    # optimizes near 1.0 and turns over by 1.5; at a safety-net 0.005 the preconditioner
    # is slower than none at all.
    xtb_anc_curvature_floor: float = 1.0
    # Central-difference step for the Hessian, in Angstrom. Small enough that the
    # harmonic approximation holds, large enough that the gradient difference is well
    # above the SCF's own numerical noise.
    xtb_hessian_displacement: float = 0.005
    # Atom-count ceiling for a Hessian. Cost is 6N gradient evaluations of a finite
    # difference, so this is an absolute practicality limit, not a latency one — the
    # latency question is answered by the inline budget below, which routes an expensive
    # request to Temporal instead of refusing it. 150 covers the 200-800 Da range this is
    # pointed at (an 800 Da molecule is ~120 atoms with hydrogens) with headroom; a
    # Hessian there is ~40 minutes, which is a job, not a refusal.
    xtb_hessian_max_atoms: int = 150
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
    # optimization, so this bounds the cost of a single agent call the way
    # `xtb_hessian_max_atoms` bounds a Hessian.
    xtb_scan_max_points: int = 24
    # How many times a geometry that lands on a saddle point may be displaced along its
    # imaginary mode and re-optimized, and how far (Angstrom, the largest atom's motion).
    # One attempt clears the ordinary case — a force field's eclipsed methyl held by
    # symmetry through a Cartesian optimization; more than two means the structure is
    # saying something real that another kick will not fix. Each attempt costs a full
    # optimization *and* a full Hessian, which on a 100-atom substrate is tens of
    # minutes — so on large molecules the refinement, when it triggers, dominates the
    # job. Measured: sildenafil (63 atoms) does not reach a clean minimum on the first
    # pass, so this is not a rare path at drug size.
    xtb_minimum_refinement_attempts: int = 2
    xtb_imaginary_kick_angstrom: float = 0.3
    # Default number of IR bands a thermochemistry result reports, strongest first.
    # A measured spectrum is compared on its strong bands; the weak modes between them
    # carry no information for that comparison and cost context.
    xtb_ir_bands_top_n: int = 12

    # xTB-based pKa predictor (plan step 1c.4): pKa from the GFN2-xTB solvated (ALPB)
    # deprotonation energy via a linear calibration pKa = slope*dE + intercept. Defaults fitted
    # over 10 reference O-H acids (R^2 0.93, residual ~1.6 pKa units); recalibrate against a
    # proper dataset before production. Changing any of these invalidates the cache (they are
    # part of the key).
    pka_solvent: str = "water"
    pka_calibration_slope: float = 0.28733
    pka_calibration_intercept: float = -29.3116
    pka_uncertainty: float = 1.6
    # Conjugate-acid pKa of a **base**, its own calibration (X11). Fitted over seven
    # aromatic/aryl-nitrogen references spanning pKa 1.0-6.95: Spearman 1.000, R^2 0.993,
    # in-sample RMSE 0.17. The reported uncertainty is deliberately far above that RMSE —
    # a two-parameter fit on seven points does not support a tighter out-of-sample claim.
    # Aliphatic amines are refused rather than calibrated; see `calc.pka`.
    pka_base_calibration_slope: float = 0.241396
    pka_base_calibration_intercept: float = -22.1843
    pka_base_uncertainty: float = 1.0
    # Reported log-S RMSE of the Reizman-descriptor solubility model (calc step 1c.3):
    # model uncertainty attached to every prediction, config like `pka_uncertainty`.
    solubility_rmse_log: float = 0.75
    # Split-conformal intervals over this deployment's own recorded residuals (F8-T1), which are a
    # claim about *this* system's chemistry rather than about a paper's test set — so they replace
    # the reported constant above whenever there is enough evidence, and `Estimate.method` says
    # which was used.
    #
    # `coverage` is the fraction of future predictions the interval is meant to contain. 0.9 rather
    # than 0.95 deliberately: the guarantee needs `ceil((n+1)·coverage) ≤ n` observations to exist
    # at all, so 0.95 needs 19 reconciled measurements before it can report anything and 0.9 needs
    # 9 — and a deployment that has measured ten compounds should get the benefit of them.
    #
    # `minimum_samples` is a floor beneath the arithmetic one, because an interval computed from
    # nine residuals is *valid* and still badly estimated: the quantile is the largest of nine
    # numbers, so one unusual compound sets it. 20 is where a residual distribution starts to have
    # a shape. Below it the reported constant is used and said so.
    calibration_conformal_coverage: float = Field(default=0.9, gt=0, lt=1)
    calibration_conformal_min_samples: int = Field(default=20, gt=0)

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
