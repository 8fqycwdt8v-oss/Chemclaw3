# D-2026-09-07-a-driver-with-no-caller-is-not-a-capability — five unreachable paths deleted or relocated, and the map guard that could not see the tree it maps

**Status:** accepted · **Date:** 2026-09-07 · **Builds on:**
D-2026-08-27-a-hold-nothing-can-open-is-not-a-hold (the predicate: a thing no *configuration* can
reach is dead; a thing a *deployment* selects is not), D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution
(an absence gets a test), D-2026-08-15-a-claim-is-a-mutex-not-a-line-edit ·
**Supersedes** the "the dead-ish `science/bo/campaign.py` was not deleted" paragraph of
D-2026-08-27-a-bound-that-multiplies-and-a-record-that-survives-the-cancel, and the webhook-starter
warrant D-2026-08-27-a-periodic-job-decides-for-itself-whether-a-bug-should-park-it gave
`NoteReindexWorkflow`'s failure declaration.

## Context

A dead-weight audit ran six AST passes over `src/`, checking every candidate against
`connector.yaml`, `datasource.yaml`, `sink.yaml`, `channel.yaml`, the `SHIPPED` provider dicts, the
Makefile, `deploy/entrypoint.sh` and the Helm chart — because this repository resolves half its
capability by `module:callable` and a Python-only scan reports every manifest-resolved driver as
dead. What it found is not volume; it is a pattern this tree has now deleted four times under four
names (`map_to_hpc_identity`, `reject_widening`, `set_current_specialist`, `effects_for_session`):
**code that reads as capability, is maintained as capability, and no configuration can enter.**

The expensive instance is the one that makes the case. `science/bo/campaign.py` had zero `src/`
callers and said so in its own docstring — and the day before it was reported, a session measured a
**16,069.7 ms event-loop stall** inside `optimize` and threaded two BoFire calls to fix it. Real
engineering on a path no process can enter, and the fix's measurement paragraph then reads as
evidence the function is live. That is the whole cost of this class: not the lines, the attention.

The same audit's structural half found the map guard is one level deep while `ARCHITECTURE.md` says
it is not.

## Decision

**1. `science/bo/campaign.py` is deleted, and its loop moves to `tests/bo_harness.py`.**
`D-2026-08-27-a-bound-that-multiplies-and-a-record-that-survives-the-cancel` kept it, and its
objection was right and is answered rather than overruled: deleting it would have *inlined* the same
ask/tell loop into three test files, which is worse duplication than the one it removes. One
definition, in the tree whose callers are real, costs nothing and leaves nothing unenterable in the
shipped package. What ships is `BoCampaignWorkflow`. `objectives.molecule_library_problem` moves
with it for the same reason — and its three live citations are rewritten to argue from
`solubility_max`, a *registered* objective whose contract (`params[MOLECULE_KEY]`) forces the very
shape the deleted builder built, so the campaign-identity casefolding rule now rests on the shipped
path instead of on an unreachable one.

**2. `agent/durable_tools.request_note_reindex` is deleted.** It was half-removed a week ago —
`tests/test_service.py` records stripping the name from `api/app.__all__` because no route read it
and its only starter was a merge webhook this app does not serve — and the function stayed, with a
present-tense docstring about a git host that "can deliver several within seconds".
`NoteReindexWorkflow` is alive on its Schedule, so the capability is untouched.

**Its deletion moves a warrant, and the stance stays.**
`D-2026-08-27-a-periodic-job-decides-for-itself-whether-a-bug-should-park-it` decided that workflow's
`failure_exception_types=[Exception]` *on the webhook starter* — an unbounded per-minute run whose
park would be immortal. Schedule-only, that argument is gone; the declaration keeps its place on the
argument that same ADR records as having replaced the visibility case,
`D-2026-09-04-a-schedule-that-cannot-report-an-outcome`: a failed scheduled run reaches an operator
through `last_outcome` and a parked one reaches nobody, while hybrid retrieval serves the stale index.

**3. `science/bo/engine.predict_at` is deleted.** A one-statement wrapper over
`interrogate_surrogate` with `assess_fit=False` baked in, zero `src/` callers, and the shipped tool
(`predict_outcome`) calling the wrapped function directly — so the tree carried two spellings of one
question with no way to tell which the system used. Its empty-`points` guard is not lost:
`interrogate_surrogate` already refuses a call asking for neither a prediction nor a fit.
`tests/test_bo_predict.py`'s 667 lines now drive the spelling the connector runs.

