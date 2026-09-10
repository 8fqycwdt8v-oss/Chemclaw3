# Multi-wave re-review — waves 10-15

**What came before.** Nine waves are merged (#331-#339). Waves 1-3 *read* the tree, 4 **ran**
it, 5 **attacked** it, 6 **interrupted** it, 7 **measured** it, 8 audited **the doctrine**, and 9
worked the register down. Their plan and closing review are at `git show aa7a3b28:tasks/todo.md`;
they are not restated here, because a ticked item that outlives its closure reads as live state
(lesson 16, and the reason `DEFERRED.md` grew nine sections describing each other).

**What is left is what those nine waves treated as given.** That is the axis these six take, and
each entry below names the thing not to assume:

- Waves 1-9 read, ran and attacked **this repository**. Three others complete the system, and
  every claim about the seam between them was made from this side of it. — W10
- Every wave measured *plumbing*. Nothing checked whether a **number this system computes about
  chemistry is right** — units, magnitudes, or whether a cache key names everything that changes
  its value. — W11
- Everything ran on **one generation of code against one generation of schema**, migrated forward
  from empty, once. — W12
- Everything ran on a **fixture-sized deployment**: a corpus of tens, a thread of forty turns, a
  fresh database. Wave 9's `read_corpus` returning 5 of 12 entries with `complete=True` is that
  blindness caught once, by accident. — W13
- Wave 8 asked of every control *does it exist and is it called*. It did not ask **whether the
  human on the other end can act on what it produces** — which is the whole of the argument
  `D-2026-09-05-the-gate-follows-behaviour-not-knowledge` rests on. — W14
- And waves 4-9 each found that **the review had reproduced, inside its own repair, the defect it
  exists to find**. Assume waves 10-14 did it again. — W15

## Rules (carried forward — they were earned, and two are new)

- No agent runs `git checkout --`, `git stash`, or `git reset`. A/B by hand-edit, `cp f f.bak` /
  `mv f.bak f`, and clear `__pycache__` inside a mutation loop.
- Fix agents get **disjoint file sets**, checked for duplicates before launch. Cross-scope
  residuals come back to the orchestrator.
- Every fix reproduces the defect first, checks `docs/decisions/` for an ADR making the behaviour
  intentional, and ships a test **watched failing** against unfixed source — per fix, not per batch.
- `rm -rf .mypy_cache` before the gate whenever agents ran in parallel.
- Prose is evidence about what its author believed, never about what the code does. No wave writes
  a current number into prose; the test holds it.
- Never edit a merged ADR. A changed decision gets a new one.
- **New, from wave 9:** a reviewer must state the premise it was handed and whether measurement
  upheld it. Three of four premises in wave 9 were false, and each disproof was worth more than the
  fix would have been. "The row is wrong" is a finding, not a failure to deliver.
- **New, from wave 8:** a guard this review adds is mutation-tested **in its own wave**, not
  deferred. Wave 8 found three of the review's own new guards vacuous, and the orchestrator's own
  assertion accepted exactly what it was written to refuse. Also: before building a check, confirm
  the data it reads exists — a control that always passes is worse than a missing one.
- A gate that has never been watched refusing is a claim that a gate exists. Every validator,
  guard or probe touched must be driven to a **red** as well as a green.

---

## Wave 10 — The fleet seam: the three repositories this one talks to

All three siblings are checked out (`/home/user/8fqycwdt8v-oss/chemclaw3-mcp`,
`/home/user/chemclaw3_mock`, `/home/user/chemclaw3_ui`). Fixes there ship as **their own PR in
that repo**, per CLAUDE.md — never proxied through this one.

- [x] W10.1 `connector.yaml` in both directions: this repo's loader against every manifest the
      fleet actually serves. A field this side ignores, a field that side needs and this side
      drops, a bundle that loads here and fails there. Same for `datasource.yaml` and the sink
      manifest against any real consumer.
- [x] W10.2 The calc seam (`CHEMCLAW_CALC_SERVER_URL`): request/response shapes, error and timeout
      semantics, and the item wave 5 deferred as needing the other repo — **a wrong value under a
      right cache key**. Does D-011's key name every input that changes the answer (method,
      solvent model, charge/multiplicity, server version)? Drive it: change an input the key omits
      and see whether the stale value comes back.
