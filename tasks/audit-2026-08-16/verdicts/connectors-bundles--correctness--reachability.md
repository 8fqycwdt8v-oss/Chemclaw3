# Verification — `connectors-bundles--correctness.md`, lens: reachability & consequence

Scope: the two **high** findings. The other four are medium/low and out of scope.
All numbers below were produced by execution, in this checkout plus the companion checkout at
`/workspace/chemclaw3-mcp` (`git rev-parse HEAD` = `92170117d8f25cc5588ad2a014c98a1bc14cdb04`, the
exact commit the finding cites). No source file was mutated. Scripts under `/tmp/vfy/`.

---

## `predict_site_reactivity` re-ranks a set the server already truncated by the *other* mode

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium (reported: high)

### What I did

**1. The mechanism, confirmed by reading both sides.**
`src/chemclaw/connectors/calc/server/tools.py:782-793` sends `{"smiles": smiles}` and nothing else.
`cached_remote` → `remote_compute(session, tool, arguments)` forwards the dict verbatim
(`connectors/calc/remote.py:317-352`). The server tool
(`servers/calc/src/chemclaw_mcp_calc/tools.py:294-334`) therefore runs with its own signature
defaults `mode="electrophilic", top_n=0`, and its body is exactly as quoted:

```python
result = xtb_props.compute_fukui(*xtb_props.fukui_inputs(smiles), mode)
limit = top_n if top_n > 0 else settings.xtb_fukui_top_n     # engine/config.py:77 -> 15
return result.model_copy(update={"sites": result.sites[:limit]})
```

`compute_fukui` (`engine/xtb_props.py:253-256`) sorts descending by the mode's field *before*
that slice, and sets `total_atoms=len(sites)` on the full list. `identity.py:155-163`
(`_site_reactivity`) confirms neither `mode` nor `top_n` enters the key. So the persisted row for
`xtb.fukui` holds the **electrophilic top 15**, and `SiteReactivityResult.ranked_for(mode)` on this
side re-sorts those 15. The comment at `tools.py:790-792` — "the row holds every atom" — is false.
`cached_compute` (`science/calc/store.py:291-316`) persists exactly the payload `_compute()`
returned, so the truncated row is what is stored, permanently. All of that stands.

**2. Reproduced the reporter's repro.** Driving the real client tool with `cached_remote` replaced
by a stand-in that reproduces the server body exactly, over a synthetic perfectly-anticorrelated
site list (`/tmp/vfy/probe_fukui.py`):

```
mode=nucleophilic: sent=[{'smiles': 'CC(C)Cc1ccc(cc1)C(C)C(=O)O'}]
  reported top site: idx 14 (f_plus=0.4242)
  TRUE      top site: idx 32 (f_plus=0.9697)
  WRONG ANSWER: True
  total_atoms=33, sites returned=15
top_n=100 requested -> sites returned 15 of 33
```

**3. Then measured it with real xTB numbers**, which the finding did not do. `tblite` is installed
in the companion checkout, so `compute_fukui` runs for real. 30 molecules, both non-default modes,
comparing the true argmax over all atoms against the argmax over the served electrophilic top-15
(`/tmp/vfy/real_fukui.py`, `/tmp/vfy/batch_fukui.py`, `/tmp/vfy/batch2.py`):

```
ibuprofen    n=33 r=+0.80  nuc:ok(11O->11O)   rad:ok(11O->11O)
diclofenac   n=30 r=+0.42  nuc:WRONG(0O->13Cl) rad:ok(18Cl->18Cl)
omeprazole   n=43 r=+0.87  nuc:ok(8S->8S)     rad:ok(8S->8S)
tamoxifen    n=57 r=+0.26  nuc:ok(35H->35H)   rad:ok(17N->17N)
... (30 molecules total)

top-1 wrong in 1/60 (molecule, mode) pairs;  mean corr(f_minus, f_plus) = +0.60
```