**4. `durable/effect_ledger.get_effect`/`unsettled` stay, and the fork is closed by saying so.**
The audit offered expose-or-delete and forbade "leave it"; the resolution is neither arm and is a
decision rather than a shrug. They are the write path's read-back under test — deleting them puts
raw SQL in a test instead of removing a claim, which `tests/test_effects.py` argued a week ago — and
`effects_unsettled_idx` is *partial* on `state = 'attempting'`, so it holds in-flight rows only and
a never-pruned table does not pay for it. The false present tense the audit was reacting to had
already been corrected on 2026-09-06. What was still missing is a check: the module says "served by
no route, CLI or tool", and that sentence is now enforced *in the direction it can go wrong* — a
test that fails when an operator surface starts serving the set without rewriting the sentence.
`unsettled`'s docstring carries the query an operator runs by hand, so the absence is actionable.

**5. `create_app`'s four injection parameters stay; the prose around them changes.** They are a test
seam no setting, manifest or chart value can supply, and `graph_factory` is what makes the whole
HTTP surface drivable without a model credential. What was wrong is that `GET /sessions` and the
plan inbox described their non-`SessionOwnerStore` branches as *a deployment's* registry — an
operational caveat about a deployment that cannot exist, inviting an operator to hunt for a knob.
The 422 detail loses the word "deployment" with it.

**6. The map guard becomes recursive over Python packages, and `ARCHITECTURE.md` states exactly
that.** `_tracked_directories(_PACKAGE)` read direct children only and `_ROW` cannot match a slash,
so a nested subpackage needed neither a README nor a map row: measured, adding
`src/chemclaw/retrieval/rerank/` with neither left all fifteen tests green, while the same files one
level shallower failed four. **38 directories** sat in that gap — every connector bundle but
`results`, all three `science/` engines, `publish/{drivers,sinks}`, `deliver/channels/*` and all ten
`ingest/sources/*`.

The two obligations get **different scopes**, stated rather than conflated:

- a **map row** for top-level directories and *direct* subpackages of `src/chemclaw/` — the two
  tables, and nothing deeper; a nested package is documented by its own README and its parent's, not
  by a third table restating them;
- a **README** in every directory under `src/chemclaw/` holding Python modules, at any depth.

A manifest-only folder is out of scope on purpose: `ingest/sources/*` is ten near-identical
`datasource.yaml` folders described by the seam's own README, and a broad rule nobody satisfies is
worse than a narrow one everybody does. Twenty READMEs were owed under the narrow rule and twenty
are written.

**7. The one corpus inside `src/` stays, named.**
`science/bo/benchmarks/data/reizman_suzuki_case_1.csv` is package data pinned to the surrogate that
reads it — `objectives._reizman_suzuki` registers the fit under a name a `CampaignSpec` carries, so
swapping the file silently changes what that name means, which is exactly what an operator-settable
corpus may do and this must not. `data/vendored/` is a `DataSource` with a manifest contract
(checksum, licence, `text_column`) that a benchmark's training grid does not have. The reverse
direction of "`data/` holds every corpus" was enforced by nothing; it is now, with this one file
enumerated, so a second arrival is a decision rather than a precedent.

## What would notice

Four absence tests, each watched failing before it was kept:

| guard | fails when |
|---|---|
| `test_bo.py::test_the_in_process_campaign_loop_has_no_definition_under_src` | `optimize` or `molecule_library_problem` is defined under `src/` again |
| `test_durable_tools.py::test_no_workflow_starter_here_is_reachable_from_nowhere` | any `start_workflow` launcher in `agent/durable_tools.py` is named nowhere in `src/` or `tests/` — identifiers **and** string constants, so a `module:callable` counts |
| `test_effects.py::test_no_operator_surface_serves_the_unsettled_set_without_saying_so` | `api/`, `cli/` or an agent tool module starts reading the ledger while the docstring still says none does |
| `test_repo_map.py::test_no_corpus_lives_outside_data_except_the_one_that_is_argued` | a second corpus appears under `src/` |

`predict_at` needed none: `tests/test_bo_predict.py` fails to import without it, which is the
loudest possible signal, and after the rewrite that file is a *better* net than before because it
covers the code the connector actually runs.

## Consequences

Roughly 200 lines leave `src/`; 20 READMEs and one test-support module arrive. The BO suites are
unchanged in what they assert and changed in what they drive.
`test_event_loop_offload.py`'s campaign arm was retargeted from the deleted in-process loop onto
`connectors/bo/activities.py`, so the 2026-09-06 threading measurement now guards the two calls a
chemist's campaign actually makes — and it was mutation-checked: with the `to_thread` hops removed
it fails at 150.8 ms against a 150 ms ceiling.

## The pattern worth carrying

Three of these seven are one shape: **a gate, a guard or a claim that is green while checking
something narrower than the sentence beside it.** The map guard checked one level and promised all
of them; `test_service.py` checked an export list and read as having removed a function; the
effect ledger's index was justified by a reader nothing could reach. In each case nothing was
failing, which is why each survived several sweeps. The remedy is the same every time and it is not
more prose: make the *scope* of the check and the scope of the sentence the same scope, and where a
sentence asserts an absence, write the test that fails when the absence ends.