- [x] W10.3 `SERVED_ELSEWHERE_ALLOWANCE`: run the sibling's own servers against the ratchet,
      measure today's real bound-tool prefix with connectors bound, and check the skip path is
      honest about what it did not look at.
- [x] W10.4 The mock's fidelity. Every green test resting on `Chemclaw3_mock` or
      `chemclaw.cli.mock_llm` is evidence about **the mock**. Diff the mock's surface against the
      real one it stands for: usage fields, streaming/tool-call shapes, JWKS/OIDC claims, error
      bodies. Where they diverge, that divergence is the size of the untested gap.
- [x] W10.5 The UI contract: every SSE event name and field the front door emits against what
      `Chemclaw3_ui` consumes, and its e2e/full-stack config against what this repo actually
      serves. A renamed field is a silent break in the direction no test here can see.
- [x] W10 fix stage (per repo), gate, PR, merge on green

## Wave 11 — The science: units, magnitudes, and identity

The first wave whose subject is whether a computed number is *right*. Lessons 20, 24, 25 and 26
are all science-side defect classes, and no wave has swept for them.

- [x] W11.1 Unit and constant audit across `science/`, `connectors/`, `publish/` and the wire
      models: hartree/kcal/eV, bohr/Å, K/°C, ppm, molarity. Each conversion traced to **one**
      definition — two branches nearly inflated every geometry by 1.8897 for want of this.
- [x] W11.2 The arithmetic that stayed here after the physics left: RRHO thermochemistry, Crippen,
      the calibration ledger's fit, the `dft` backfill projector. Each against a **reference
      value**, not against itself; split the class before judging a bad fit.
- [x] W11.3 Cache identity in this repo (the in-process half of W10.2): `key` derivation, its
      normalisation, and the round trip through `result JSONB` — does a float, a null or a unit
      survive it unchanged?
- [x] W11.4 Fingerprints and similarity: ECFP4/DRFP parameters, bit collisions, the metric's
      actual semantics, and `standardize()`'s class of defects re-swept beyond the three instances
      already named.
- [x] W11.5 BO: objective sign conventions, constraint handling, whether the benchmark corpus
      exercises what its registration claims, and whether any documented ceiling bounds the thing
      it is documented as bounding.
- [x] W11.6 Labels, safety projections and reaction records: does a value keep its meaning across
      every store round-trip and every projection into the result store?
- [x] W11 fix stage, gate, PR, merge on green

## Wave 12 — Lifecycle: upgrade, rollback, two generations at once

- [x] W12.1 All 89 migrations replayed from empty against the live database, in order, **twice**
      (idempotency), and against a database that already holds the objects.
- [x] W12.2 Grants: reconciled on every deploy rather than applied once (lesson 22). Does a table
      added by a late migration arrive with its grant, and does the reconciliation notice a drift?
- [x] W12.3 Rolling update, both directions: old code against new schema, new code against old.
      Every persisted shape read by both — the `session_messages` stamp, checkpoint blobs,
      `turn_costs`' new columns, the outbox lease, `reaction_records`.
- [x] W12.4 Rollback: does the previous image run against the migrated database, and is the
      failure **loud** where it does not?
- [x] W12.5 Backfills and projectors run against a mixed-shape table, not a uniform one: the `dft`
      backfill, the message migration, the record backfill.
- [x] W12 fix stage, gate, PR, merge on green

## Wave 13 — Day one and day one thousand

- [x] W13.1 Cold start: empty database, no corpus, no connectors, no sinks, no skills, no
      `SERVED_ELSEWHERE` sibling. Every read path at zero rows — does each answer *honestly*, or
      silently emptily? (Lesson 18: the obvious implementation returns a silently empty answer.)
- [x] W13.2 **The silent-truncation sweep.** ~50 default `limit=` parameters and every unpaginated
      scan in `src/`: which return one page while the caller believes it holds everything? This
      generalises wave 9's `read_corpus` (5 of 12 with `complete=True`) from an accident into a
      class, per lesson 20.
- [x] W13.3 Aged state: synthesise a deployment with years of rows — a large corpus, thousands of
      sessions and turns, a big graph — and measure what degrades that was fine at wave-7 scale.
