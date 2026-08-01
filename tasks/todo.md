# Task: fix every defect found by the full-codebase review

Requested 2026-08-01. Branch: `claude/v1-readiness-analysis-wd5jq1`.

Source: an adversarially-verified review across all four layers and every phase — 32 findings
verified, 28 survived refutation, 22 distinct defects after merging duplicates, plus four
refutations re-opened as questions. Four PRs, ordered so nothing later undoes something earlier:
A3 depends on A2 landing first, and PR C's chart work assumes PR A's config change.

Every fix gets a test proven by mutation ("remove the fix, watch it fail"), per this branch's
standing bar.

(The previous occupant of this file, the dataflow-review implementation, is merged; its record is
D-158…D-170.)

## PR A — Tier 1: the science is silently wrong

- [ ] **A1** `science/calc/xtb_thermo.py:344` — the linear rotational partition function divides by
      `2 * symmetry` instead of `symmetry`. Delete the `2 *`. Test: N2 (or CO2) standard entropy
      against literature, which is the geometry class the existing water test cannot reach.
- [ ] **A2** `core/chem.py:84-86` — `standardize()` collapses wholly inorganic reagents (NaOH and
      KOH both become water; CsF and NaH lose their anion). Skip `FragmentParent`/`Uncharger` when
      no fragment is organic — a carbon bonded to hydrogen or to another carbon — so the salt keeps
      its own identity.
- [ ] **A3** `ingest/eln/compound.py:50-66` — one note id, two bodies: the id is standardized and
      the body is not. Derive `canonical` from `require_standard_smiles`. **After A2**, or the NaOH
      note becomes the water note.
- [ ] **A4** `science/calc/logd.py:86-87` — monoprotic Henderson–Hasselbalch applied to polyprotic
      and amphoteric molecules; glycine bypasses the amine refusal entirely. Refuse or flag
      out-of-domain when more than one ionisable site falls inside the pH window.
- [ ] **A5** `core/config.py` + `connectors/qm/cache.py` — mock and real DFT energies share a cache
      key. Fold the resolved backend into the calc-version component and require
      `hpc_pipeline_version` when the interface is `nextflow`.
- [ ] **A6** `science/calc/solubility.py:111` — the applicability-domain check is absent on every
      cache hit, because `estimate` was added without a version bump. Add a payload-schema
      component to the cache key (not a bare `calc_version()` bump, which also keys the calibration
      ledger).

## PR B — Tier 2: safety screening returns false-clean

- [ ] **B1** `science/safety/rules.yaml:86` — `polynitro-aromatic` matches only 1,2-dinitroarenes;
      TNT and picric acid screen clean today. Express the count rather than a written six-atom
      chain. Add meta/para reference molecules to both the unit test and the eval case.
- [ ] **B2** Audit every other rule in `rules.yaml` for the same written-chain regiochemistry
      mistake — nobody has enumerated them.
- [ ] **B3** `connectors/bo/knowledge.py` — `bo-candidate` notes carry no `compound_smiles` and
      write structures as plain markdown, so `structures_in` returns `[]` for the one note type the
      gate was built for. Fix the writer; fix `tests/test_safety.py`, whose fixture backticks values
      the real writer does not emit.

## PR C — Tier 3: gate, record and durability integrity

- [ ] **C1** `kg/git_submitter.py` — no `try/finally`; a failed push leaves the shared checkout on
      the note branch, so an unreviewed note is served as merged knowledge.
- [ ] **C2** `deploy/helm/` — sync publishes to a directory no reader resolves; the default install
      answers with zero knowledge-graph evidence, silently. Also `durable/digest.py:56` reads a
      different tree from every other reader.
- [ ] **C3** `api/runner.py:322` — the cancellation clause rolls back unconditionally and never
      consults `answered`, deleting a completed turn's durable history.
- [ ] **C4** `agent/authz.py:238-246` — `expensive: true` authorizes nothing. Derive the declared
      job names into the effective gate set; cross-check manifests against it in a test.
- [ ] **C5** `agent/plan_gate.py` + `api/app.py` — an empty plan is approvable and its hash is a
      constant, so a spent approval re-arms after LRU eviction. Refuse empty plans; move the
      consumed marker into durable state.
- [ ] **C6** `durable/connector_job.py:186` + `durable/template_job.py:222` — child started under
      `REJECT_DUPLICATE` beneath a parent that is `ALLOW_DUPLICATE_FAILED_ONLY`, so the retry the
      policy exists to permit dies immediately. Both sites together.
- [ ] **C7** `kg/proposal_store.py` — a proposal that succeeds after a transient failure stays
      `failed` forever, excluded from every `open` query.
- [ ] **C8** `memory/observation_mining.py` — "on every recorded attempt" asserted over a corpus
      filtered to non-successes, copied verbatim into the promoted playbook's PR body.
- [ ] **C9** `memory/supersede.py` — retires playbook notes it did not mint.
- [ ] **C10** `durable/connector_job.py:188` — the parent ceiling equals the QM poll's own budget,
      so the poll's retry policy is dead. Couple them in a validator.
- [ ] **C11** `deploy/helm/` — a `helm upgrade` changing `.Values.config` never restarts pods.
- [ ] **C12** `deploy/helm/_helpers.tpl` — Temporal mTLS paths are mandatory in env against an
      `optional: true` Secret the chart never creates.
- [ ] **C13** `durable/job_record.py:175-187` — the runtime counter is booked before a retryable
      write, so an outage reports 5× the compute for a run with no durable record.
- [ ] **C14** `science/fingerprints/rxnfp/` + `ingest/eln/ord.py` — the `agents` slot changes no
      bits, so the solvent-domination fix is not delivered though four places assert it is. Also
      `reaction_smiles()` uses raw `smiles`, never `standard_smiles`.

## PR D — Tier 4, and the four re-opened refutations

- [ ] **D1** `agent/audit.py:191` — cancelled tool calls write no audit row.
- [ ] **D2** `memory/observations.py:150-157` — the anchor docstring's justification is a non
      sequitur (disjointness gives collision-freedom, not anchor stability).
- [ ] **D3** `deploy/helm/networkpolicy.yaml` — `/metrics` is on the public Route while four places
      assert a compensating control that does not hold.
- [ ] **D4** `core/logging.py:109` — dead entry in the credential-redaction inventory; assert the
      inventory against `Settings.model_fields`.
- [ ] **D5** Re-opened (a): the repo holds two opposite rules about whether the producing backend
      belongs in a calculation key, with a test pinning one. Decide once, apply to both, supersede
      whichever ADR loses.
- [ ] **D6** Re-opened (b): does `ingest/eln/sync` report a reaction as ingested when its PR was
      blocked by `hazard_problems`?
- [ ] **D7** Re-opened (c): re-test empirically whether the lexical/vector legs are
      deterministically dropped by cross-source truncation.
- [ ] **D8** Re-opened (d): whether `runaway_rate` intends to count async-job turns, and if so
      whether it is misnamed.

## Review

(filled in at the end)
