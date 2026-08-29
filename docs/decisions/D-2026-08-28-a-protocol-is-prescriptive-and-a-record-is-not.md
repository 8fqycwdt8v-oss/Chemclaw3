# D-2026-08-28-a-protocol-is-prescriptive-and-a-record-is-not — the prescriptive tier

**Decision.** Add `chemclaw.protocols` — a prescriptive experiment design as a first-class,
checked, revisable, persisted object — with one envelope covering a single experiment and an HTE
plate, four agent tools, five HTTP routes, two skills, and two tables. Do **not** reuse
`OrdReaction`, `ReactionRecord`, `ProcessConditions` or `Note` for it, and do **not** put the
drafting pipeline in a Temporal workflow.

## What was missing, measured against `HEAD`

Every ingredient existed and none of them produced a protocol.

The system could *read* the record — `substrate_precedent`, `conditions_for_similar_reaction`,
`conditions_for_similar_product`, `reagent_frequency`, `workup_precedent`, `similar_reactions`,
`gather_evidence`, `condense_protocols`, six memory tiers — and it could *design a point in a
space*: `generate_screening_design` returns a factorial's runs, `suggest_next_experiment` returns
the next batch. What neither produces is the thing a chemist acts on. A list of condition tuples is
not a procedure: it says nothing about what to weigh out, in what order, under what atmosphere,
quenched how, sampled when, measured with what.

Five concrete holes:

1. **No prescriptive object.** Every reaction shape here is descriptive by construction, and the
   ORD schema this tree borrows says so in as many words: a record "should describe what was
   actually done in the lab, and not an idealized protocol or instruction set". `record.py`'s own
   docstring carries the same rule one level down — *nothing here infers anything*.
2. **No structured intake.** The ask arrives as free text and was re-derived from the transcript on
   every turn, with no artifact the chemist could correct before the expensive work started.
3. **Nothing for a plate.** `ScreeningDesign` returns `runs`; there was no layout, no well, no
   control, no run order, no plate map anywhere in the tree.
4. **No persistence and no revision.** A drafted protocol lived in one transcript. No id to reopen,
   no revision, no diff — and therefore **no record of the edit the expert made**, which is the
   single most informative observation this system can make about its own suggestions.
5. **No surface.** `Chemclaw3_ui`'s own `USER-STORIES.md` files E4 ("hand a screening design to the
   lab") as `PARTIAL` and G1 as `PROSE-ONLY`.

## The design

### One envelope, two shapes

**A single experiment is a design with one arm and no factors.** An HTE screen is the same object
with factors, levels, N arms and a layout. Because there is one shape, the store, the checks, the
renderer, the CSV export and the UI are each written once; a second "HTE campaign" type would have
duplicated all five and let them drift.

```
ExperimentDesign = request + base(ProtocolBody) + factors + arms + layout + evidence
```

`request` is written by `structure_experiment_request` and `layout` is computed by
`protocols.layout.place`; `draft_experiment_protocol` authors only the four in between. See "The
intake is a prerequisite" below for why that split is structural rather than stylistic.

### Rows, not PR-gated notes

The gate answers *is this true*. A draft is a proposal to act and nothing about it is true yet —
the decision it needs is *running it*, which happens in a laboratory rather than in a review queue.
That is `D-2026-08-25-an-eln-transcription-is-data-not-a-claim` arriving from the opposite side: a
transcription is ungated because a reviewer has nothing to decide, and a draft is ungated because
the reviewer is not the person who decides. What a human asserts about a design *after* it runs is
still a `playbook` or an `experiment-proposal` note, gated as it always was, citing the design.

### Four vocabularies not reused, and why each

- **`ingest.eln.ord.StepKind`.** That is the record vocabulary — the values a source may state and
  a warehouse binding's `value_map` may write — and its members decide how a recorded procedure is
  segmented. A prescriptive vocabulary needs three verbs a record has no reason to carry (`sampling`,
  `analysis`, `hold`), and widening the record enum would let a tenant YAML file write a value the
  ingest path cannot interpret. Same argument `science.labels.vocabulary` makes for not widening
  `Role`. The six shared values are spelled identically so a design later transcribed as a run maps
  across with no table.
