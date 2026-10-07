"""Prescriptive experiment designs: what to run, as a first-class, revisable, persisted object.

Every other reaction shape here is descriptive (what a chemist did); this package is the
prescriptive half. `models` is the shape (one envelope for a single experiment and a plate),
`checks` the deterministic verdicts a draft must survive, `layout` the plate arithmetic, `diff`
what an expert changed, `render` what a reader and a model receive, and `store` the revision
history.

Judgment (which precedent counts, which factors to vary) belongs to the protocol skills. This
package owns the shape and the checks, so a protocol whose numbers no tool or precedent touched
is refused by code rather than discouraged by a prompt.
"""
