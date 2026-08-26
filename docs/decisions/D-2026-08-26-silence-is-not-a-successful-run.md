# D-2026-08-26-silence-is-not-a-successful-run — a value the source could not supply is not a value it supplied

## Status

Accepted, 2026-08-26.

## Context

A user story — *"summarise the activities on reaction abc; where did development start and why was
it altered; list every change on a timeline with its rationale; recommend where to go next"* — was
put to this system against the ELN it will actually run on. That ELN delivers **two structured
things, the materials charged and the reaction SMILES, and one free-text cell** holding the
protocol, the observations and the run's initial hypothesis. There is no experiment-date column, no
temperature column, no yield column and no status column. A project code exists and is reliable only
on well-kept projects; the reaction SMILES is the grouping key.

Measured against that shape, on four runs built with structured components and prose only and put
through the production `optimization_campaign_note`:

- Every condition delta was **correct** and read entirely off structured data — solvent, base and
  substrate swaps. The timeline's spine survives a prose-only ELN, because DRFP clustering and
  `changes_between` read the two things that arrive structured.
- **All four runs came back `outcome_class: success`**, with nothing having said so.
- **No run carried a date**, so `Progression.is_timeline()` was false and the note correctly refused
  to narrate a trajectory at all — while `RawEntry.created_at` sat in every one of those rows,
  *required*, because the sync watermark cannot advance without it.
- Temperature, time and yield rendered as a column of dashes on every row.

Three separate defects, one shape: **a value the source could not supply was indistinguishable from
a value it did supply.**

Two structural findings sit underneath them. `map_to_ord` has **six call sites and no shared
downstream**: the durable sync validates through `validate_ord`, and `durable.memory_jobs.read_corpus`
— which builds the optimization-campaign note, the artifact this whole story asks for — validates
nothing. And `ingest.eln.validate.main`, the gate whose stated job is checking a declaration against
the live surface, constructed `JsonExportAdapter` and `OrdJsonAdapter` **by name** and never asked
the registry what was attached, so an ELN wired the supported way (D-120) was outside it and the gate
printed `OK` regardless.

## Decision

**1 · `OrdReaction.outcome_class` becomes optional; `None` means the source did not say.**

The SUCCESS default was argued for on compatibility grounds — silence had always meant an ordinary
run, and reinterpreting it would retroactively weaken the corpus. That argument holds for a status
column that happened to be null. It does not hold for a source with no status column at all, where
the default makes **every record** assert a success nobody claimed, silently, on the one field whose
entire purpose is that a failure must not read as an ordinary run.

`None` is deliberately **not** folded into `INCONCLUSIVE`. That value means the run carries no
evidence about the chemistry — aborted, mis-charged, never assayed — which is a statement somebody
made. "Nobody has read the prose yet" is a different fact, and collapsing the two teaches the corpus
something untrue: the same argument `OutcomeClass` already makes for keeping INCONCLUSIVE apart from
FAILURE.

Two consequences, both accepted:

- `find_playbook_candidates` distils only from stated successes, so **a source recording no outcome
  distils no playbooks.** That is the honest answer — a playbook says "this works", and building one
  from runs nobody assessed is a claim built on silence — and it is the reason such a site has to
  supply the outcome rather than a reason to keep the default.
- `mine_failure_observations` had to stop asking `is not SUCCESS`. A negated test sweeps every
  unassessed run into "unsuccessful", and that function's output is a sentence counting how often a
  transformation failed: it would have read an unread corpus as a corpus of failures. It now names
  the two stated non-successes.

A stated success is now written to `ProcessConditions.outcome` as `"success"`, which it could not be
before: the renderer omitted it precisely to avoid asserting a success the ELN never claimed, and the
cost was that a chemist's recorded success and an unassessed run were the same `None`.

**2 · The entry's own timestamp is the floor for `performed_at`, applied once at the registry.**

`DatedIngest` wraps an ingest half and fills `performed_at` from `RawEntry.created_at` when the
adapter left it unset. An adapter that knows better always wins; both file-drop adapters already map
it and are unaffected.

It goes at `ingest.sources.registry._build_ingest_half` because that is the **one construction point
both production readers share** — `make_data_source` for the sync, `active_ingest_sources` for the
miner. Putting the rule in either caller means the other silently does not get it, which is the shape
of the defect being fixed.