- **`kg.note.ProcessConditions`.** It mixes setpoints with yield and impurity because its docstring
  says it holds "what a run recorded". A plan's temperature is an instruction; a plan's yield is a
  prediction. `Setpoints` and `ExpectedOutcome` are two models so a reader can never take one for
  the other.
- **`science.bo.problem.OptimizationProblem`.** A design space is not a design: it has no charge
  table, no steps, no analytics, and its whole reason for existing is to be searched.
- **`templates.Template`.** A template is a fixed chain of *orchestration* steps with no loops
  (`D-2026-08-25-the-loop-is-a-composite-not-a-template`). A protocol is chemistry.

What **is** reused is `science.labels.vocabulary.SpeciesRole`, deliberately: "the ligand a precedent
used" and "the ligand this design charges" have to be the same word, or the two are enums that agree
by accident.

### The checks are code, and one of them is a blocker for a reason

Deterministic verdicts computed from the design — never asked of a model, which would be a second
answer about its own first answer. `checks.check_ids()` is the list and no number is written here,
because a count in prose is a second declaration nobody re-derives
(`D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose`).

**Severity is per case rather than per check**, which is the part worth stating: `charge_is_consistent`
is a blocker when the table names no limiting reagent or its equivalents contradict its amounts, and
a warning when there is no table yet. A **blocker** is reserved for cases where storing the design
would be storing something misleading — it is not a protocol at all, a structure nobody can read, a
charge table that cannot be weighed out, an arm setting a level the factor does not declare, a plate
that does not fit, a reagent the chemist forbade, no evidence at all. A **warning** is a judgment
about a specific piece of work that a checker is not entitled to make: a missing control, an
unmeasured objective, an unscreened hazard, a temperature outside the band a unit mistake leaves.

**`evidence_present` is the load-bearing one.** A design citing no precedent and no tool cannot be
stored. That is what makes "use the record and the tools" a property of the code rather than a hope
about the prompt — and the distinction matters most on exactly the turn where a model has an answer
it likes and no reason to go looking.

### Composition in the turn, not a workflow

The drafting pipeline is the agent composing precedent, prediction and safety tools under two new
skills (`protocol-generation`, `hte-campaign-design`). Freezing it into a Temporal workflow would
put chemistry judgment into orchestration code — the thing this tree consistently refuses — and
would have made "which precedent counts" a Python function. Code owns the shape and the checks;
skills own the judgment. It becomes durable work when a draft starts fanning out `calc` jobs, and
that is a measurement to take first.

### Human edits are REST writes, and conflicts are 409s

`Chemclaw3_ui`'s own rule is that everything the *agent* does reaches it as a chat turn, because a
button that composed a tool call would be a surface deciding what the agent does. Editing a document
is not that — it is the chemist authoring a revision, the same shape `POST /proposals/{id}/decision`
already has. So the write is `POST /protocols/{id}/revisions`, `author_kind` records that a person
made it, and the agent is not involved.

`parent_revision` is required and is compared against the head, the way `plan_hash` is on
`POST /sessions/{id}/plan/decision`. Two chemists editing one plate is the ordinary case, and a
last-write-wins store would lose one of them silently.

One asymmetry is deliberate and is documented in both places: **a blocking check refuses the agent's
draft and does not refuse a human's edit.** A chemist editing towards a working protocol passes
through invalid intermediate states and can see the verdict; a model cannot.

### The revision table is the product

`experiment_protocol_revisions` is append-only *by grant* — INSERT and no UPDATE, beside
`bo_suggestions` and `structures` — not by convention. A revision is what an expert's correction of
a generated protocol *is*, so a credential that could rewrite one could erase the signal the table
exists to keep. `protocols.diff` flattens two revisions to dotted paths rather than tree-diffing
them, because both consumers want that form: a UI puts a marker next to one field, and a later miner
asks how often the chemist changes `base.setpoints.temperature_c` and in which direction.

## The honesty rule, and how it inverts an existing one

`RequestField` carries `basis: stated | inferred | absent`, and `stated` obliges a verbatim quote
that `require_quotes_are_verbatim` checks against the chemist's own text — normalised for
whitespace and case, and nothing else, so a paraphrase is refused.

