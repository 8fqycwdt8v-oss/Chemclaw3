---
name: qm-job-submission
description: >-
  Judgment for when to run a quantum-mechanical (QM/DFT) calculation and how to
  choose the method and basis set before calling submit_qm_job.
---

# QM job submission

This skill holds the *judgment* around QM/DFT calculations. The mechanics —
starting the durable job and polling it — live in the `submit_qm_job` and
`get_qm_job_status` tools; use this skill to decide **whether** and **how** to
call them.

## When a QM calculation is warranted

A QM/DFT job is slow and expensive (HPC time). Prefer it only when cheaper
evidence is unavailable:

- No sufficiently similar molecule already has a result (check the knowledge
  graph / fingerprint search first, once those layers exist).
- The question needs electronic-structure information: energies, geometries,
  transition states, regioselectivity, spectra.

For a purely empirical or precedent-based question, answer from existing data
instead of launching a calculation.

## Choosing method and basis set

Pick the cheapest level of theory that answers the question; escalate only if it
does not.

| Question | Reasonable starting point |
|---|---|
| Fast geometry / relative energies | `B3LYP` / `def2-SVP` |
| More reliable energetics | `B3LYP` or `wB97X-D` / `def2-TZVP` |
| Non-covalent / dispersion-sensitive | a dispersion-corrected functional / `def2-TZVP` |

State the method and basis set you chose and why, then call `submit_qm_job`.
Report the returned job id to the user rather than waiting for the result.
