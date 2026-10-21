# D-2026-10-21-long-running-compute-runs-on-a-backend-seam — cluster and other long-running compute return as driver manifests behind one external-job workflow, with engine preparation in the fleet

**Status:** proposed · **Date:** 2026-10-21

**Supersedes (in part, on acceptance):** [D-2026-08-26-semiempirical-is-the-whole-tier](D-2026-08-26-semiempirical-is-the-whole-tier.md), its scope line "the calculation tier is semiempirical and complete as it stands". Its findings about what the deleted tier cost stand, and are what this design answers.

## Context

D-2026-08-26 deleted the `qm` tier because one bundle depended on one unreachable launcher (Seqera
Tower). The bundle carried fourteen `hpc_*` settings and a 25-hour job ceiling that every other job
inherited. Its contract could not be verified offline.

What remains:
- Every new long-running backend means hand-written workflow code in core (`JobSpec.workflow`
  names it), a worker, a chart entry and settings.
- The fleet cannot host one, because it is stateless and allows no egress.
- Decisions inside xTB's error bar have no next tier to escalate to. CLAUDE.md's answer today is
  to propose an experiment.

## Options

1. **Stay semiempirical-only** (status quo). Cheapest. Method escalation stays impossible, and the
   next backend will arrive as bespoke code anyway.
2. **Per-engine bundles** (the old `qm` shape). Repeats each cost D-2026-08-26 measured, once per
   engine.
3. **A compute backend seam.** It has these parts:
   - A driver protocol: `submit`, `status`, `fetch`, `cancel`, `estimate`.
   - A `compute/backends/<name>/backend.yaml` naming the driver and its keyword arguments. All
     backend configuration lives here, and none of it in `core/config`.
   - One `ExternalJobWorkflow` that polls on durable timers.
   - A `JobSpec.compute:` block, so a capability is a manifest job.
   - Engine input preparation and output parsing as stateless, keyed fleet primitives that run no
     engine binary.
4. **A remote agent per backend.** Declined in
   D-2026-10-21-a-source-or-backend-is-not-reached-through-a-remote-agent.

## Decision

**Option 3.**
- **Drivers.** They run in a `compute-worker-<backend>` component. Its egress is limited to the
  manifest's `egress:` hosts, sanctioned the way the ELN sources are: it is the deployment's own
  system. The drivers shipped are `fleet` (today's calc path as a backend), `slurm_rest`, then
  `k8s_job`, `tower` and `databricks_jobs`.
- **DFT.** It arrives as fleet `servers/qm` (`prepare_input` and `parse_output`, no binary in the
  pod) plus a manifest-only `connectors/qm` bundle.
- **Ceilings.** They are per job: `timeout_seconds` may raise a job's ceiling only when the job is
  listed in `connector_jobs_long_running_list`. Nothing global grows.

## Consequences

- **The tier is off until enabled.** With nothing in `CHEMCLAW_COMPUTE_BACKENDS`, the system is
  today's. Each driver is tested offline against an injectable transport, and the live lane runs
  against `Chemclaw3_mock`'s `slurmrestd` mock. This answers "a contract nobody can look at".
- **Rules that change.** CLAUDE.md's "Not in this system" loses HPC, DFT and the cluster tier, and
  "every calculation is GFN2-xTB or CREST" becomes a tiering rule. The fleet needs its own ADR: a
  prepare/parse server is not a DFT tier in the fleet.
- **On acceptance**, D-2026-08-26 gains `**Superseded-by:**` naming this ADR `(in part)`.
- **Plan item:** W12. Depends on W6.2–W6.4 and W2.

Revisit when: a backend's lifecycle cannot be expressed as `submit`/`status`/`fetch`/`cancel` (an
interactive or streaming session), which would need a second workflow shape rather than a driver.