- [x] W13.4 What is implicitly single-site: ids, namespaces, caches, metric labels, the knowledge
      graph's git repository, the checkpointer's thread space. A second tenant is a question this
      tree has never been asked.
- [x] W13.5 Ceilings at rest under the aged tree: disk, WAL, index bloat, and whether the retention
      posture actually holds it bounded.
- [x] W13 fix stage, gate, PR, merge on green

## Wave 14 — The chemist's view: what actually reaches the human

`D-2026-09-05-the-gate-follows-behaviour-not-knowledge` deleted the PR gate and named three
existing things as the control that replaced it: provenance on every chunk, citations checked at
the point of use, and contradiction. Wave 8 asked whether those exist. This asks whether they
**work for the reader**, which is the only form in which they are a control at all.

- [ ] W14.1 Drive an agent-written note end to end: does it land carrying `created_by: agent`,
      reach a reader beside its citations, and can a chemist tell it from a reviewed one?
- [ ] W14.2 Contradiction and supersede, driven: write a note that contradicts a held one and
      check what a later retrieval actually returns — including bi-temporal `valid_to`.
- [ ] W14.3 Retrieval quality measured rather than asserted: a probe set, recall and precision, and
      a re-check that the cap still does not starve a source (D-2026-08-01).
- [ ] W14.4 The answer surface: SSE, CLI, report harness, evidence pack. Does an error tell the
      truth, and is a **degraded** answer distinguishable from a complete one?
- [ ] W14.5 `explain` and the audit trail read back on a current session, end to end — the
      reconstruction that was silently blank once already.
- [ ] W14.6 What the model is told about its own controls versus what is true (the withdrawn-claim
      shape): every present-tense sentence in the system prompt and the skills listing, checked.
- [ ] W14 fix stage, gate, PR, merge on green

## Wave 15 — Residue, and the operator's incident

- [ ] W15.1 Everything waves 10-14 leave open, worked down as wave 9 did: closed, or **decided**
      with the trade stated. A row nobody revisits is not a decision.
- [ ] W15.2 Mutation sweep over every guard waves 10-14 added, and over the modules they touched.
- [ ] W15.3 Incident rehearsal: inject three real failures — a connector serving wrong data, a
      wedged durable job, a poisoned checkpoint — and diagnose each using **only** logs, metrics,
      traces and the dashboards. What cannot be diagnosed is the finding.
- [ ] W15.4 Re-derive every number waves 10-14 wrote into prose, at HEAD, one last time.
- [ ] W15.5 Final gate incl. `make cov`, PR, merge on green

## Review

*(the closing review is written at the end of wave 15; each wave adds its own section)*

### Wave 13 — day one and day one thousand (MERGED: see PR below)

Twelve waves ran on a fixture: a corpus of tens, a fresh database, a
forty-turn thread. This one asked what the code does on **day one** and on
**day one thousand**. Five review agents, nine fix agents.

**Three of the five premises were wrong, and each disproof is worth more
than the fix it displaced.**

1. **Cold start is mostly handled.** Every path whose job is "have we seen
   this before" is honest, sometimes conspicuously — `index_empty: true`
   with a verdict opening `"SEARCH NOT RUN"`, `"NO ROWS IN SCOPE"`,
   `"UNCALIBRATED: … Its accuracy is unknown, not good."` Ten of ten
   validators report honestly, eight exit non-zero. **Zero crashes.** What
   bites at cold is not the answers but *the machinery that asserts things
   about them*.
2. **The tree is already scale-hardened** where the brief predicted it
   would not be — retention measured at 600k rows, backfill keyset-paged
   from a 500k measurement, the outbox with `EXPLAIN` at 200k, the
   fingerprint probe chosen from 0.55 ms against 26.32 ms. Re-measured,
   the numbers still hold. The two real findings at scale are **not
   database problems**, which is why eleven waves of query review missed
   them: both are invisible to `EXPLAIN`.
3. **The retention register is right.** Fourteen adversarial cases, every
   prunable table reached exactly as documented. The premise "something it
   claims to prune, it does not" is **false at row level** — and true at
   byte level, which nobody had asked.

**The two worst findings are not crashes.**

