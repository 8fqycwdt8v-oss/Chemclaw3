# Task: fix every defect found by the full-codebase review

Requested 2026-08-01. Branch: `claude/v1-readiness-analysis-wd5jq1`.

Source: an adversarially-verified review across all four layers and every phase — 32 findings
verified, 28 survived refutation, 22 distinct defects after merging duplicates, plus four
refutations re-opened as questions. Shipped as **one** PR of eleven commits (#98, merged), not the four planned: the fixes turned out to be
entangled — the entropy correction is inert without the cache epoch, the epoch's own motivation is
that correction, and the authz change falsified a runbook sentence that the chart's test then
enforced. Splitting them would have meant merging a fix that was provably incomplete.

Every fix gets a test proven by mutation ("remove the fix, watch it fail"), per this branch's
standing bar.

(The previous occupant of this file, the dataflow-review implementation, is merged; its record is
D-158…D-170.)

## PR A — Tier 1: the science is silently wrong

- [x] **A1** `science/calc/xtb_thermo.py:344` — the linear rotational partition function divides by
      `2 * symmetry` instead of `symmetry`. Delete the `2 *`. Test: N2 (or CO2) standard entropy
      against literature, which is the geometry class the existing water test cannot reach.
- [x] **A2** `core/chem.py:84-86` — `standardize()` collapses wholly inorganic reagents (NaOH and
      KOH both become water; CsF and NaH lose their anion). Skip `FragmentParent`/`Uncharger` when
      no fragment is organic — a carbon bonded to hydrogen or to another carbon — so the salt keeps
      its own identity.
- [x] **A3** `ingest/eln/compound.py:50-66` — one note id, two bodies: the id is standardized and
      the body is not. Derive `canonical` from `require_standard_smiles`. **After A2**, or the NaOH
      note becomes the water note.
- [x] **A4** `science/calc/logd.py:86-87` — monoprotic Henderson–Hasselbalch applied to polyprotic
      and amphoteric molecules; glycine bypasses the amine refusal entirely. Refuse or flag
      out-of-domain when more than one ionisable site falls inside the pH window.
- [x] **A5** `core/config.py` + `connectors/qm/cache.py` — mock and real DFT energies share a cache
      key. Fold the resolved backend into the calc-version component and require
      `hpc_pipeline_version` when the interface is `nextflow`.
- [x] **A6** `science/calc/solubility.py:111` — the applicability-domain check is absent on every
      cache hit, because `estimate` was added without a version bump. Add a payload-schema
      component to the cache key (not a bare `calc_version()` bump, which also keys the calibration
      ledger).

## PR B — Tier 2: safety screening returns false-clean

- [x] **B1** `science/safety/rules.yaml:86` — `polynitro-aromatic` matches only 1,2-dinitroarenes;
      TNT and picric acid screen clean today. Express the count rather than a written six-atom
      chain. Add meta/para reference molecules to both the unit test and the eval case.
- [x] **B2** Audit every other rule in `rules.yaml` for the same written-chain regiochemistry
      mistake — nobody has enumerated them.
- [x] **B3** `connectors/bo/knowledge.py` — `bo-candidate` notes carry no `compound_smiles` and
      write structures as plain markdown, so `structures_in` returns `[]` for the one note type the
      gate was built for. Fix the writer; fix `tests/test_safety.py`, whose fixture backticks values
      the real writer does not emit.

## PR C — Tier 3: gate, record and durability integrity

- [x] **C1** `kg/git_submitter.py` — no `try/finally`; a failed push leaves the shared checkout on
      the note branch, so an unreviewed note is served as merged knowledge.
- [x] **C2** `deploy/helm/` — sync publishes to a directory no reader resolves; the default install
      answers with zero knowledge-graph evidence, silently. Also `durable/digest.py:56` reads a
      different tree from every other reader.
- [x] **C3** `api/runner.py:322` — the cancellation clause rolls back unconditionally and never
      consults `answered`, deleting a completed turn's durable history.
- [x] **C4** `agent/authz.py:238-246` — `expensive: true` authorizes nothing. Derive the declared
      job names into the effective gate set; cross-check manifests against it in a test.
- [x] **C5** `agent/plan_gate.py` + `api/app.py` — an empty plan is approvable and its hash is a
      constant, so a spent approval re-arms after LRU eviction. Refuse empty plans; move the
      consumed marker into durable state.
- [x] **C6** `durable/connector_job.py:186` + `durable/template_job.py:222` — child started under
      `REJECT_DUPLICATE` beneath a parent that is `ALLOW_DUPLICATE_FAILED_ONLY`, so the retry the
      policy exists to permit dies immediately. Both sites together.
- [x] **C7** `kg/proposal_store.py` — a proposal that succeeds after a transient failure stays
      `failed` forever, excluded from every `open` query.
- [x] **C8** `memory/observation_mining.py` — "on every recorded attempt" asserted over a corpus
      filtered to non-successes, copied verbatim into the promoted playbook's PR body.
- [x] **C9** `memory/supersede.py` — retires playbook notes it did not mint.
- [x] **C10** `durable/connector_job.py:188` — the parent ceiling equals the QM poll's own budget,
      so the poll's retry policy is dead. Couple them in a validator.
- [x] **C11** `deploy/helm/` — a `helm upgrade` changing `.Values.config` never restarts pods.
- [x] **C12** `deploy/helm/_helpers.tpl` — Temporal mTLS paths are mandatory in env against an
      `optional: true` Secret the chart never creates.
- [x] **C13** `durable/job_record.py:175-187` — the runtime counter is booked before a retryable
      write, so an outage reports 5× the compute for a run with no durable record.
- [x] **C14** `science/fingerprints/rxnfp/` + `ingest/eln/ord.py` — the `agents` slot changes no
      bits, so the solvent-domination fix is not delivered though four places assert it is. Also
      `reaction_smiles()` uses raw `smiles`, never `standard_smiles`.

## PR D — Tier 4, and the four re-opened refutations

- [x] **D1** `agent/audit.py:191` — cancelled tool calls write no audit row.
- [x] **D2** `memory/observations.py:150-157` — the anchor docstring's justification is a non
      sequitur (disjointness gives collision-freedom, not anchor stability).
- [x] **D3** `deploy/helm/networkpolicy.yaml` — `/metrics` is on the public Route while four places
      assert a compensating control that does not hold.
- [x] **D4** `core/logging.py:109` — dead entry in the credential-redaction inventory; assert the
      inventory against `Settings.model_fields`.
- [x] **D5** Re-opened (a): the repo holds two opposite rules about whether the producing backend
      belongs in a calculation key, with a test pinning one. Decide once, apply to both, supersede
      whichever ADR loses.
- [x] **D6** Re-opened (b): does `ingest/eln/sync` report a reaction as ingested when its PR was
      blocked by `hazard_problems`?
- [x] **D7** Re-opened (c): re-test empirically whether the lexical/vector legs are
      deterministically dropped by cross-source truncation.
- [x] **D8** Re-opened (d): whether `runaway_rate` intends to count async-job turns, and if so
      whether it is misnamed.

