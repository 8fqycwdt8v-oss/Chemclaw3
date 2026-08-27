# BACKLOG

The things worth doing next, highest-consequence first. Top = next.

**This is a queue of what is still open, not a log of what was found.** A closed item is **deleted**
from it in the commit that closes it; the commit is the record and `git log` is the history. Do not
strike a row through, do not append "**Done**" under it, and do not add a dated section explaining
that a row above has gone stale. That is exactly how this file reached 4,717 lines and 237 open rows
in twenty-one days, growing about three lines for every line removed — the same failure `DEFERRED.md`
had and D-154 fixed there with this one rule.

**Rows are grouped by what they ask for, not by which review produced them.** A finding's date and
its reviewing pass are provenance, and provenance belongs in
[`docs/archive/findings-2026-08.md`](../archive/findings-2026-08.md) — the long-form record of every
row this queue has ever carried. How many rows either file holds is a `grep`, not a sentence —
`grep -c '^- \[ \]' docs/archive/findings-2026-08.md` and the same over this file — and the two
counts do not subtract: promoting a row **restates** it, so a queued row is still open there under
its original wording, and matching the two sets by title matched only 7 of the 30 this queue held
when that was measured. §5 is the first thing here that is not a defect this repository found
in itself, and none of its rows is in the archive at all. The overlap is real and unmeasurable by
`grep`, which is why neither number is a difference. When a queued row needs its full measurement
history, that file has it under the review that found it.

Both counts *were* written here, and both were wrong — 223 against 221, and 41 against 45 — each
printed beside the command that disproves it, which is the whole argument of
`D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose`. A number nobody re-derives is a claim
about its author's afternoon; `tests/test_backlog_register.py` keeps one from coming back.

**A row must name an anchor in the tree** — a module, a line, a manifest key — so any row can be
checked with one `grep` instead of an argument. A row that cannot name one is not ready to be
queued.

**A row is a claim about the code, and claims go stale.** A 2026-08-17 pass opened every anchor this
file then held and found seventeen rows not workable as written: four described code that a merged
decision had already deleted or fixed, eight were misstated in a way that would have sent someone to
the wrong function, and three carried their own deferral trigger and belonged in `DEFERRED.md`. Two
stated the opposite of what the tree does — one pointed at a `DEFERRED.md` row that does not exist,
and one said a data-subject erasure route was missing while `make user-erase` implements it across
**twelve** tables with a dry run and per-table counts
(`python -c "from chemclaw.agent.leaver import _ERASE; print(len(_ERASE))"` → 12: seven always,
plus the checkpointer's three and the store's two, each skipped when the deployment has not created
it). **Before working a row, check it against `HEAD`**; if it is wrong, the fix is to correct or
delete the row, and that is as much a contribution as the code would have been.

Ten further rows arrived from concurrent reviews while that pass ran and are carried here unedited —
they postdate it and have not been re-verified against `HEAD` by anyone but their author, which is
exactly the state the paragraph above is about.

**And the pass that wrote the two paragraphs above is not exempt from them.** An audit later the
same day found its own numbers stale in the way it was written to catch: it said "nine tables" for
an erasure that clears twelve; it filed a hazard as unpinned that
`tests/test_connector_registry.py:293` had already pinned; and five of the anchors it wrote no
longer resolved to the construct they named by the time the branch was audited, four of them off by
a handful of lines. None of that is neglect — it is a measurement taken hours before the tree moved
under it, which is the failure mode this file has rather than an exception to it. Re-measure on the
way past; a row you had to correct before you could work it is a row that was worth opening.

Related registers: [`DEFERRED.md`](DEFERRED.md) (postponed with the trigger that would revisit each),
[`docs/decisions/`](../decisions/) (why the system is the way it is; its README indexes the record by
topic).

---

## 1 — Untrusted input reaching a privileged surface

- [ ] **The unauthenticated `X-Chemclaw-Actor` header becomes durable attribution** — [M], and
      **narrower than this row used to claim**. It does not reach `job_records` or the audit trail:
      the durable path takes the actor as an argument sourced from core's validated front-door
      principal (`ConnectorJobInput.requested_by`, `durable/connector_job.py:126` — the row named a
      field called `actor`, which does not exist), and never reads the header. The real reach is two
      columns on the synchronous MCP path — `bo_campaigns.opened_by` and `bo_suggestions.actor`, via
      `connectors/bo/server/tools.py:393`. The `unverified:<id>` marking is in place (D-2026-08-13),
      so what is open is that a caller still chooses the string. A bearer on the row above proves
      *core called*, not *which chemist*, so full closure needs an actor assertion bound to the call
      (OBO or a signed memo) — which is the `DEFERRED.md` warehouse row's blocker too.

- [ ] **`fetch_artifact` is a tool that can only refuse** — [S]. `list_artifacts` and
      `fetch_artifact` promise "the relaxed coordinates, the second derivatives, the raw vibrational
      spectrum" and "a conformer search seeded from a known structure", and two agent profiles
      (`computation`, `evidence`) instruct the agent to reach for them before proposing a rerun. In
      this repository the **only** artifact writer left is `ArrayOffloadingStore`, which writes
      `hessian.npy` and `dipole_derivatives.npy` — and `fetch_artifact` refuses binary by design.
      Measured: `list_artifacts` returns one `application/x-npy` entry and `fetch_artifact` on it
      raises "is binary … not text". `xtbopt.xyz`, `crest_conformers.xyz` and `vibspectrum` appear
      only in `science/calc/artifacts._MEDIA_TYPES` and in comments; their writers left with the
      engines in `D-2026-08-16-the-physics-leaves-the-cache-stays`. **The geometry half is now
      answered elsewhere** — D-2026-08-21's `structures` store is the addressable geometry the
      profiles were really asking for — so what is left is the *spectrum*: either the server returns
      `vibspectrum` as an artifact, or these two tools stop advertising one. Note the row below
      assumes `fetch_artifact` hands the model externally-produced text; today it cannot hand it
      anything.

- [ ] **No connector or MCP tool result is framed** — [M], wide half only. The two narrow channels
      are closed (`EvidenceChunk.source` is defanged, `recall_observations` frames its statements),
      and `agent/framing.py` is the pattern to reuse rather than reinvent — `frame_untrusted`,
      `defang`, `safe_id`, with a deployment-stable nonce. What remains is that **no connector
      result is framed at all**: `connectors/calc/server/tools.py`'s `fetch_artifact` hands
      arbitrary externally-produced text straight to the model, and none of the seven
      `wrap_tool_call` middlewares in `agent/langgraph_agent.py:594-631` is a framing one.
      This is ADR-sized rather than a patch: a middleware must not corrupt structured results
      (`ArtifactContent`, `EvidenceChunk`), so it needs a content-field convention first. The
      registry already answers "which tools are a connector's".

- [ ] **The stored-message conversion is a destructive in-place rewrite, run as a pre-upgrade
      hook** — [M]. `agent/message_migration.py:242` overwrites `session_messages.message` while its
      own docstring and `043_session_message_shape.sql:20` both promise the original stays readable.
      `migrate-job.yaml:10` runs it *before* any new pod exists, so it rewrites data the previous
      release is still serving with a reader that raises on the new shape — and `helm rollback`
      stays broken. Two separable halves: a preserved-original column (~15 lines), and moving the
      conversion out of the `pre-upgrade` Job into a `post-upgrade` one while the schema DDL stays
      where it is (~25 lines). Needs an ADR.