- **`erase_actor` stops protecting what it claimed.** It took the turn
  lease one session per round trip at ~56/s against a 60 s lease nothing
  refreshed, so above ~3,500 sessions the first claims lapsed while it was
  still taking the last. Measured: 40% expired before the erase
  transaction opened, and a simulated second pod **took a slot while the
  sweep was running** — the live turn the guard exists to refuse, admitted
  by the guard. A data-subject erasure that cannot complete is a
  compliance problem, not a latency one.
- **A prompt asserting controls the deployment does not have.** 16 tool
  names nothing binds, including `screen_hazards` in the paragraph telling
  the model to call it before proposing a synthesis; and a traceability
  claim in the present tense on a deployment where one completed turn
  leaves `audit_events` 0 and `explain` blank. D-122's *stated* condition
  and its *implemented* condition were different predicates, and
  `.env.example` sits in the gap.

### Where measurement overturned the brief — five times

1. **The compaction quadratic was ours.** The brief blamed upstream's
   `messages[:idx]` slice; measured, that slice is **0.3%** of the cost.
   The dominant term is `count_tokens` inside a branch **upstream defaults
   off and this repository turns on**. The proposed fix would have removed
   0.3% and left 356 s at 32k messages standing.
2. **`GREATEST` on the second cursor would lose data.** `corpus_cursors`
   is TEXT in the source's domain, and `GREATEST('9','10')` is `'9'` — the
   high-water spelling pins at the first single-digit id and skips
   *forward*, where blind costs only a re-drain. That module keeps its
   blind upsert deliberately.
3. **De-registering unrunnable launchers breaks two shipped profiles**
   outright, taking them from "one procedure unavailable" to every turn
   failing at build.
4. **The `work_mem` fix is 27% slower** at 53 MB per caller. The disk
   spill was a symptom; sorting 600,000 rows to answer a 24-row question
   was the cost. Splitting the DISTINCT is 9.0x.
5. **Two of three proposed counters were written and dropped.** Reclaimed
   bytes is structurally near-zero — retention deletes the *oldest* rows
   and a plain `VACUUM` truncates only trailing pages — and no rule over
   either fires solely on a fault.

**Two findings were found *by* the fixing, not by the review.** Splitting
the prompt into blocks exposed that the envelope rule shared a paragraph
with `record_knowledge_note`, which `subagents.py` subtracts — so the
compiled helper graph was sent **no envelope rule at all**, half the
injection defence, while the framing test stayed green reading the maximal
text. And batching the erase claims exposed that the obvious refresh
**deadlocks against the erasure it protects**.

**My own error, recorded because the rule is mine too.** Wiring a counter
into `record_refusals`, I used keyword labels where `METRICS.increment`
takes a mapping. That module swallows the exception by design, so the
eviction rolled back with it and the cap silently stopped applying. My
first A/B said it was not mine — and that A/B was invalid: `ruff format`
had reflowed the call, so the `str.replace` matched nothing and I compared
the file with itself. I made the same class of error twice more in this
wave, once measuring a "clean checkout" that an editable install resolved
back to the branch.

### The number this repository did not have

At its own sizing (200 chemists, 3,000 turns/day): **111 MB/day durable,
none reclaimed — a 100 GB volume fills in ~2.5 years**, 69% of it one
table stored uncompressed by an argument resting on a retention window the
shipped configuration sets to zero. WAL is 333 MB/day, a ~29:1 ratio.

### Decisions taken, with what each costs

- **The context ceiling rose 500** and the cost is paid where the
  constraint is: the trigger rises with it and keeps its allowance whole,
  the budget cannot (it is derived downwards from the 128k window), so the
  thread loses 500 tokens — 1.16%. Set with ~420 tokens of headroom on
  purpose: a ceiling 23 tokens above a measurement is a tripwire, not a
  ratchet.
- **`temporal.namespace` has no default and the chart refuses to render.**
  A boolean was declined — three environments would each tick it and still
  collide. Two releases need separate databases too, and no chart guard
  can check that half.
- **The fingerprint definition joins the key**, and what it costs is
  stated: with no DELETE for the runtime role a finished rebuild leaves
  the superseded generation forever and every search says PARTIAL until an
  operator disposes of it. Not papered over with an anti-join probe, which
  is O(n) per search in exactly the healthy case.
- **Measurements aggregate rather than collapse.** The default source
  still collapses, and the reply says so instead of hiding it.