Recall of the true nucleophilic top-15 inside the served set (`/tmp/vfy/recall.py`):

```
ibuprofen   n=33  13/15 present   top-5 present 4/5
naproxen    n=31  11/15           5/5
warfarin    n=39   6/15           4/5
propranolol n=40   8/15           5/5
atenolol    n=41   6/15           3/5
diclofenac  n=30   9/15           3/5
```

### Why

The **mechanism is real, reachable and correctly identified** — an ordinary agent tool call on any
molecule with >15 atoms gets a nucleophilic/radical ranking assembled from a set chosen by
`f_minus`, with no gate, validator or caller-side constraint anywhere in between, and the row is
cached under D-011 so a code fix alone will not repair stored data. `tests/calc_server_fake.py`
genuinely cannot catch it (`_predict_site_reactivity` at line 339 builds a site for every atom and
never truncates), and `top_n` genuinely cannot exceed 15 despite the docstring's "pass a larger
number to see the whole molecule" — an advertised capability that is inert.

What does **not** hold is the finding's stated consequence and the causal story behind it:

- "f⁻ / f⁺ are near-anticorrelated by construction (an electron-rich site is a poor acceptor)" is
  **false for the quantity this code computes**. Condensed Mulliken Fukui indices are dominated by
  which atoms are polarisable, so f⁻ and f⁺ are *positively* correlated: mean **r = +0.60** over 30
  real molecules, +0.80 on the reporter's own example (ibuprofen), +1.00 on styrene. The reporter's
  "systematically among the atoms that were discarded" is the opposite of what the numbers say.
- "essentially every drug-sized molecule ... the returned 'most susceptible site' is not the most
  susceptible site" is **false**: measured **1 of 60** (molecule, mode) pairs. The reporter's
  repro produced a wrong answer because the stand-in it drove was a *perfectly* anticorrelated
  synthetic ladder (`f_minus = 1 - i/n`, `f_plus = i/n`) borrowed from the test fake — an artefact
  of the fixture, not a property of Fukui indices. It measured its own fake.

So a chemist asking "where does a nucleophile add" is, in 59 of 60 measured cases, shown the
*correct* top site — with an incomplete list under it. In the one failing case (diclofenac) they are
shown Cl-13 as the most nucleophilically susceptible site when the true f⁺ maximum is O-0, with no
flag, which is a genuinely wrong regiochemistry answer and is why this is not REFUTED.

The residual defect is best described as **a truncated ranking, not an inverted one**: for every
molecule above 15 atoms the served list silently omits 2–9 of the true top-15 sites, and
`total_atoms=33 / len(sites)=15` reads to a consumer as "the top 15 of 33 for the mode you asked",
which is not what it is. That is worth fixing and the reporter's fix is right (send an explicit
`top_n` — safe, since `identity.py::_site_reactivity` provably excludes it from the key). It is a
medium: a degraded hypothesis list with a rare wrong #1, not a systematically inverted answer.

---

## `parse_qm_output` silently truncates a scientific-notation energy instead of raising

- **Verdict**: OVERSTATED
- **Severity I would assign**: medium (reported: high)

### What I did

**1. The regex behaviour, reproduced exactly** (`/tmp/vfy/probe_parse.py`, the same two patterns
from `connectors/qm/activities.py:45-46`):

```
mock shape     -> energy=-12.3          converged=True
scientific E   -> energy=-1.5423156     converged=True    # input was -1.5423156E+02
scientific e   -> energy=-1.5423156     converged=True
fortran D      -> energy=-1.542315      converged=True
integer        -> RAISES ValueError
SCF trace      -> energy=-153.1         converged=False   # first match, not last
```

Every line of the reporter's evidence table reproduces. The docstring at `activities.py:149-151`
("raises on unparseable output so a corrupt result never silently becomes a ... record") is
demonstrably wrong for four of these six shapes.

