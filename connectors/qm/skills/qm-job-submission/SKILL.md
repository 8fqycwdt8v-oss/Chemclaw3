---
name: qm-job-submission
description: >-
  Judgment for when to run a quantum-mechanical (QM/DFT) calculation and how to
  choose the method and basis set before calling compute_dft_energy.
tools:
  - compute_dft_energy
  - get_durable_job_status
---

# QM job submission

This skill holds the *judgment* around QM/DFT calculations. The mechanics —
starting the durable job and polling it — live in the `compute_dft_energy` and
`get_durable_job_status` tools; use this skill to decide **whether** and **how** to
call them.

`get_durable_job_status` answers for **every** durable job, not only QM. The larger xTB
tasks (a reaction over several sizeable species, a solvent screen, a long scan) hand back
a job id instead of a result whenever they run past their inline budget. That is not an
escalation to DFT and does not go through this skill's cost reasoning — it is the same
cheap calculation, run somewhere it will not block the conversation. Poll it the same way.

## When a QM calculation is warranted

A QM/DFT job is slow and expensive (HPC time). Prefer it only when cheaper
evidence is unavailable:

- No sufficiently similar molecule already has a result (check the knowledge
  graph / fingerprint search first, once those layers exist).
- The question needs electronic-structure information: energies, geometries,
  transition states, regioselectivity, spectra.
- **The fast semiempirical tier has already been tried and is not enough.** GFN2-xTB
  answers relative stability, frontier orbitals, partial charges and regioselectivity in
  under a second, and now also optimized geometries, free energies, IR spectra, reaction
  thermodynamics and torsion barriers in seconds to minutes (`calculation-selection`).
  That is a much larger set than it used to be, so check it before escalating. Escalate
  when the decision turns on a difference smaller than the fast method's error bar, or
  on something semiempirical methods do not provide at all — transition states and
  activation barriers, excited states, accurate absolute energies — and say which. "The fast answer is
  good enough for this decision" is the right conclusion more often than it is reached;
  `computational-evidence` holds that judgment.

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

State the method and basis set you chose and why, then call `compute_dft_energy`.
Report the returned job id to the user rather than waiting for the result.