- **The vacuum is not opt-in**, on three measured grounds — a sweep
  without it does not bound growth at all.

**Deliberately open**: ANN recall at scale (uniform random bit vectors
give each query one true neighbour by construction, so it is neither
confirmed nor falsified); the scratchpad store, which nothing bounds;
`corpus_molecules`, which keeps the definition defect until its own
upsert moves; the fingerprint re-index job that would carry the measured
3.4x bulk path; and the skills listing, which over-promises the same way
`_INSTRUCTIONS` did because it narrows by what manifests advertise rather
than by what a turn binds.

### Wave 12 — lifecycle (MERGED: see PR below)

Eleven waves ran one generation of code against one generation of schema,
migrated forward from empty, once. This one asked about the upgrade, the
rollback, and the minutes when both generations are live — which is every
deploy. Five review agents, six fix agents.

**The deploy-breaking finding.** The grant set is a full restatement
applied by a `pre-upgrade` hook, so a verb removed from the file is
**revoked from the release that is still serving**. Measured on a real
commit: the old release lost `INSERT` on a table it was writing. There was
no rollback hook at all, so `helm rollback` restored the old image against
the new ACL. Two prose claims — "the grants only widen", in the job
template and an ADR — were falsified by their own repository.

**The silent one.** Rolling back past migration 090 reverses a correctness
control: the calculation cache stops filtering by epoch, so
`find_calculations` hands the model superseded results as evidence to
cite, with no exception, log or counter. Every other rollback break found
is loud; this one is not.

**And the operator was told the opposite.** `migrate()` iterates the
image's own files, so a rolled-back image printed "already up to date"
against a database eleven migrations ahead — the documented recovery
command asserting the thing that is false. `/readyz` probed `SELECT 1`, so
a pod *ahead* of the schema passed readiness and threw on the first turn.
The runbook's one rollback paragraph promised every migration "only
expands"; four drop and re-add a primary key, one replaces a CHECK, one
nulls a backfilled column out, one rewrites a type.

### Where measurement overturned the brief — again, four times

1. **The Temporal severity was wrong.** A change appended at the *end* of
   a run does not wedge an unfinished one: driven as a real handover on
   the live broker, generation N parked a run and today's code completed
   it. Nondeterminism is a property of a **closed** history, and
   production never resumes one. The hazard is relocated, not removed.
2. **And the hang was partly first-party**: `failure_exception_types=
   [Exception]` turns the error into a workflow-failure command a closed
   history cannot accept, so it evicts forever. Emptied, it raises in
   under a second — a detector not knowing that hangs CI instead of
   failing it.
3. **A second replay-breaking migration** nobody had named (058, failing
   the other arm of the same docstring), found by replaying every file
   individually rather than trusting the first.
4. **A third way a row lands in no bucket**: `ON CONFLICT DO NOTHING`
   means a re-run over a fully covered corpus reports rows unaccounted
   for with nothing wrong at all.

**Two of the wave's findings were consequences of wave 11's own `std7`
bump**, which I had called "worth expecting rather than discovering". They
were sharper than that: one re-indexed row flips `index_is_empty` to
False, so a chemist gets a confident answer over 2% of the corpus, and the
re-label stamps rows the labelling server could not answer for as current.

### Decisions taken, with what each costs

- **`pre-rollback` hook** for grants, and the *contract step* declined:
  enforcing "hold a verb for one release" needs a lag set that weakens the
  excess ratchet exactly where it is strongest, for a discipline no test
  enforces. **The upgrade window is still open and the ADR says so**, with
  the exact blocker.
- **Archived histories replayed by the ordinary suite**, not a CI job —
  the docstring that asked for one named the wrong obstacle, since what a
  self-recorded history lacks is *age*, not a runner. `workflow.patched`
  declined for an empty recipient set; worker versioning declined because
  draining a 12.6-hour run needs the two-worker overlap `replicas: 1`
  exists to prevent.
- **`ALTER COLUMN … TYPE` into `_BREAKS_PREVIOUS_IMAGE`, not
  `_DESTROYS_DATA`**: 091 is a widening that already exists, and the
  destroy bucket refuses with no exemption mechanism *on purpose*.
- **Drift reported, not refused**: refusing fails the `pre-upgrade` hook
  and blocks the release, and the operator whose hand-grant caused it is
  the one person who cannot fix it from the deploy. CI fails; the deploy
  reports.
