# Process development, HTE campaigns and protocol prediction — early PD to kilo lab

**Status:** in progress. Ideation merged as
`docs/archive/IDEATION-2026-09-20-process-development-hte-and-protocol-prediction.md`; this is the
implementation of it.

The previous occupant, the computable-discriminating-check plan (#425), is
`docs/archive/plans/computable-discriminating-check.md`.

## The ask

"Implement and fix everything" from the ideation. That document's own §7 orders it, and this plan
follows that order rather than the document's.

## What the ideation found, that the plan is shaped by

1. Five fleet bundles (`thermalsafety`, `kinetics`, `unitops`, `props`, `suitability`, 33 tools)
   are unreachable from every chart deployment — and declaring them the ordinary way costs 21,913
   tokens of prefix on **every** model call, which four `test_context_floor.py` entries already
   declined.
2. The prescriptive tier is bench-scale by construction: `_MAX_MASS_MG = 1_000_000` (1 kg),
   `_MAX_VOLUME_ML = 20_000` (20 L), and `ChargeLine` is three fixed-unit floats.
3. Nothing attaches a plate's results back to the design that prescribed them.

## Steps

- [x] **S1. Split declaring from binding.** `ConnectorManifest.default_enabled`, read in exactly
      one place (`registry.enabled()` when `connectors_enabled` is empty). Explicit lists are not
      filtered by it.
- [x] **S2. The declared basis for validators.** `declared_connector_tool_names`,
      `declared_tool_names`, `declared_skills_dirs` — so an opt-in bundle's skill is still
      validated on a checkout that never binds it. Runtime keeps the enabled basis.
- [x] **S3. Declare the five bundles**, `default_enabled: false`, manifest text carried from the
      fleet (authoritative there), each with a `README.md`.
- [x] **S4. The four skills that could not previously exist** — `thermal-safety-assessment`,
      `kinetics-and-reactor-choice`, `unit-operation-sizing`, `system-suitability`. `props` gets
      none: `skills/solvent-selection` already holds that judgment.
- [x] **S5. ADR** `D-2026-09-20-declaring-a-capability-and-binding-it-are-different-decisions`.
- [x] **S6. Tests** for the split: opt-in bundles stay out of `enabled()` and out of the measured
      prefix; an explicit list reaches them; a declared-but-unbound tool resolves for a validator
      and not for the runtime verifier. Five tests in `tests/test_connector_registry.py`.
- [x] **S7. Chart**: five `connectors.<name>` entries at `enabled: false`, plus the
      `networkPolicy.egressPorts` entry each one needs *before* anybody enables it. Rendered config
      unchanged for an existing release. helm/kubeconform/promtool installed, so the 73 chart tests
      that had been skipping actually ran.
- [x] **S8. Context-floor prose**: the allowance's membership rule is now two predicates —
      declared in both trees *and* bound by silence — and `_ARGUED_DIVERGENCES` carries nine rows.
- [x] **S9. Plausibility bands relative to the declared scale.** Scoped down from the plan's
      "`ChargeLine` on `Measurement`" — see the review below for why, and what it costs.
- [x] **S10. `rescale_protocol`** + `rescale_experiment_protocol` + the
      `protocol-scale-translation` skill.
- [x] **S4b. Six cross-capability skills** (not in the original list): crystallisation, solvent
      swap, impurity fate, analytical readiness, readiness review, robustness.
- [x] **S11. The design → results loop** — `experiment_arm_results` keyed by
      `(design, revision, arm)`, `attach_plate_results` and `read_plate_results`, with the
      observations handoff into a campaign.
      `D-2026-09-21-an-outcome-is-a-third-table-not-a-column-on-either-tier`.
- [x] **S12. The gate** a fleet template's arguments needed — the fleet's own recorded
      `tool-surface.json`, read offline. The first template is written and **parked**, because
      its launcher would cost every deployment prefix for a capability that ships off; the
      measurement and the ADR that would unblock it are the backlog row.
- [x] **S13. Design generators** — BoFire's `DoEStrategy` behind a `criterion` argument, not a
      second tool (`D-2026-09-21-a-design-is-a-criterion-not-a-second-tool`). Closes the
      `DEFERRED.md` row and the `BACKLOG.md` row, both deleted. Blocking and `NChooseK` are
      still open and still their own row.

## Verification

`make lint type test` green, plus every validator (`connector-validate`, `skill-validate`,
`prose-validate`, `template-validate`, `kg-validate`, `helm-validate`). Postgres-backed tests must
actually run — `sudo -n dockerd`, `make up`, `make db-migrate` — because a local run that skips them
prints green and proves nothing.

## Review

### What shipped

Four commits. The bundles and the declared/bound split; the scale-relative bands and the rescale
path; the three declarations a new bundle lands in; six skills and the four a new tool lands in.

**The finding that shaped everything**: the ideation expected to *add* capability and found the
capability already built and unreachable — 33 tools across five fleet servers, with a manifest in
this tree the only thing missing. The reason it had stayed missing was not oversight but price:
declaring them the ordinary way costs 21,913 tokens of prefix on every model call, and four
separate `test_context_floor.py` entries had already declined that trade on smaller numbers.
`default_enabled` is the whole change — one field, read in one place — and it exists because
*declaring* a capability and *binding* it are different decisions with different costs.

### What I got wrong, and what corrected it

- **The ideation said the prescriptive tier "refuses" a kilo-lab scale.** It does not;
  `quantities_are_plausible` is a warning. Corrected in the archived document rather than left to
  land, because the real defect is worse in a more interesting way: a warning that fires on correct
  input is how a chemist learns to stop reading the two checks beside it.
- **I raised `agent_context_token_budget` to hold the thread allowance whole.** The window pins
  that number, not the prefix, and `tests/test_compaction.py` records a previous branch trying
  exactly this and reverting it. The thread absorbs 1,400 tokens instead, and says so.
- **I ran the suite in parallel against an already-loaded box** and got nine failures, five of
  which were scheduling artifacts. `D-2026-09-13` documents that this happens and says to re-run
  serially before believing a parallel failure. It cost a triage pass that a serial run would not
  have needed.

### Three things a reviewer should push back on

1. **S9 is narrower than the plan.** `ChargeLine` still carries `mass_mg`/`volume_ml`/`amount_mmol`
   as fixed-unit floats rather than `Measurement`. The band defect is fixed and the field shape is
   not. Moving it touches stored JSONB revisions, `from_bo`, `render`, `export` and the
   `charge_is_consistent` arithmetic, which is a migration rather than a change, and it was not
   needed to close the defect. It stays the right long-term shape.
2. **Two ceiling raises in one branch**, totalling 1,400 tokens of thread allowance taken from
   every deployment — including every one that binds none of the five bundles. Both are argued in
   the file; whether the second (six skills) is worth it is the judgement call most worth
   challenging, and the cheaper alternative — bundling four of the six — was rejected because they
   genuinely span bundles.
3. **~~`SERVED_ELSEWHERE_ALLOWANCE` and `FLEET_PUBLISHED_ALLOWANCE` went unverified~~ — closed,
   and the way it closed is the point.** For most of this work both skipped, because measuring the
   fleet's schemas needs a built `.venv` in the sibling checkout and this one had none; the token
   figures were that file's own recorded measurements, cited as such. So the sibling was built
   (`make install` there, one command), and the four cross-repository checks now **run**: the
   allowances are measured against the real servers, `tests/conftest.py`'s "Cross-repository checks
   did not run" epilogue is absent from the final run, and the skip count went 7 → 3. The three
   left are an IPv6-less host and two surfaces declared not to be deployment surfaces.

   This is the same lesson as the 73 chart tests that had been skipping for want of `helm`:
   **a skip is not a pass, and in both cases the cost of turning it into evidence was one
   install.** What remains unverified is nothing — which is a different sentence from the one this
   row started as, and worth the two commands it took.

### Not built, and why

`attach_plate_results` (S11) is the loop-shaped gap the ideation calls out: a design reaches
`executed` and nothing attaches what the plate produced, so the plate → observations →
`suggest_next_experiment` round trip is handwork and the deferred mining of human protocol edits
has no corpus. It needs a table, a migration and a decision about whether results hang off the
design or off `reaction_records` — an ADR, not an afternoon, and a half-built version is worse than
none.

The step templates (S12) were started and stopped on a principle: a template's steps carry literal
argument keys, `make live-template-args` is the only gate that checks them against a running
connector, and this lane cannot run one. Writing argument names for five fleet servers I cannot
introspect is the fabricated-argument failure `D-2026-09-20-a-ranking-is-evidence-a-critic-is-not-
a-gate` refuses one layer over.

The design generators (S13) are self-contained and simply not done.