**The filled-in date carries its provenance, and the first draft of this decision did not.** A
record-creation time is when the entry was *written*, not necessarily when the run was performed —
and sometimes three weeks of bench work transcribed in one afternoon. That draft asserted the
weakening was "what `ordering_caveat` already exists to describe". Reviewing the change against the
code found it was not: that function distinguished *missing* dates from present ones and knew
nothing about where a present one came from, so a filled-in date turned `Progression.is_timeline()`
true and the campaign note claimed "Runs in the order they were performed" over an afternoon of
typing. **A value the source could not supply reading as one it did — this decision's own subject,
reintroduced by its own fix**, and asserted in prose rather than checked, which is the second failure
this repository keeps a rule about.

So `OrdReaction.date_source` records whether the date was stated or filled in, `DatedIngest` stamps
`"entry"`, and `ordering_caveat` says "Runs in the order they were **recorded** … not proof of the
order they were run" for such a series. That is the `DigestSource` pattern a third time in one
change, and the reason it keeps recurring is that it is the same problem each time.

None of it licenses causality: `memory.progression`'s rule that a date proves sequence and never
response is untouched, and is more load-bearing here than before.

**3 · The prose reader is asked for the run's intent, and marks it as read.**

`agent.condense._Extraction` gains a `hypothesis` field and the comparison gains a **"Tested (read)"**
column, dropped when no protocol stated an aim.

This does not overturn `json_adapter`'s refusal to pattern-match a hypothesis out of a procedure —
it is what makes that refusal affordable. The objection there is not to reading prose; it is to
producing a value that lies about where it came from, "indistinguishable, downstream, from one the
chemist wrote". Here it cannot be: the row carries `digest_source: extracted`, the header says
"(read)", and `evidence_excerpt` quotes the sentence. The extraction is deliberately narrow — this
corpus is free-form, with no `Objective:` convention to key on, so anything short of an explicit
statement of aim returns null, and inferring a purpose from the conditions that changed is exactly
the causal fabrication the design refuses.

**4 · A binding may name the column holding the intent; it may not carve one out of prose.**

`IngestBinding` refuses the transforms that can put text in `hypothesis` which the source cell does
not hold — `regex`, `value_map`, `default` — through fallbacks as well. Normalising transforms
(`strip`, `upper`, `lower`) are allowed: an `OBJECTIVE` column with its padding trimmed is the
chemist's own field, and refusing *every* transform, as the first draft did, failed the worker at
startup while accusing that binding of carving intent out of prose. The vocabulary
has a `regex` transform, so a site whose objective lives inside the protocol text could have written
`hypothesis: {path: root.PROTOCOL_TEXT, transform: [{regex: ...}]}` — loading, validating, ingesting
and rendering a `Tested:` line indistinguishable from a chemist's own words. One half of a codebase
refusing what the other half permits is not a rule; it is a rule plus whoever reviews the manifest.
The refusal names the supported route rather than leaving an operator with no alternative.

**5 · The campaign note drops columns nothing recorded, as the turn-time table always has.**

Its three headline setpoints were hardcoded on the assumption that an ELN always records a
temperature, a time and a yield — true of a columnar source, false of a prose one, where all three
render `—` on every row. That is the exact reading `drop_empty_columns` exists to prevent, and
`agent.condense._table` has always put the same three through it. Two tables built from one extracted
renderer disagreeing about which columns are real is the drift the extraction was done to stop.

**6 · `eln-validate` validates the adapters that are attached.**

`main()` enumerates `active_ingest_source_names()` and labels failures by source name. An empty
enabled set prints that nothing was checked and says plainly that this is not a pass, because "OK"
over an empty set is the failure being fixed. The shipped `make eln-validate` names the two file-drop
sources it has always covered; a deployment runs the same command against its own
`CHEMCLAW_DATA_SOURCES`.

## Consequences

The four-part story now runs on this schema: the series assembles and orders, each row names what it
changed, an outcome is one of three honest states, and where the chemist wrote down what they were
testing it appears in the comparison, quoted and marked as read. Two things remain deliberately
unserved and are not defects. The **mined** campaign note still shows no intent, because it renders
`OrdReaction.hypothesis` and no adapter on this source can honestly fill it — the reading is
turn-time, where its provenance travels with it. And **causality is still never inferred**: `follows`
is minted only by someone who can read the intent, which after this change is more often a real
sentence and less often a guess.

What this does not fix, and is filed rather than implied: `read_corpus` still runs no validator, so
an invariant enforced at ingest is not enforced for the artifact that answers the question.