- [ ] **The four-repo lane puts the fleet's manifests on `CHEMCLAW_CONNECTORS_DIR`, which one
      manifest forbids** — [S], and it is the half of the now-closed live-lane row that did not
      close with it. `infra/live/e2e-full-stack/up.sh:185` adds `$MCP_REPO/manifests` to that path
      while `connectors/calc/connector.yaml:13` explicitly forbids it. It works only because
      `connectors/registry.py:124` is first-dir-wins (`found.setdefault`), and **that behaviour is
      pinned**: `tests/test_connector_registry.py:293` builds two directories each holding a bundle
      named `alpha`, on ports 7777 and 8888, and asserts the first directory's endpoint is what
      `enabled()` returns. So this is latent rather than broken — the lane depends on an ordering
      guarantee nothing in the lane states. Either the manifest stops forbidding it or the lane
      stops relying on the order.

- [ ] **`CalculationKey`'s primary key is an unescaped concatenation of caller-shaped strings** —
      [M]. `science/calc/store.py:122` (`CalculationKey.as_str`) builds the literal
      `calculation_results` primary key as `f"{calc_type}@{calc_version}:{input_hash}:{params_hash}"`
      (`infra/sql/001_calculation_results.sql:7`), and `calc_version` is not guaranteed free of `@`
      or `:` — `docs/decisions/D-2026-08-16-the-physics-leaves-the-cache-stays.md` gives real
      examples (`esol-delaney@2004`, `cal-0.28733:-29.3116`). Two different `(calc_type,
      calc_version)` pairs can serialise to the identical string (`calc_type="a", calc_version="b@c"`
      vs. `calc_type="a@b", calc_version="c"`); if the hash pair also matched, one calculator's
      `ON CONFLICT (key) DO UPDATE` (`science/calc/postgres_store.py:32`) would silently overwrite
      another calculator's cached row with a different `result`. The fix — deriving the key from
      `stable_hash` over the four components as a mapping, the way `molecule_hash`/`input_hash`
      already do — changes every existing row's key, which under D-011 ("never recomputed") is a
      full-cache invalidation on deploy; that trade needs an ADR and a migration plan, not a quiet
      change to `as_str`.

## 2 — Answers that are wrong without saying so

- [ ] **Two `Chemclaw3-mcp` changes this repository's half already landed against** — [S] each,
      and neither is fixable from here.
      `D-2026-08-27-a-gradient-is-the-evidence-a-frequency-set-cannot-carry` closed the Chemclaw3
      half of both; these are the halves that live in the other tree.
      1. `servers/calc/src/chemclaw_mcp_calc/engine/xtb_hessian.py::compute_hessian`'s size refusal
         ends *"Submit it through Chemclaw3's durable QM job path instead"* — a route
         `D-2026-08-26-semiempirical-is-the-whole-tier` deleted, and one that could not have helped
         anyway, since every durable job here composes that same primitive under the same ceiling.
         The same function's own docstring and the comment on `xtb_hessian_max_atoms` both say in
         the present tense that the wording was changed *because* there is no such path; only the
         message was not. It should state the server's own limit and stop naming a route — this side
         now refuses first and names `level="quick"` and a truncated model system. Paired change:
         `servers/calc/tests/test_engine.py::test_a_molecule_over_the_atom_limit_is_refused_and_says_where_to_go`
         matches on `"durable QM job path"` and its docstring says the refusal "names Chemclaw3's
         durable job path"; both have to change with it.
      2. `servers/calc/tests/test_key_contract.py` pins `CHEMCLAW3_EPOCH = "2"` and asserts
         `CALCULATION_EPOCH == CHEMCLAW3_EPOCH` under a docstring calling it "one constant with two
         homes". It is not one constant: `connectors/calc/remote.py::remote_key` **composes** the
         two — `H({"epoch": ours, "remote_params": <the server's params_hash, which already carries
         theirs>})` — so either may move alone and neither addresses the other's rows. The assertion
         is a coupling the code does not have and goes red on a legitimate one-sided bump; it should
         become a statement about the composition, or be dropped. This side's
         `tests/test_calc_remote.py::test_the_two_epochs_compose_rather_than_having_to_match` is the
         relationship that does hold.

- [ ] **Every structured tool result reaches the model as pydantic repr, not JSON** — [M].
      `langchain_core.tools.base._stringify` prefers `json.dumps(content)` and falls back to
      `str(content)`, which is what happens for every `BaseModel` this repo returns —
      `EvidenceSweep`, `NoteView`, `FingerprintSearch`, every connector model. Measured: an
      `EvidenceSweep` arrives as `chunks=[EvidenceChunk(content='…', source_note_id='…', …)]`.
      Two consequences worth separating. **`Field(exclude=True)` silently does nothing** anywhere in
      this tree — one design decision was already taken against the wrong belief about it
      (`D-2026-08-25-the-number-was-measured-on-a-path-production-does-not-use`), and the next one
      will be too unless the shape is known. And repr is a *guess* at a payload rather than a
      contract: nothing upstream promises it, which is why
      `tests/test_upstream_surface.py::test_a_pydantic_tool_return_still_reaches_the_model_as_repr`
      now pins it in that file's absence form.
      Not fixed in passing, because the blast radius is every tool at once and the question — should
      payloads be JSON, or should each tool render its own boundary as `condense_protocols` now does
      — deserves deciding rather than defaulting.

- [ ] **The `chem` enumerations and `compute_fukui_at` are served, pending that PR's merge** —
      [S], and what is left is a version bump rather than an implementation.
      `Chemclaw3-mcp#18` adds the six enumerations this repository's `chem` manifest declares plus
      the `compute_fukui_at` that `connectors/calc/compose.py::ensemble_property` calls, so the six
      templates `D-2026-08-25-the-loop-is-a-composite-not-a-template` added can complete. Delete
      this row once that PR is merged **and the live lane has run one of those templates end to
      end** — `make template-validate` cannot see the difference (`chem` is a bundle this
      repository declares and does not run, so its tools are name-checked and argument-unchecked),
      and `make connector-validate` against a running server is what would.
      **`transform_structure` was the seventh name and is now gone from the manifest** rather than
      implemented: it had no caller, no template, no skill reference and no documented signature in
      either repository, so serving it would have meant inventing its contract.

- [ ] **A solvate collapses onto whichever fragment is larger** — [M], and worse than filed: it is
      not only the cache key, it is the **knowledge-graph note id**. Measured,
      `standard_smiles("CCN.C1CCOC1")` returns THF, and `compound_id("CCN.C1CCOC1")` equals
      `compound_id("C1CCOC1")` — a solvated compound and its solvent become one note.
      `core/chem.py:206` `FragmentParent` keeps the largest fragment and
      `_identity_survives_stripping` guards only organometallics and reactive metals.
      *Blast radius, measured rather than feared:* the D-011 calculation cache is **unaffected** (it
      keys on `require_canonical_smiles`), fingerprints have a designed invalidation lever
      (`STANDARDIZATION_VERSION` → `std5`), **0 of the 68 shipped reagents change**, and the
      committed corpus has 9 compound notes with **0 multi-fragment**. The 68 is
      `core/reagents.py::_RAW_SYNONYMS` — 97 spellings over 68 distinct SMILES, 21 of them
      multi-fragment — named here because the row quoted the number without its corpus and an
      auditor looking for it found `data/vendored/records.csv` (35 rows) instead and read the row
      as invented.
      *What the same measurement turned up, and it strengthens the row:* three of those 68 —
      LDA, HATU and TBTU — **already** collapse today (`CC(C)[N-]C(C)C.[Li+]` →
      `CC(C)NC(C)C`, i.e. LDA recorded as diisopropylamine), and `data/vendored/records.csv`'s
      sodium tert-butoxide already becomes tert-butanol. Those are D-2026-08-01's counterion rule
      working as designed, not this defect — but they are why the candidate fix is stated as it is.
      Candidate fix: keep every fragment when ≥2 are organic. Measured, that changes **none** of the
      68 and leaves all three salt collapses in place, which is the point: it is a solvate rule, not
      a re-litigation of the salt one. Caveat that needs deciding, not assuming — the heuristic
      keeps the tartrate on nicotine bitartrate, which is arguably a salt; consulting the existing
      solvent table (`science/calc/solvents.py:44`) is the stricter variant.