**2. The consequence chain, verified by reading it end to end.** It is as claimed: the value goes to
`persist_qm_result` → `default_store().put(...)` (`activities.py:239-241`), and to
`qm_energy_estimate` (`connectors/qm/knowledge.py:45-56`), which sets `in_domain=result.converged`
and `uncertainty=None` — so a 100×-wrong energy is published through the PR-gate marked
in-domain with no error bar. Confirmed.

**3. Reachability of the code path.** Stronger than the finding says: `deploy/helm/chemclaw/
values.yaml:362-366` sets `CHEMCLAW_HPC_LAUNCH_INTERFACE: "nextflow"` with `chemclaw-qm@1.0.0`, so
the nextflow branch is the *shipped chart's default*, and `core/config/hpc.py:113-139`
(`_hpc_launch_config`) only checks that the four endpoint settings are non-empty — nothing validates
the artifact.

**4. Reachability of the *trigger*.** This is where it fails. The artifact is not a QM program's
output; it is a wrapper-written line whose format **this repo specifies**
(`hpc/nextflow.py:142-148`: "Returns the same `energy=… converged=…` text shape"). I searched all
four repos for the producer:

```
$ find . -name '*.nf' -not -path './.venv/*'        -> (nothing, in any of the four repos)
$ grep -rn 'energy=' /workspace/chemclaw3_mock/app/hpc/*.py
  store.py:64:  return f"energy={self.energy_hartree():.6f} converged={converged}"
  src/chemclaw/connectors/qm/activities.py:44:  _MOCK_OUTPUT_TEMPLATE = "energy={energy:.6f} converged={converged}"
```

Both producers that actually exist emit `:.6f`. The `chemclaw-qm` pipeline exists in no repo in the
family — it is site-supplied and unwritten.

### Why

The mechanism is real, reproduced, and the consequence *if triggered* is exactly as stated — a
silently 100×-wrong DFT energy, permanently cached and PR-gated into the graph as in-domain. I have
no quarrel with any of that, and the one-line fix is worth doing regardless.

What does not hold is the **reachability argument**, which is the whole basis for "high":

- "QM programs print energies in scientific notation as a matter of course" is the load-bearing
  claim and it is unsupported. Total energies in hartree are O(10²–10⁴), and every mainstream code
  prints them in fixed-point F format — ORCA `FINAL SINGLE POINT ENERGY -154.231560`, Gaussian
  `SCF Done: E(RB3LYP) = -154.231560 A.U.`, Psi4 `Total Energy = -154.23156`, NWChem
  `Total DFT energy = -154.231560`, xTB `TOTAL ENERGY -154.231560 Eh`. E-notation appears for
  gradients, convergence thresholds and dipoles, not for total energies. Even a Python wrapper doing
  `f"energy={e}"` yields a plain decimal at this magnitude (`repr` only switches to E-notation
  below 1e-4 / above 1e16).
- More decisively, the QM code's own formatting is a red herring: the pipeline writes
  `qm_output.txt` to *this repo's stated contract*, and the only two implementations of that
  contract in the family both use `:.6f`.

So the finding is a defensive-parsing / contract-hardening defect on a producer that does not yet
exist, whose deviation from a stated format is hypothetical. That is a real latent hazard —
unanchored patterns, `search` taking the first match rather than requiring exactly one, a false
docstring, and the only test being `parse_qm_output(job, "garbage output, no fields")`
(`tests/test_qm_workflow.py:161-165`), which is nowhere near a near-miss — but it is not a live
wrong-answer path in any deployment that exists today. Medium, and worth fixing cheaply before the
first site writes its pipeline; not high.

One correction *against* the reporter's own framing, in its favour: the `energy=-154` case it
labels "milder" is arguably the more likely of the two, since an integer-valued hartree total is
what a wrapper printing `%g` on a rounded value would emit — and that path raises loudly, which is
the right failure. It is the E-notation path, which cannot occur without the producer breaking
contract, that fails silently.
