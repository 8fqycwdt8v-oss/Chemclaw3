# D-162 — A series of experiments is a sequence, not a set

**Context.** The question that opened this: a lab technician works one step for weeks, running one
experiment a day, each chosen in response to yesterday's result. With an ELN connected, can the
system read that progression and propose tomorrow's experiment with a rationale — *without* BO?

Most of the machinery was already there and the audit is worth recording, because the missing parts
were not the ones an architecture diagram would predict. Present: ELN ingest carrying conditions,
outcomes, impurities, `outcome_class`/`failure_reason`, procedure prose and `performed_at`
(D-062-era gaps KNW-1..3); `memory.optimization` grouping same-transformation runs into an
`optimization-campaign` note; `gather_evidence` + `expand_note` over it; the
`optimization-campaign-synthesis` skill's lever/confound discipline; proactive computation (D-024);
`failure-mode` notes with `contradicts` edges; and — already non-BO — `deep-research`'s
"generating new protocols" path, which builds conditions from cited evidence and files them through
the PR-gate.

Three things were structurally absent, each of them a *data* gap rather than a reasoning one:

1. **No chronology anywhere.** `cluster_by_similarity` returns ids sorted lexically and the campaign
   table had columns *run | temp | time | yield*, emitted in that order. `performed_at` was on every
   record and reached no artifact the agent reads. Six weeks of daily work was served as an
   unordered set: any two runs comparable, the trajectory invisible, and "which variable has nobody
   touched" unanswerable.
2. **No intent.** `OrdReaction` had no field for what a run was testing, and `KNOWN_RELATIONS` had
   no sequential edge. The reasoning that connects consecutive experiments — the actual content of a
   line of enquiry — existed nowhere in the system.
3. **"What should I run next?" was wired to BoFire.** `deep-research` §6 routed it to
   `suggest_next_experiment` unconditionally. Sound when the question is an optimization over
   bounded variables; wrong when it is "the impurity moved when I dried the solvent — what does that
   mean and what do I run to confirm it", which no surrogate is being asked about.

**Decision 1 — the campaign note is chronological, and says so.** `chemclaw.memory.progression`
orders a series by `performed_at` (undated last, ties by id, total and deterministic so
re-synthesis is not a spurious diff) and computes, for each run, what differs from the run
*immediately before it in time*: the two setpoints and the species set of each non-product role,
folded through the one identity table so a spelling cannot fabricate a change, and reported as the
swap (`solvent DMF → 2-MeTHF`) rather than as two lists. The note gains a **Performed** and a
**Changed vs previous** column.

Three renderings of what the order means, because they license three different readings: a timeline;
a timeline with the undated runs named and parked at the end; or — when no run carries a date — an
explicit statement that the rows are an id listing and the changes column is *not* evidence of what
was tried next. A run identical to its predecessor reads `unchanged (repeat)`, because a
reproducibility check is a deliberate act and an empty cell reads as a missing record.

Amounts (equivalents, loading) are deliberately not diffed: they are optional on `Component` and
frequently absent, so comparing them would report a change whenever one run happened to record a
mass and its neighbour did not.

**Decision 2 — intent is recorded, never inferred.** `OrdReaction.hypothesis` carries what the run
was set up to test, in the chemist's words; the JSON ELN adapter reads it from the entry's own field
and the reaction note leads with it (`Tested: …`). It is explicitly *not* extracted from the
procedure prose: a pattern-matched motive would be indistinguishable downstream from one a chemist
wrote, and the whole value of the field is that it is testimony.

The new `follows` relation is the same argument as an edge — "this run answers that one". It is
minted by whoever can read the intent (the agent proposing the next experiment; a chemist confirming
a series), and **never derived from the campaign's own date order**, though that would have been
one line. `performed_at` proves sequence; it does not prove response. Auto-emitting `follows` from
two dates would have manufactured exactly the reasoning this ADR exists to stop the system
inventing.

**Decision 3 — the reasoned path is a first-class sibling of the BO path.** A new
`experiment-progression` skill holds the judgment: reconstruct the series in order, check the
ordering caveat before narrating a trajectory, separate **established** (a controlled comparison,
both runs cited) from **confounded** from **untouched** (the variable that never appears in the
changes column — usually the most valuable and the easiest to miss), read failures as data, compute
what the record does not know, and then propose **one** experiment with its rationale, a falsifiable
expectation, a fallback, and what it will not answer. `deep-research` §6 and the BO
`experiment-design` skill now both name the fork and require the answer to say which path it took —
so a reasoned proposal is never dressed as a model's output, or the reverse.

Proposals are recorded as a new `experiment-proposal` note type — the non-BO sibling of
`bo-candidate` — through the existing `propose_knowledge_note` and the existing PR-gate. No new
agent tool: a second door onto the same gate would be boilerplate, and the type plus a
`[[follows:reaction-…]]` link is the whole difference.

**Decision 4 — retrieval can be windowed in time.** `since`/`until` on `_eligible_notes` and on
`gather_evidence`, matched against `valid_from` — which for a reaction note is the day the
experiment ran. "What have I tried in the last two weeks" was unanswerable while the dates sat on
the notes with no filter that could reach them. An undated note *fails* a windowed query rather than
passing it: it cannot be shown to fall in the window, and a question about a period should not be
answered with a note of unknown date. Unwindowed sweeps — every existing call — are untouched.

**Consequences.** A daily-growing series stays current for free: the memory job re-synthesizes the
campaign note and D-078's supersede machinery retires the previous one. The seed corpus gains an
`experiment-proposal` instance and a `follows` edge, so both new vocabulary entries have real
instances (`tests/test_seed_corpus.py`). `tests/test_progression.py` covers the ordering, the
deltas, the three orderings-caveat cases, the repeat marker, and the two refusals — no motive from a
date, no hypothesis from prose.

What this does **not** do: nothing here reads an instrument trace, and nothing correlates an
impurity profile across runs beyond what the notes state in prose. A progression whose real content
is "the LC trace changed shape" is still only as legible as the chemist's own words about it.