Inference is *allowed* here, which is the opposite of
`D-2026-08-26-a-transcription-may-not-infer-a-setpoint`, and the two are consistent: a record must
not gain a number nobody measured, and a proposal is nothing but numbers nobody has measured yet.
What both rules share is that neither permits the inference to be silent. Without the check, "the
chemist wrote this" would be a claim the model grades itself on.

### The intake is a prerequisite, and the context ratchet is what made it structural

`draft_experiment_protocol` first took a whole `ExperimentDesign`, `structure_experiment_request`
being merely *documented* as the thing to call before it. `tests/test_context_floor.py` refused that
signature — 4,645 tokens, against a 900-token per-tool bound — and narrowing it produced a better
contract than the prose had: the tool now takes `design_id` plus the protocol half only, so the ask
lives in exactly one place and re-sending it is not possible rather than merely discouraged. A
model-supplied `layout` went the same way, because it is computed from `plate_format` and a supplied
one could contradict the format it was asked for.

The measured reduction, in four parts, is in that file: **6,231 tokens to 3,380 (−46%)** across the
two writing tools, and the whole `default` prefix from 35,035 to **32,184**. Two of the four causes
generalise and are worth naming here: pydantic publishes a referenced enum's *class docstring* as a
field description, and `convert_to_openai_tool` inlines rather than `$ref`s — so
`science.labels.vocabulary.SpeciesRole`'s 180-token argument shipped three times in one schema, and
`RequestField`'s shipped four. Fifteen model docstrings moved into `#` comments for the same reason.

**Both writers stay over the per-tool bound and are recorded in `KNOWN_OVERSIZED` with the
measurement.** The irreducible core is `base: ProtocolBody` at 922 tokens with every description
already one line: a typed laboratory procedure is about 900 tokens of schema, so nothing short of
deleting the schema meets a 900-token bound. Taking the payload as a JSON string or a scratchpad
path was considered and rejected — it drops the schema to ~150 tokens and takes schema-guided
generation with it, on exactly the call where a malformed argument is most expensive. The
`BACKLOG.md` row filed against it names the real fix and the fact that it is not first-party: a
`$ref`-emitting conversion would cut all four `KNOWN_OVERSIZED` entries at once.

## Costs accepted

- **Four tools on the default surface**, with their schemas on every turn: the ceiling moves 29,500
  → 33,000 against a measured 32,184, and two entries join `KNOWN_OVERSIZED` after the narrowing
  above, not instead of it (`D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose`).
- **A new top-level package** (`ARCHITECTURE.md` row, `README.md`, layering edges
  `protocols → {core, science}` and `{agent, api} → protocols`).
- **A hard ordering between the two writing tools.** `draft_experiment_protocol` refuses an unknown
  `design_id` and tells the caller to structure the ask first. That is the intended flow and it is
  now the only one, which costs a turn on the rare ask that needed no intake.
- **Two tables that retention does not prune and offboarding does not erase.** Both are stated
  positions rather than omissions: `experiment_protocols.opened_by` and
  `experiment_protocol_revisions.author` are in the retain tier for the reason
  `bo_campaigns.opened_by` is — a design is a shared scientific artifact and who revised it is what
  makes the correction attributable at all.

## Deliberately not done

- **Wiring `rxnpredict`** (`predict_reaction_conditions`, `predict_forward_reaction`) from
  `Chemclaw3-mcp`. It is the highest-value single addition to this pipeline and it is a
  *default-surface* decision rather than a wiring change — the argument the `pyexec` backlog row
  already makes: discovery is enablement, so a manifest here turns six tools on in every fresh
  checkout, needs six probes, and moves the context floor. Its own ADR and its own pull request.
- **A `ProtocolDraftWorkflow`.** See above.
- **Mining the human-edit diffs.** The diffs are stored from day one precisely so the measurement
  becomes possible; the miner needs a corpus that does not exist yet, and building it now would be
  the `map_to_hpc_identity` shape — a mechanism whose only caller is its own test.
- **A `protocol` note type.** The design is not a knowledge claim; nothing mints one.