- **No fingerprint re-index built**: re-fingerprinting from stored labels
  reproduces the previous standardization's losses, and the runtime role
  holds no DELETE, so a key-changing bump would leave orphans counted
  forever. The state is made discoverable instead; the register carries
  the rest.

**What held.** All 90 migrations replay from empty in 437 ms as one
transaction — a mid-run failure left the ledger at zero rows, not
half-applied. All 38 `ADD COLUMN NOT NULL` statements carry a `DEFAULT`,
so an old pod's INSERT still works. No ledger drift on the live database.
The session store round-trips against a schema eleven migrations ahead.
The publish walk is genuinely resumable and both backfills are idempotent.
And the INSERT-only audit grant is really in force — UPDATE and DELETE
both denied as the role, not merely asserted by a file.

**Deliberately open**: the upgrade window for contracting grants (blocked
on a two-document assertion); 21 of 22 background workflows have no
archived history; and there is no fingerprint re-index path.

### Wave 11 — the science (MERGED: fleet #53; this repo's PR below)

The first wave whose subject is whether a **number is right** rather than
whether the plumbing around it works. Six review agents, six fix agents.

**The two worst findings are wrong answers, not crashes.** A NaN in a
secondary objective is a wildcard that never loses — `_dominates` reads it
as "no difference", so a run whose impurity assay *failed* dominated every
clean run it beat on yield and the chemist was shown a one-point Pareto
front consisting solely of it. And an objective with no spread reported
R² 1.00 with "predicts held-out runs perfectly", which is exactly the
answer a flatlined assay must not give.

**The class four agents found independently.** Non-finite floats reaching
`jsonb` on five paths, doing different damage each time: the calc cache
recomputes forever, the publish outbox loses the batch beside the poison
record, and the ELN sync **never advances its cursor**, so one site row
holds a corpus at a fixed date on every run thereafter. The rule already
existed here — applied by hand in two subsystems, missed in five. Now one
function plus a derived guard; the eight unreviewed sites are *counted*
rather than converted, because whether each needs a write guard or a model
constraint depends on a reachability measurement this wave did not make.

**Where measurement overturned the brief — five times.**

1. The RDKit divergence between repos does not exist (14/14 identical);
   the cross-repo control shipped anyway, proven by injecting one.
2. My own claim that a large float "keeps its value exactly" is false —
   `6.02214076e23` returns a different exact integer, the same double.
3. `ProcessConditions` was guarded on four of five fields **by accident**,
   because `ge`/`le` reject NaN.
4. The fleet's xtb ordering assumption was *correct*; the real defect was
   the external-mode **count**, where the two repos disagree at 179.0° and
   every IR band shifts silently.
5. My proposed units fix was illegal — the layering test forbids `core`
   importing a sibling, forcing the opposite direction.

**Three decisions taken with their cost stated.** `includeChirality=True`,
decided not by the tie but by the citation: a hit carries a stereo-aware
`compound_note_id`, so the index returned the wrong enantiomer at 1.0000
citing a *different compound's note*. The qRRHO cutoff 25 → 50 cm⁻¹,
because one of the two Hessian producers is the xtb binary, making 50 the
only value a chemist can check. And the alkali-salt sweep **split**: the
alkoxide collapse accepted and asserted, the hydride case fixed, because
neutralization there removes a hydrogen and turns a reducing agent into a
borate ester.

**What held.** Every CODATA constant checked is right; the RRHO chain
reproduces closed-form references to 1e-13 and NIST-JANAF to within the
anharmonicity RRHO omits; two AST sweeps over 4,906 unit-named assignments
found zero double or missing conversions; all sign conventions correct;
56/56 BO proposals honoured their constraints; every `DOUBLE PRECISION`
path exact under `==`. The 1.8897 trap the brief named is genuinely absent.

**Deliberately open**, each with what would change the answer: the eight
unmeasured `jsonb` boundaries; `std7` makes every stored reaction label
stale on the next drain pass (designed, worth expecting); the similarity
threshold now sits 0.0061 above the measured worst case for a
stereo-unspecified query; and a carbanion written as a separated ion pair
still standardises to its hydrocarbon, left alone because the obvious
guard would also split sodium acetylacetonate from acetylacetone.

