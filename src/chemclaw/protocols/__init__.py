"""Prescriptive experiment designs: what to run, as a first-class, revisable, persisted object.

Every other reaction shape in this tree is **descriptive** — `ingest.eln.ord.OrdReaction` and the
`reaction_records` transcription behind it record what a chemist did, and the ORD schema this
system borrows says so explicitly: a record "should describe what was actually done in the lab, and
not an idealized protocol or instruction set". Nothing here recorded what to *do*. This package is
that half.

`models` is the shape (one envelope for a single experiment and for a plate — a single experiment is
a design with one arm and no factors), `checks` the deterministic verdicts a draft has to survive,
`layout` the plate arithmetic, `diff` what an expert changed about the first shot, `render` what a
reader and a model receive, and `store` the revision history.

**Judgment is not here.** Which precedent counts, which factors are worth varying and what the
levels should be belong to `skills/protocol-generation` and `skills/hte-campaign-design`; the agent
composes the precedent, prediction and safety tools under them. What this package owns is the shape
the answer has to take and the checks it has to pass — so that a protocol whose numbers no tool and
no precedent ever touched is refused by code rather than merely discouraged by a prompt.
"""