- [ ] **A retracted ELN entry stays current evidence** — [M]. A withdrawn entry that simply
      disappears from the export is invisible to a cursor-based sync, so the note it produced keeps
      answering as current. `RawEntry` has no tombstone and the `ElnAdapter` protocol's two methods
      cannot express one. The *amendment* half already works (`ingest/eln/sync.py:201` — a body that
      is not byte-identical to the merged note falls through and is re-proposed); only disappearance
      is invisible. **The receiving end is already built** — `Note.valid_to`
      + `is_current(as_of)` — and `ingest/documents/sync.py:428 prune_share` is the same problem
      already solved for the share, including the three refusals that make a sweep safe ("an
      unreachable share and an empty one look identical"). Port that shape. Testable offline against
      a fake adapter; a real ELN is needed only to decide *which* mechanism the tenant offers.

- [ ] **Split-conformal uncertainty is unwired — and the function no longer exists** — [S].
      This row said `science/calc/uncertainty.conformal_uncertainty` "is correct and tested and has
      no caller". It has no *definition*: `uncertainty.py` records its deletion, and `Method` is
      `reported | propagated | none`. The row's framing is also wrong — it says wiring it must
      answer whether the interval attaches on the server, "which cannot see the ledger". It attaches
      here: `predict_solubility` (`connectors/calc/server/tools.py:660`) runs in this repo, holds
      the server's payload, and already calls `_log_prediction`; `reconciled_for` is one await away.
      So this is a re-add plus a call site (~100 lines), not a cross-repo decision. What it really
      needs is a policy answer: which predictors are calibrated enough to override their published
      RMSE. `calibration_conformal_coverage` / `_min_samples` come back with the caller.

**`reaction_fingerprints` keys on a bare reaction id, so two sources collide on one row.**
`science/fingerprints/store.py` writes `reaction_fingerprints.id = reaction.reaction_id` with no
source column, while `ingest/sources/eln-databricks/datasource.yaml` puts the source name into
`provenance` precisely "so two ELNs with colliding entry ids stay distinguishable in the graph".
Two sources using one entry id therefore share a fingerprint row and a `reaction-<id>` note id:
the second ingest overwrites the first, silently, and a similarity hit cites the wrong run. Found
while designing the label index, which uses a composite `(source, reaction_id)` key and does not
inherit it — so the fix is to give the fingerprint tables the same key, and it is a migration plus
`note_id_for_reaction`. Not urgent while one ELN is enabled anywhere; not detectable at all when
it happens.

## 3 — Work that is lost, dropped or invisible

- [ ] **A Hessian is cached and never published, and neither is the thermochemistry built from
      it** — [M], and the two halves are one question. `xtb.hess` is a `calc_type` the server
      stamps and `_CALC_TYPE_PROJECTORS` has no prefix for it, so vibrational frequencies never
      reach a results store; `ThermochemistryResult`, which is where a Hessian's scientific value
      is actually realised, has a projector but **no hook at all** — it is a *tool* composite, so
      it is neither written to the cache (composites are not cached, D-011) nor returned by a job
      envelope. Publishing frequencies therefore needs a third hook, not a projector, which is why
      this is a decision rather than a one-line addition. Named in
      `tests/test_publish_reaches_the_hooks.py::_PRIMITIVES_NOT_PUBLISHED` so the gap is declared
      and not merely absent.

- [ ] **A pinned template's arguments go unchecked once its bundle stops being ours** — [S].
      `cli/validate_templates.py` reads signatures from `connectors/<name>/server/tools.py`, so a
      bundle we declare but do not run has none — `hazard-briefing` calls `screen_hazards` and is
      name-checked only. The loss is *reported* (`unchecked_arguments`), which is what makes this a
      row rather than a defect. **This row's proposed fix location is wrong**: `make
      connector-validate` also has no live session — it imports the bundle's local module and
      returns `[]` for exactly these bundles — and it runs inside `ci`, which must stay offline. The
      check belongs on the **live lane**, opening real sessions via `open_connector_specs` where
      `BaseTool.args_schema` carries the names, as a new target beside `live-probes`.
      `make template-validate` stays offline and keeps the note.

- [ ] **A timed-out attachment parse still runs to completion** — [L], not [M].
      `agent/attachments.py:284` shields the future deliberately, so the timeout bounds the caller
      and the slot, never the thread — a hostile document holds a worker forever. The only real fix
      is a killable subprocess, with pickling and a new child-OOM failure mode to classify
      (~150-250 lines). **The cheap, honest half is separable**: `ingest/documents/sync.py:204` calls
      `asyncio.to_thread` with no `wait_for` at all, so one pathological file can hold the sync
      activity indefinitely; giving it the bound the front door already has is ~10 lines.

- [ ] **The digest is written to a mailbox with no reader, and the watermark advances anyway** —
      [L]. `durable/digest.py:146-166` writes to `session_events` under session id `digest-<owner>`
      with `kind="digest"`, and the only consumer in the tree is `GET /sessions/{id}/events`, which
      claims `kinds=("job_completed","job_failed")` and sits behind `resolve_session` — so it 404s
      that id and would filter the kind out anyway. Measured: the route's exact claim against a real
      `digest` row returned `[]` and left it unconsumed. `notify_session_best_effort` returns `True`
      on a successful *insert*, so `acknowledge_digest` fires and `mark_reported` moves the
      watermark past notes the subscriber will never see; `_is_new` can never re-qualify them, and
      `durable/retention.py:133` (`_PRUNABLE`'s `consumed_at IS NOT NULL` predicate) makes the
      orphaned rows immortal. The same
      dead end exists for `system-eval-drift`, whose must-deliver stance therefore guarantees
      delivery to nobody. Needs a route (`GET /digests` claiming `kinds=("digest",)`) — and until
      one exists, `digest_enabled` should plan no Schedule, since shipping the ack without the
      reader loses matches permanently rather than merely not delivering them.

- [ ] **The sixteen periodic workflows can still hang instead of failing** — [M]. The job path now
      declares `failure_exception_types` and `tests/test_workflow_registry.py` holds it
      (`D-2026-08-16-a-job-that-cannot-fail-is-a-job-that-hangs`), scoped deliberately: for a run
      nobody is waiting on, parking a redeploy bug until someone ships a fix is a defensible trade
      and the opposite of the one taken there. Decide it per workflow rather than by widening the
      test — retention and the memory jobs are the ones worth arguing about, since a parked run
      there is invisible in exactly the way the fan-out drop was.

- [ ] **`connector_job_timeout_seconds` bounds a 20-second job and a 24-hour job identically** —
      [M]. `core/config/connectors.py:71`: one global 90,000 s ceiling is the child's
      `execution_timeout` for every bundle, so if the `calc` worker is down a 20 s xTB job sits
      `running` for a day with no signal, while the setting is sized entirely by the QM path. An
      optional `JobSpec.timeout_seconds` applied as `min(declared, setting)` would let a bundle
      lower its own ceiling while the deployment keeps the maximum, leaving
      `_the_job_ceiling_covers_the_poll_it_bounds` untouched.

## 4 — Operating it

- [ ] **Settle `pytest-xdist` on a real runner** — [S].
      The `check` job is 87% one step: `make lint type cov` was **12m06s of a 13m56s job** on
      `d8c312a`, of which lint is 1s and type 68s (measured), so ~11 min is the suite itself.
      `D-2026-08-26-a-cancelled-run-on-main-is-a-missing-answer-not-a-superseded-one` took the free
      half — lint and type now run in parallel in `static` — and deliberately left this one open,
      because the evidence for it is a *reading* rather than a measurement.
      **What the reading says**: the suite looks parallel-safe already. `tests/pg.py` suffixes its
      `TEST_SCHEMA` with `os.getpid()` at import time, so an xdist worker gets its own
      Postgres schema with no change at all, and the two files that use Temporal go through
      `start_time_skipping()`, which binds an ephemeral port per environment. `pytest-cov` combines
      across workers natively, so the 84% floor survives.
      **Why it is not done**: "looks safe" is not a number, and the sandbox this was reviewed in ran
      the suite far slower than a GitHub runner does, so a local figure would say nothing about CI.
      The unknowns worth checking are tests that write into the repo tree rather than `tmp_path`,
      and whether four workers on a 4-core runner contend on the single Postgres service container.
      Closing this is one experiment: add `pytest-xdist`, run `-n auto` on a branch, compare the
      job's wall time and its failure set against the serial run on the same commit. If it is not
      a clear win, say so and delete this row.

- [ ] **Two of the four deployables have no chart, so a release changes their bytes and nothing
      else** — [M]. `D-2026-08-26-a-release-is-a-descriptor-and-a-target` deploys `Chemclaw3_ui`
      and each `Chemclaw3-mcp` server with `oc set image` against a Deployment an operator created
      by hand, because neither repository describes itself deployably: the fleet has seven
      `Containerfile`s and a per-server `networkpolicy.yaml`, the UI has a `Dockerfile` and a
      compose file for local work. That is the honest minimum — it changes the image and claims
      nothing else — and it means a release cannot move a port, a probe, a resource limit or an
      env var for either, and cannot create either from nothing. A chart per repository (or one
      chart for the fleet, whose seven servers differ only in name, port and token env) closes it.
      Not written from here, because doing so would be inventing somebody's Service, Route and
      limits; it wants one real namespace to be written against.

- [ ] **Turn the image scan back on, with its contradiction resolved** — [M].
      Carried forward unchanged from the SBOM work and re-confirmed by the 2026-08-26 CI review:
      `image.yml` now emits an SBOM and pins/verifies both binaries it downloads, but there is
      still no scan of the built image. It ran once, found three real classes of problem now fixed
      in `deploy/Containerfile`, and then reported two packages the build's own exhaustive
      filesystem listing says are not present. A gate whose last word contradicts the artifact it
      scanned makes every future red build ambiguous, so it goes back on with its own change rather
      than riding along on someone else's. The SBOM is now `main`-only, so a scan reading it is
      `main`-only too.

- [ ] **`read_corpus` re-reads the entire ELN from `datetime.min` on every call** — [M].
      `durable/memory_jobs.py:63` calls `fetch_new_entries(datetime.min)` on every ingest half, so
      each of the three memory jobs (`build_campaign_notes_activity`,
      `build_playbook_notes_activity`, `build_optimization_notes_activity`) walks the whole record
      from the beginning of time, and `all_reactions()` is called once per activity. On the two
      file-drop exports this costs nothing; against a real warehouse ELN it is a full table scan
      per activity per scheduled run. `ElnAdapter` (`ingest/eln/adapter.py:128`) has exactly two
      methods and neither is a fetch-by-id, so there is no cheaper read to reach for — closing this
      means either a fetch-by-id on the adapter protocol (every source pays) or a derived store of
      mapped `OrdReaction`s.
      **Found while building the protocol condenser and deliberately not fixed there**
      (`D-2026-08-25-the-structure-is-discarded-at-the-note-boundary` records the reasoning): a
      derived store would have answered it as a side effect, and answering a scaling problem as a
      side effect of a retrieval change is how a store nobody decided on gets built. It is also the
      trigger on the `DEFERRED.md` row for reagent/solvent set diffs in the turn-time comparison —
      one change answers both.

- [ ] **The results store has no live target** — [M]. `D-2026-08-25-a-cache-is-not-a-record` ships
      the whole path — `src/chemclaw/publish/`, the canonical schema in `schema/result-store/`, two
      drivers, the outbox (migration 050) and the drain — and it is proven end to end against a
      local Postgres running the shipped DDL (`tests/test_publish_sql.py`). What has not happened is
      an actual deployment pointing at an actual results database: `CHEMCLAW_RESULT_SINKS` is empty
      by default and `src/chemclaw/publish/sinks/postgres/sink.yaml` addresses a host nobody runs.
      Attaching one is configuration (`make sink-schema`, apply, set the variable), so this is a
      deployment action rather than code — but until it happens, no number below has been measured
      against a real corpus. `D-2026-08-26-a-route-is-not-a-shape` is why that matters more than it
      reads: the composite half of the path was inert for a release and no test noticed, because
      every test started at a projector rather than at a hook. A live target is the only thing that
      would have made it obvious.
- [ ] **Nothing has measured how many rows a real corpus produces** — [M]. The volume risk named in
      `D-2026-08-25`: `cached_compute` publishes on every miss, and a conformer search projects one
      record with ~47 conformer rows plus their structures. Before publishing is enabled by default
      anywhere, run `python -m chemclaw.cli.backfill_publications --dry-run` against a populated
      deployment and count rows-per-calculation per `calc_type`. That growth curve is also what
      decides the deliberately open question of whether `property_value` needs partitioning, and on
      what — a partition key chosen before the row count is known would be a guess.
- [ ] **Postgres and Temporal are neither deployed nor owned** — [L]. The chart dials
      `chemclaw-temporal-frontend.temporal.svc:7233` and namespace `chemclaw`; there is no subchart
      and no statement of who runs either. `docs/guides/runbook.md:925` states what this system
      *requires* of those stores and documents a Postgres restore procedure — what does not exist
      anywhere is tooling that performs or **verifies** a restore, and that cannot be built against
      a store this repo does not own. (The former separate "no backup tooling" row is folded in
      here; it was downstream of this one and overcounted the stores.)

- [ ] **The image vulnerability scan is not merged as a gate** — [M]. The runbook's false claim that
      it runs is corrected (2026-08-17) and
      `tests/test_deploy_chart.py::test_every_supply_chain_gate_the_runbook_names_actually_runs`
      keeps it corrected. **State the guarantee, not the implementation:** no supply-chain tool the
      runbook's §(xiv) claims — in the gate table *or* in the prose beside it — may be one that
      `image.yml` does not actually execute. It does not prove the named gate is *blocking*, only
      that something runs it. This row said it "fails if the runbook names a gate nothing runs",
      which was one degree stronger than the assertion then in the tree: the check was a substring
      over the workflow, so a comment naming the tool satisfied it, and only backticked table rows
      were read at all. Both holes were found and closed the same day — which is the argument for
      naming the guarantee rather than the mechanism, since the mechanism changed under this row
      within hours of it being written. The gate itself is still absent: `trivy`
      appears nowhere in
      `.github/workflows/image.yml`, which already builds `chemclaw:ci` locally on every PR, so the
      step needs no registry. Held for a stated reason — per D-2026-08-01 the candidate scan
      reported `setuptools` 70.3.0 and `msgpack` 1.1.2 while an exhaustive `find / -xdev` in the
      same build listed neither, and a gate whose last word contradicts the artifact it scanned
      makes every red build ambiguous. Re-check that against a current trivy before merging.

- [ ] **The background worker is a hard singleton** — [S], narrowed. The PR-gate half is closed:
      `kg/git_submitter.py::_cluster_lock` serializes submissions to one remote across pods through
      a Postgres session-level advisory lock whenever `session_store="postgres"`
      (`D-2026-08-27-the-gate-tells-the-truth-about-what-it-pushed`), and the host-local `flock`
      stays for the per-clone worktree sweep. What remains before `workers.background.replicas`
      can exceed 1 is an audit of the *other* activities that queue owns — ELN sync's cursor
      advance and retention's prune batches were written under a one-worker assumption and nobody
      has argued they are safe under two.

- [ ] **A durable deployment with no `framing_envelope_secret` silently loses the injection
      marking on its oldest content, and nothing says so** — [S]. `agent/framing.py::_envelope_nonce`
      falls back to `secrets.token_hex(8)` per process when the setting is empty, and the agent
      instructions say only an envelope carrying *exactly* the current tag marks retrieved content
      as data. With `session_store_dsn` set, a replayed thread carries envelopes written under a
      previous process's nonce: they no longer match, and that content is read as ordinary prose.
      `framing.py` claimed `Settings` warned about the pairing until 2026-08-26; it does not —
      `grep -rl framing_envelope src/` returns three files and none of them is a validator. The
      guard belongs in `core/config/__init__.py::_guards_that_the_comments_already_demand`, which
      is the right place and the reason this is not a two-line fix: every guard there *raises*, and
      raising would take down every existing durable deployment that has not set the value. So it
      needs a warning mechanism that section does not have — and a decision about whether the
      combination is an error at all.

- [ ] **Two credentials cannot be set through the chart at all** — [S], and the half of the
      settings-secret row that did **not** close with
      `D-2026-08-26-a-credential-is-a-type-not-a-convention`. `llm_fallback_api_key` and
      `temporal_api_key` are `SecretStr` fields with readers
      (`agent/llm_provider.py:251`, `core/temporal_client.py:74`) and no entry under
      `secrets.keys` or `secrets.optionalKeys` in `deploy/helm/chemclaw/values.yaml`, so
      `chemclaw.env` renders no `secretKeyRef` and a deployment has no supported way to provide
      them. The consequence differs per credential and that is what makes it a judgement rather
      than two identical additions: `llm_fallback_api_key` unset silently reuses the primary's
      key, which is correct for the common case (a second replica of one deployment) and wrong for
      a second vendor; `temporal_api_key` is Temporal Cloud only and the chart ships self-hosted
      with mTLS, so it may be right that it has no key — but nothing says so. Both go under
      `optionalKeys`, for the upgrade reason `framingEnvelopeSecret` already records.
      (`hpc_artifact_store_token` was the third and is gone with the tier that read it,
      `D-2026-08-26-semiempirical-is-the-whole-tier`.)

- [ ] **No session pagination and no per-session delete** — [M], **corrected**. This row claimed a
      data-subject erasure request "has no route across the seven tables". It does:
      `agent/leaver.py:161` (`_ERASE`) erases across **twelve** tables in one transaction with
      per-table rowcounts and a dry-run default, shipped as `make user-erase` — seven unconditional,
      plus the checkpointer's three (`CHECKPOINT_TABLES`) and the store's two (`_MEMORY_ERASE`),
      each skipped when a deployment has not created it. (The row said nine until 2026-08-17 — a
      hand count of a tuple whose length is seven literals plus two splats, which is why it is now
      `len(_ERASE)` and not as a reading of the source.) What is actually missing is (a) cursor
      pagination — `session_store.list_for_owner` truncates at `service_max_listed_sessions` with no
      cursor, so older sessions are unreachable, and (b) `DELETE /sessions/{id}`, which `leaver`
      does not offer because it is actor-scoped, not session-scoped.
- [ ] **`session_owners` and `session_turns` grow without any age-based disposal** — [S].
      `infra/sql/README.md`'s own `session_owners` row already flags this ("survives its session's
      pruned history; BACKLOG") but no row existed here to match it — this closes that dangling
      cross-reference. Neither table is in `durable/retention.py`'s `_PRUNABLE` set, and the only
      `DELETE` against either is `agent/leaver.py`'s manual, actor-scoped erasure — so every session a
      client ever created (the companion UI creates one on the first keystroke, before any message is
      sent) leaves a `session_owners` row forever, even after `session_messages` for that session is
      fully pruned by age. Needs a policy decision — prune once a session has no remaining
      `session_messages` and is past the retention window, or explicitly accept unbounded growth and
      say so — not a code change made unilaterally.

- [ ] **`observations_status_idx` does not cover the query it was built for** — [S].
      `infra/sql/025_observations.sql` indexes it `(status, last_seen DESC)`, with a comment saying
      "the retrieval bucket wants open observations newest-first" — but `memory/observations.py:122`
      (`_SELECT_OPEN`) actually sorts `ORDER BY cardinality(evidence_note_ids) DESC, last_seen DESC`,
      an expression the index does not cover. The index serves the `status='open'` filter only; every
      read still sorts all open rows in memory by an unindexed expression. Whether the fix is an
      expression index matching the real sort or a correction to which one is authoritative is a
      product call — the migration's stated rationale and the code that ships disagree about what the
      "newest and most-evidenced first" bucket actually orders by.

- [ ] **A jobs-only bundle has no reachability signal at all** — [M]. `connectors/health.py:81-99`
      derives its target from `health_url(manifest)`, which is `None` for a bundle with no
      `endpoint:` — so `results` reports `unprobed` whether its worker fleet is at two replicas or zero,
      `chemclaw_connectors_unhealthy` counts only `unreachable`, and `check_connectors_at_startup`
      raises only on `unreachable`. The fail-fast posture an operator opts into is structurally
      blind to the failure with the largest blast radius. `describe_task_queue(bundle_queue(name))`
      in the same sweep, reported as `unpolled` and counted like `unreachable`, is the runtime twin
      of the manifest check `connector-validate` now does — and it catches the row above too.

---

## 5 — Where the field moved past us

Filed by the 2026-08-25 field benchmark — see
[`docs/archive/REVIEW-2026-08-25-agentic-field-benchmark.md`](../archive/REVIEW-2026-08-25-agentic-field-benchmark.md)
for the measurements and the sources behind every figure here. These rows are unlike the four
sections above: none of them names broken code. Each names a place where something outside this
repository now has a **measured** better answer to a problem this repository solved earlier and has
not revisited. That is a different kind of debt and it needs its own section, because a queue that
only holds defects can only ever restore the system to what it already intended to be.

- [ ] **One merge added eighteen tools and 32% to what every turn costs** — [M], and it is the first
      thing the two new gates caught. The GFN multi-step work took the `default` profile's static
      prefix from **18,805 to 24,838 tokens** — measured by `tests/test_context_floor.py`, which
      landed in the same window and so reported a cost that had already been paid. Its ceiling was
      raised to 27,500 deliberately rather than the merge blocked, because blocking would have
      punished an unrelated branch; the figure and the cause are in the constant's comment.

      Two things are owed. **Eighteen probes**, because those tools are in
      `tests/test_probe_coverage.py::GRANDFATHERED` — a list that claims nothing, only records what
      predated the gate, and that the suite forbids growing. And a look at whether the eighteen need
      to be eighteen *advertised* names: `run_bond_strength_survey` and `survey_bond_strengths` sit
      beside each other on one surface, and `enumerate_*`/`run_*` reads like a primitive set that a
      profile could narrow rather than every turn carrying all of it. **Bringing the ceiling back
      down is the commit that proves that happened.**

- [ ] **A tool schema is 38% developer rationale, and it ships on every turn** — [M], and it is
      what `§ 5`'s deferral row turned into once measured. `science/bo/problem.py`'s nested models
      carry design arguments in their class docstrings — *"One `objectives` field rather than a lead
      objective plus a sidecar list (W3)"* — and Pydantic turns a class docstring into the schema
      `description`, so `convert_to_openai_tool` ships them. Measured 2026-08-25 on the `default`
      profile: `start_optimization_campaign` is 8,063 chars of schema, 4,392 of it description and
      **3,047 of that elaboration past the first paragraph**; `propose_knowledge_note` 4,259/2,262/663.
      Those two are 25% of the profile's 12,536-token tool budget between them, and both are already
      in `tests/test_context_floor.py::KNOWN_OVERSIZED`.

      **Not a blanket cut.** Some elaboration is genuinely the caller's — when to supply categorical
      descriptors changes what the model should send — so this is per-paragraph judgment: rationale
      moves to a `#` comment, guidance stays in the docstring. **And it does not ship until the live
      lane can show every probe still reaching its tool**, because a cheaper prompt that stops
      finding tools is a regression with a good-looking metric. Blocked on the live-lane row in § 1.

- [ ] **Half the probe corpus tests one tool** — [S]. `gather_evidence` is in `expects_tools` for 116
      of 232 probes; `find_notes` 91; `expand_note` 60; the tail is thin. Two consequences worth
      separating: the corpus mostly measures one retrieval path, and ChemToolAgent's finding — that
      tool augmentation **does not consistently beat the base LLM**, and hurts on general chemistry
      questions — cannot be reproduced here. Bucket C (51 probes) scores restraint but never runs the
      same question tool-free for comparison. `evals/ab.py::compare_tool_utility` is already written
      and already registered as `plan_execute_utility`; an A/B arm over bucket A is mostly wiring.

      **Blocked on a working model credential** (see §4), and the mock cannot stand in: `cli.mock_llm` emits scripted tool calls without *choosing* them in response to a question, so both arms of any comparison would measure the script. Measured 2026-08-25 through the real lane: expected-tool-reached 0/3.

- [ ] **No external benchmark has ever been run** — [M]. `make eval` gates 23 metric values over 14
      case files, a **7-document** retrieval corpus and a **39-note** knowledge graph, with the
      science half resting on one solubility value, one BO regret replay and two mass balances. It is
      honest and it is not comparable to anything. ChemRAG-Bench (1,932 expert-curated chemistry QA
      pairs) is the best first target because it scores the retrieval half — where this system's
      science actually lives — and it runs against an OpenAI-compatible endpoint, which is exactly the
      seam `agent/llm_provider.py` already has. ChemBench and AstaBench are the follow-ups. A number
      somebody else can also produce is the only kind that survives an argument with a chemist.

      **Blocked on a working model credential** (see the row in §4), and the mock cannot stand in: `cli.mock_llm` emits scripted tool calls without *choosing* them in response to a question, so both arms of any comparison would measure the script. Measured 2026-08-25 through the real lane: expected-tool-reached 0/3.

- [ ] **`deep-research` has no index behind it** — [M]. `agent/research_tools.py::gather_evidence`
      sweeps the knowledge graph, the ELN, the mounted document share and the fingerprint store —
      every one internal. `skills/deep-research/SKILL.md` describes a capability whose corpus is
      whatever notes exist (39 on this checkout). `Chemclaw3-mcp/MODULES.md` files `litsearch`
      (Europe PMC / OpenAlex / Crossref bulk, built at image time, no egress) as *proposed*, and says
      in as many words that it "gives Chemclaw3's existing `deep-research` skill a real index".
      ChemRAG measured **+17.4% average relative gain** from a chemistry corpus and — the design input
      that matters — that corpus choice is task-dependent: reaction prediction wants literature,
      nomenclature wants structured databases. A process chemist asking "has anyone run this coupling
      on a deactivated aryl chloride" currently gets whatever those 39 notes happen to say.

- [ ] **`pyexec` is merged in the fleet and unreachable from any deployment here** — **[M], not
      [S], and the sizing changed when somebody looked.** `Chemclaw3-mcp` #12 shipped
      `servers/pyexec` and `D-2026-08-25-a-sandbox-is-a-server-not-a-verb` records the decision, but
      `grep -rn pyexec` in this tree finds only that ADR and `tasks/todo.md`: no entry under
      `connectors:` in `deploy/helm/chemclaw/values.yaml:161`, so no `url`, no
      `networkPolicy.egressDestinations` host, and nothing telling an operator to provide
      `CHEMCLAW_PYEXEC_TOKEN`. The seam working as designed is why it is easy to miss — **zero core
      edits also means zero core changes to remind anybody.**

      **The obvious fix is wrong, and this is the part worth reading before starting.** Copying
      `chem`/`safety` means adding a manifest stub under `src/chemclaw/connectors/pyexec/`. But
      `registry.enabled()` is *"discovery is enablement until you say otherwise"* — an empty
      `connectors_enabled` loads every discovered bundle — so shipping that stub would:

      1. put `run_python` on the agent surface of **every fresh checkout**, by default;
      2. make the front door dial `127.0.0.1:8899`, which under the `connectors_required=true` the
         chart ships means **it refuses to boot** unless somebody is running the sandbox;
      3. trip `tests/test_probe_coverage.py` (no probe names `run_python`) and raise the context
         floor.

      Turning a code-execution tool on by default is a decision, not a wiring change, and (2) makes
      it a breaking one for every existing deployment. **So this needs an ADR about the default
      before it needs a diff**, and the branch point is whether `CHEMCLAW_CONNECTORS_ENABLED` stops
      meaning "empty loads everything" — which is a chart-wide behavioural change with its own
      blast radius. Whichever way it goes, the change also owes a `run_python` probe, an
      `egressPorts` entry for 8899 (the egress rule restricts by port independently of the peer
      list, so a destination with no matching port still drops), and the token obligation in the
      comment `chem` already models.

- [ ] **This environment's `API-KEY` comes and goes, and three rows are blocked exactly while it is
      down** — [S], and it is operational rather than code. Measured 2026-08-25:
      `anthropic.AuthenticationError: 401` with and without the session's `ANTHROPIC_BASE_URL`
      cleared. **Re-measured 2026-08-27: the same variable answers** (a haiku call returned 200; the
      day's verifier-margin run spent ~120 calls through it), so present-and-rejected is a *state*
      of this environment rather than a fact about it, and the worse case remains the stale one —
      it reads as a defect rather than as a missing credential.
      `tests/test_prompt_caching.py` probes reachability and skips with a reason naming which case
      it is, so the suite is honest about it. The *live* half of the eval plan (the bucket-C control
      arm, any external benchmark, grading any probe on the model's judgement) needs the working
      state and nothing else — probe first (`printenv 'API-KEY'`, one cheap call), then run the
      measurement in the same session, because tomorrow's state is not evidence about today's.

- [ ] **Memory records; it does not change what the next turn does** — [L], and it needs an ADR
      before it needs code. Six tiers exist and all six are *read on request*:
      `memory/campaign.py`, `interaction.py`, `failure.py`, `playbook.py`, `progression.py`,
      `observations.py`, surfaced by `recall_observations`, `find_past_jobs` and `record_failure`.
      Nothing in that set changes the agent's behaviour on the next turn unless a human writes a
      `SKILL.md`: `skills/playbook-distillation/SKILL.md` is the distillation *judgment*, and the
      PR-gate is where a distilled playbook becomes knowledge — but the loop is manual end to end and
      nobody has measured how often it closes. The 2026 work (SkillRL, SkillForge and the
      self-evolving surveys) is specifically about abstracting recurring trajectories into reusable
      procedure automatically. **The PR-gate is the right control for that, not an argument against
      it** — a proposed skill is exactly the shape the gate already carries. What is owed first is a
      measurement rather than a mechanism: over the sessions on disk, how many recurring trajectories
      *are* there, and would a distilled one have changed a later answer? A generator built before
      that number is a routing hypothesis nobody measured, which is the mistake
      `D-2026-08-15` already made once here.

      **Attempted 2026-08-25, and the corpus to measure does not exist.** Against a live Postgres:
      `session_messages` 12 (all from that day's own probe run), `session_turns` 0, `observations` 0,
      `note_proposals` 0, `audit_events` 3. The five notes under `knowledge/playbook/` are committed
      examples, not distillations of anything. So this row is blocked on **deployment history**
      rather than on effort — nobody can count recurring trajectories in a database that has never
      served a user. Its trigger is therefore a deployment with real sessions in it, and until then
      building the generator would be building against an imagined corpus, which is the row's own
      objection.

      **The measurement itself is no longer owed — the corpus is.**
      `D-2026-08-27-count-the-trajectories-before-building-the-distiller` defines the recurring
      trajectory, ships `make trajectory-census` (`chemclaw.cli.trajectory_census`), and states the
      greenlight numbers (≥5 recurring classes across ≥3 sessions, ≥1 would-have-helped multi-tool
      class); the command prints the verdict itself. Run 2026-08-27: 0 sessions, 0 turns, not
      greenlit. The day a deployment has sessions, this row is one command to check.

### The upstream-capability register — what our pinned dependencies now ship that we build ourselves

*Re-derived 2026-08-25, and re-derive it whenever a dependency is bumped.* `make upstream-check` and
`tests/test_upstream_surface.py` guard the *shapes* this repository borrows — the coupling that
breaks on a bump. Nothing guarded its **decisions** against upstream shipping the thing, which is
why the Temporal LangGraph plugin sat five weeks old and reached no list here. This is prose rather
than a test, deliberately: what is being watched is judgement, and a test cannot hold one.

Pinned at the time of writing: `temporalio` 1.31.0 · `langchain` 1.3.15 · `langgraph` 1.2.11 ·
`langchain-core` 1.5.5 · `deepagents` 0.7.6.

| Upstream ships | We | Standing |
| --- | --- | --- |
| `temporalio.contrib.langgraph.LangGraphPlugin` — graph nodes as activities, durable `interrupt()` | run two durability layers | **declined**, `D-2026-08-25-the-plugin-solves-an-interrupt-we-do-not-use` — we use no `interrupt()`; the human gate is already a Temporal workflow |
| `langchain.agents.middleware.ContextEditingMiddleware` / `ClearToolUsesEdit` | use it, on its own trigger since 2026-08-25 | **adopted** |
| `SummarizationMiddleware` | construct it switched off (`disabled_summarizer`) | **declined** — a summary is new model prose over content `agent/framing.py` marked untrusted, and the envelope does not survive it |
| `ModelCallLimitMiddleware` | subclass our own cap | **reverted**, `D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped` — measured, a cap of 2 ran 4 model calls |
| `ToolErrorMiddleware`, `ToolRetryMiddleware` | neither | **declined** — both trigger on raised exceptions and MCP tools never raise |
| `HumanInTheLoopMiddleware` | our own plan gate | **open** — its own row; the gate predates it and the shapes have not been compared |
| `deepagents.SkillsMiddleware` | use it, narrowed at the backend | **adopted**, with the narrowing on the backend because deepagents publishes skill *paths* into the prompt |
| `deepagents` `execute` filesystem verb | withhold it | **declined**, and answered elsewhere — `D-2026-08-25-a-sandbox-is-a-server-not-a-verb` puts the capability in the fleet instead |
| LangSmith tracing | first-party OTel + OpenInference | **declined** — proprietary, no OSS self-host, and its core value is prompt/response content in a third party |

**How to use this.** On a dependency bump, read the release notes against the middle column and ask
one question per row: *does upstream now do this, and better?* A row that changes answer needs an
ADR, not an edit here. A capability upstream ships that this table does not mention is the gap this
register exists to catch — add the row in the same pull request that notices it.

- [ ] **Nothing watches for upstream shipping a decision we made ourselves** — [S], and it is the
      meta-row the four above are instances of. `make upstream-check` guards the six *shapes* this
      repo borrows against a dependency bump — the coupling that breaks. Nothing guards its
      *decisions* against upstream shipping the thing: the Temporal LangGraph plugin row above is five
      weeks old and reached no list here. `tests/test_upstream_surface.py` is the right precedent
      (two of its assertions are *absences*, so upstream fixing something turns the workaround red).
      The cheap version is a dated section in this file, re-derived when a dependency is bumped,
      listing what each pinned upstream now ships that this repository implements itself.

---

## The turn-time comparison cannot diff what the ELN gives structured

On a prose-only ELN the *mined* `optimization-campaign` note produces excellent condition deltas —
`solvent DMF → 2-MeTHF`, `reagent cesium carbonate → potassium carbonate` — because
`memory.progression.changes_between` reads the species set of each role off `OrdReaction.inputs`.
The **turn-time** comparison cannot: `agent.condense._changes` diffs `ProcessConditions` plus the
solvent its prose reader extracted, and `reaction_records` keeps `reaction_id, body,
compound_smiles, project, performed_at, conditions, source` — the component list survives only as
prose inside `body`. So on the one schema where the components are the *most* reliable thing the
source provides, the artifact a chemist is answered with in the turn is the one that cannot use them.

Measured (`D-2026-08-26-silence-is-not-a-successful-run`, four runs): the mined note named all three
swaps; the turn-time table rendered `—` in every "Changed vs previous" cell.

Nothing is wrong today — the campaign note is retrievable, `experiment-progression` already starts
from it, and the two artifacts together answer the question. What is unresolved is whether the
deterministic delta should be available without a mining pass. The cheap shape is a column on
`reaction_records` carrying the per-role canonical species sets (a projection, not the charge list,
so it stays a serving copy rather than a second record); the expensive one is handing `Protocol` a
component list, which `agent.condense` deliberately does not have because a share document has none.
Wants its own ADR and a measurement of what the extra column costs on a real corpus.

## Everything else

223 open findings live in [`docs/archive/findings-2026-08.md`](../archive/findings-2026-08.md)
(`grep -c '^- \[ \]'` on that file), grouped by the review that found them, with their full
measurements. That set **overlaps** this queue rather than extending it — promotion restates a row,
so a queued row is still open there under its original wording, and the header's "~185 further"
was a subtraction nobody could reproduce. They are open, not abandoned — promote one into the queue
above when it becomes the next thing worth doing, and delete it from here when it is done.

The large multi-item programmes that used to be tracked here as sections are records now, not
plans: the F0–F9 foundation build, the F10 parity pass, the F11 gap closure, the BO capability
roadmap and the xTB/QM (X-series) roadmap. Their remaining live edges — real Temporal broker, real
cluster, a real Databricks workspace — are in
[`DEFERRED.md`](DEFERRED.md), each with the trigger that would revisit it, which is the register
those belong in.

## `turn_cost_ratio` scores a fixture, not the system

`data/evals/cases/autonomy-turn-cost.md` carries literal turn records, so the metric returns
0.9845458333333333 whatever changes in the agent — the 32% static-prefix growth that
`tests/test_context_floor.py` caught would leave its `baseline.json` row untouched. The metric's
arithmetic is right and tested; what is missing is a case fed from real recorded `TurnCost` rows.

Blocked on the same thing the memory-distillation row is: a deployment with turns in it. This
system has 12 session messages and 0 recorded turns, so there is nothing to build the case from
yet. Trigger: the first live lane run that persists a session's worth of turns.

## The live lane and the four-repo lane fight over `chem` and `safety`

`infra/live/processes.sh` uses `RUN_DIR=$LIVE_DIR/run` and `infra/live/e2e-full-stack/up.sh` uses
`$LIVE_DIR/e2e/run`, and both now start `chem` and `safety`. Run them together and the second
lane's pidfile guard cannot see the first's processes, so two uvicorns die on a bound port while
`wait_for` passes off the servers that are already up — leaving dead pidfiles that make
`processes.sh status` report both DOWN while the lane works fine.

Not fixed here because the fix is a decision rather than an edit: either the two lanes share one
run dir (and one lane learns to adopt the other's processes), or the fleet bundles move out of
`processes.sh` and the four-repo lane becomes the only thing that starts them. The second is
probably right — `processes.sh` grew them for a single-repo live test that the e2e lane supersedes
— but it changes what `make live-up` alone can exercise, which wants measuring first.

## Recover the flow-Suzuki screen, or decide it stays out

`Chemclaw3_mock` seeds 10,011 ORD records and **5,760 of them — 57% — cannot be ingested at all**.
Every refusal is the Perera flow-Suzuki set (*Science* 2018, 359, 429), whose second coupling
partner the source spreadsheet publishes only as its own shorthand (`2a, Boronic Acid`).
`ord_adapter._smiles` refuses rather than inventing a structure, which is right and is pinned by
`test_ord_compound_with_no_resolvable_identifier_is_still_refused` — but that docstring's own words
are "57% of a real corpus lost, including the yield data on components that *were* resolvable",
and the widening it documents (INCHI, then NAME through `resolve_compound_name`) moved the number
from 5,761 refused to 5,760.

The open question is whether a reaction with one structure-less participant is worth keeping as
*evidence*: its yield, ligand, base and halide are all real, and questions like "which base wins on
this halide" need none of the missing structure. The two candidate shapes are (a) a `Component`
that may carry a name instead of SMILES, with the reaction excluded from every fingerprint index,
and (b) a separate lower-tier record type that retrieval can cite but similarity cannot reach.
Both change what a `Component` is, so this wants its own ADR and its own measurement of what a
partially-structured reaction does to retrieval — not a patch to `_smiles`. Measured and declared
by `make live-data`; see `D-2026-08-18-a-corpus-is-not-reachable-because-it-is-on-disk`.

## Make an ingest rejection answerable instead of only logged

A record refused on ingest leaves a WARNING and nothing queryable. The seeded corpus has exactly
one such record — `santanilla-orgsyn-boronate-well-Y36`, at 119.43%, refused because `OrdReaction`
bounds a yield at 100 — and a chemist who asks about it can be told only "I have no such record".
The better answer exists and is unreachable: *"that well was rejected on ingest because a yield
cannot exceed 100%; the value is what an uncalibrated relative-UPLC readout does."* A durable
rejection ledger (entry id, source, reason, first and last seen) would make data-quality questions
answerable and would give `warn_late_arrivals`' aggregate a place to live besides a log line.
`gr-08` is written against the absence today and says in its own comment what it becomes if this
lands. Found by the 2026-08-18 corpus-fidelity pass.

## The PR-gate costs 1.81 s per proposed note, and a backfill is one note per record

Measured over the ORD backfill: 103 records per 3.1 minutes, steady, with the cost in the PR-gate's
git branch-and-commit cycle rather than in mapping (the whole 10,011-record corpus maps in 0.3 s).
That is a little over two hours for the mock's 4,251 ingestible records and 4,251 branches in the
note repository. A real deployment's first sync is a decade of records, where this is days and a
repository nobody can list. Nothing is broken — every proposal genuinely is a reviewable unit — but
a backfill and an incremental sync arguably want different submission shapes (one branch per batch,
or a bulk proposal a reviewer expands). Found by the 2026-08-18 corpus-fidelity pass.

## A revoked credential fails the two live prompt-caching tests opaquely

`tests/test_prompt_caching.py` guards its two live tests on `"API-KEY" in os.environ` — that the
variable is *set*, not that it *works*. With a revoked key both fail several frames deep inside the
Anthropic client with a raw `AuthenticationError`, so `make test` goes red in a way that reads as a
prompt-caching regression. Observed 2026-08-18: the environment's key is well-formed, present, and
answered `401` by the API.

Skipping on an auth error is the wrong fix — it would hide a real outage, and these tests exist
because a belief about caching was measurably wrong once. The right one is a message that names the
cause, so a reader learns the credential is dead rather than that the cache broke. Same distinction
`D-2026-08-17-a-harness-that-starts-two-of-five-servers-is-a-harness-that-tests-two` draws about
`/readyz`: holding a credential is not the same as holding one the other side accepts.

## Surface `invalid_tool_calls` — an unparseable tool call is currently a silent no-op

LangChain puts a tool call whose arguments do not parse into `AIMessage.invalid_tool_calls` rather
than `tool_calls`. Nothing in `src/` reads that field, so the agent — which iterates `tool_calls` —
drops the call without a `tool_failed`, a `tool_result`, or any other trace. Proven outside the
stack via `langchain_openai.chat_models.base._convert_dict_to_message`: truncated arguments yield
`tool_calls: []` and a populated `invalid_tool_calls` carrying the parse error.

A truncated argument document is what a real model emits when a stream is cut or a token budget
runs out, so this is reachable in production, not only under the storm's mock. With no prose the
turn ends as `empty_answer`; **with prose it proceeds as though no tool were needed**, which is
`D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed` exactly.

Fix in the middleware chain: after the model call, convert each `invalid_tool_calls` entry into a
visible failure the model can also correct from. Do not jump from `after_model` — see
`D-2026-08-15-an-after-model-counter-is-a-counter-that-can-be-skipped`. Verify with
`make live-storm`'s `f-malformed-json` check, which currently fails. Found by the 2026-08-17
full-stack run — see `tasks/live-test/full-stack-e2e-2026-08-17.md`.