### Wave 10 — the fleet seam (MERGED: mock #12, fleet #51, UI #70)

Four repositories, four PRs, and the thing worth recording is that **the seam was
wrong in the direction no single-repo review can see**: every defect below was a
name, a number or a shape that one repository wrote down about another and nothing
ever read back.

**The chart dialled five hostnames that do not exist.** `chemclaw3-mcp-*` against
the fleet's `chemclaw-mcp-*` — one character, five NXDOMAINs, one of them the
address every calculation uses with no second tier behind it. What makes it a
defect rather than a preference is that `values.yaml` *stated the correct rule*
("whatever Service the sibling repo's chart gives its server") in the comment
directly above the wrong name. The same mismatch stood a third time in the release
descriptor, patching a Deployment and a container that both do not exist.

**A wrong value under a right cache key was real**, and it was the item wave 5
deferred as unsettleable from this side. `xtb_bond_order_threshold` was read inside
the payload constructor, outside any spec: acetic acid at 0.5 → 7 bonds, at 0.05 →
9 bonds, **identical key**, and driven through this repo's cache seam the second
pod received the first's answer. It is a *filter*, so it breaks the fleet's own
written rule that an unkeyed argument may permute an answer and may not remove
from it — and those bonds are projected into a published record that is never
pruned.

**The cross-repo bound had never been checked by a machine.** The ratchet searched
one path in one casing under one variable; `infra/live/siblings.sh` searched four
under two, and its own header describes fixing that bug — the same day, in another
PR. So on the one machine with the fleet checked out, the live lanes resolved it
and the ratchet skipped.

**Every mock-driven lane was reporting a fully metered turn that a real gateway
would not.** The mock published `usage` unasked, so `llm_stream_usage=False` books
zero tokens on every turn — a failure this repository has shipped once already —
and its constant bill clamped the estimator ratio to 1.0 forever, leaving the
tightening branch two merged budget decisions rest on exercised by nothing.

### Where measurement overturned the brief — four times in six agents

The rule added for this wave paid for itself immediately:

1. The fleet publishes **five** connectors, not six: `calc` and `rxnlabel` declare
   a `mount:` key this repository's manifest model refuses outright.
2. `SERVED_ELSEWHERE` was **not** widened as briefed — that would have raised
   `PREFIX_BOUND` and both compaction defaults for every deployment on account of
   two bundles only the mock-LLM lane binds. The fleet's whole published directory
   got its own bound instead, and the e2e lane's 2,560-token excess is stated
   rather than absorbed.
3. The 403 Retry button does not exist: `retryable` drives exactly one banner, and
   the endpoint behind it has no 403 source. The mapping was still wrong and was
   fixed; the refusal UI was not built for a banner that cannot appear.
4. The 503 "at capacity" copy is not a defect — the JWKS outage carries its own
   `detail` and the mapper prefers it.

Two more were declined with the measurement: 401/404 request-level mock branches
(both land on the same `error` label an injected status already reaches, so they
unlock nothing), and keying `crest_perceive_max_atoms` (a CREST ensemble is
already not a function of its key, so keying a deterministic field on a
non-deterministic payload buys nothing and re-addresses the most expensive rows in
the system — recorded for a maintainer rather than taken silently).

### What the review did to itself, again

Two of this wave's own repairs contained the defect the wave exists to find. The
orchestrator shipped a stray line into a behaviour catalogue and a lint-red guard;
and the first A/B of the "no behaviour goes undriven" guard *passed* against a
mutation, because the guard is a substring scan and the renamed name still
contained the original. The real removal then failed it correctly. A guard whose
own prose satisfies it is worth knowing about.

### Deliberately open, with the reason

- **CI does not clone the fleet**, so the new cross-repo checks skip there. The
  skip is loud and the suite epilogue now names it, and the tests do run wherever
  a checkout exists. Cloning a sibling in CI needs a token the workflow may not
  have; attempting it blind risks red CI for an access reason rather than a code
  one. What would change the answer: confirming the runner can read the sibling.
- **`check-openapi.mjs` sends no bearer token**, so now that the schema is served
  behind `require_principal` it can run against a dev deployment and not an
  enforced one. Not a break — it could reach nothing at all before.

