# D-2026-08-29-a-sign-off-names-a-revision-or-it-names-nothing — the second review cycle over `chemclaw.protocols`

**Decision.** A second adversarial pass over the prescriptive tier — run against the code the
*first* review's fifteen fixes left behind
(`D-2026-08-29-the-review-of-the-prescriptive-tier-found-fifteen-defects`) — found **seven** more
here and one in `Chemclaw3_ui`, where the document page could not render a protocol at all.
The largest is a record this tree claimed to keep and did not: `experiment_protocol_status_events`
now holds which revision a person approved, ran or abandoned, who they were, and the reason they
typed. `executed` joins `approved` as a status a new revision retires. A `replicate_of` naming an
arm that runs *different* conditions is refused at the model, as a dangling one already was.

**The finding that outranks the individual defects:** the previous cycle's headline was a check that
could not fail. This cycle's is one step further out — **a fix whose stated cost was paid by a
record that did not exist.** `advanced()` demotes an approved design to `draft` when a revision
lands, and the docstring justifying that said "which revision *was* approved stays recoverable:
`set_status` records it". `set_status` wrote one column on the header row and logged a line without
the revision in it. The demotion was right; the sentence making it affordable was invented, in the
same commit, by the same author, one screen away from the code it described.

## What is now recorded, and why it is a table

### The approval

`experiment_protocols.status` describes the **head**, and it moves with the head — that is the whole
point of `advanced()`. So the header can never answer "which document did somebody sign off on",
and after a demotion nothing else could either. `infra/sql/077_experiment_protocol_status_events.sql`
is one append-only row per deliberate move, carrying the head revision at the instant of the move.

Only a *deliberate* move is recorded. An automatic demotion has no actor and no reason, and the
revision that caused it is already in the history; a row for it would be a second copy of a fact the
revision table already states.

### The reason

`POST /protocols/{id}/status` has accepted a `reason` up to 2,000 characters since the tier shipped,
and `set_status` took no such parameter — so the field was validated and dropped. That is not a
latent gap, because the caller exists and is emphatic about it: `Chemclaw3_ui`'s status panel labels
the box **"Reason (recorded with the move)"**, disables every status button until it is filled in,
and confirms *"The move is recorded against you with the reason you wrote."* A chemist was required
to write the single most useful sentence anybody writes about a design — *abandoned, the starting
material decomposes above 40 °C* — was told three times it was being kept, and it reached a 204 and
stopped. This is the `map_to_hpc_identity` shape in its user-facing form: not a control that does
nothing, but a control the interface **tells a person** is operating.

It is exposed on `GET /protocols/{id}` beside the document rather than merely stored. A record
nothing can read is the same defect one direction along, and this repository has an ADR about the
write half of it already
(`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`).

### `executed`

`advanced()` retired `approved` and left `executed` alone, which is the same sentence one word
along: a header saying a design was **run**, over a document that was not. Both are claims about a
document; a revision replaces the document. `abandoned` stays held, unchanged and for its stated
reason — a design somebody decided not to run does not come back because an agent wrote to it.

## The rest

- **A replicate that is not a replicate.** `replicate_of` naming a *real* arm with different levels
  or different setpoints was accepted, and it defeated the same two readers a dangling name did:
  `arms_are_distinct` skips every arm carrying `replicate_of`, and `coverage_is_stated` counts none
  of them towards the grid. Measured: two mislabelled arms turned a full 2-level grid into "reduced
  design: 2 of 4" while the run sheet told a chemist A2 was a repeat of A1. Averaging a replicate
  pair is how an assay's noise is estimated; averaging two different conditions reports that noise
  as the answer. Refused at the model validator, beside the dangling check, because — as that
  check's own comment says — this is not a judgment about a design but the difference between a
  document that means something and one that does not.
- **`render.summarise` was the fourth caller of `has_protocol`, spelling the condition out again**,
  in the release whose note says "three callers deciding it separately is how the second and third
  got it wrong". Nothing was wrong with this one; a fourth copy of a definition is how the fifth one
  is wrong.
- **The upsertable grant list named `reaction_records` twice.** Harmless to Postgres and not
  harmless to a reader: the file is the one place the write matrix is stated, and a duplicated entry
  is how a second, different entry gets read as the same one.
- **The HTE skill did not say to re-pass `plate_format` when the arms change.** The carried-forward
  plate is right for a revision that moves a temperature and wrong the moment the arm list moves,
  and the refusal a model then gets (`layout_fits`: "arms with no well") does not name the argument
  that fixes it. Prompt surface rather than code: the tool's own schema is already over this
  repository's per-tool token bound, and a skill loads on demand.

## What this cycle cleared

The first review's fifteen fixes hold, checked against their own reproductions rather than against
the tests written beside them: `"CCO junk"` is refused by `components_resolve`; forbidding `DMF`
blocks a design charging `N,N-dimethylformamide`; forbidding `ethanol` blocks a component carried as
the bare SMILES `CCO`, through `resolve_compound_name` in both directions. `_KEYED_LISTS`,
`atom_balance`'s three-part split, the 409 translation and the intake's carry-forward were all
re-read and stand.

## The one in the companion repository

`ProtocolView` in `Chemclaw3_ui` declared `GET /protocols/{id}` as
`{ revision: DesignRevision; history: RevisionSummary[] }`. The route returns the revision **flat**,
so `view.revision` is a number, `revision.design` was `undefined`, and the document page threw on
`design.request.title`. Under 808 unit tests and 8 browser tests, because every stub and the
end-to-end fixture emitted the same invented shape — including the spec whose docstring says it
exists to prove the page "renders against a real proxied response rather than a stubbed one".

Settled by dumping `DesignOut.model_json_schema()` rather than by re-reading the consumer, which is
the general rule this leaves behind: **a cross-repository shape is verified against the producer's
own schema.** Fixed in `Chemclaw3_ui#55`; re-nesting a stub there now fails six tests.

## Consequences

- One migration, one grant line, one row in `infra/sql/README.md`, one entry each in
  `durable/retention.py::_NOT_PRUNED` and `agent/leaver.py::_RETAINED`. The actor is **retained** on
  offboarding, on `experiment_protocols.opened_by`'s line and more strongly: an approval with nobody
  attached to it is not a smaller record of an approval, it is a claim that one happened.
- `DesignStore` gains `status_history` and `set_status` gains `reason`. Both backends implement
  both, because a store whose answer depends on which one a deployment configured is wrong on one of
  them — the divergence the first review found in `listing(session_id=…)`.
- A design approved and then revised now reads `draft`, and a UI showing only the header will show
  fewer approvals than before. That is the correct number.
- Not a defect in the tier and recorded anyway, because it is what the suite caught rather than what
  I did: this branch's merge of `origin/main` committed `<<<<<<< HEAD` into
  `docs/decisions/README.md` and dropped eight of that branch's ADR rows. `make lint` and
  `make type` were both green, because the file is Markdown.
