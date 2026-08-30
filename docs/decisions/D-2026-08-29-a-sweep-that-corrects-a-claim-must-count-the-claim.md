# D-2026-08-29-a-sweep-that-corrects-a-claim-must-count-the-claim — the connector-validate sweep was short by three

**Status:** accepted · **Date:** 2026-08-29

**Completes** [`D-2026-08-29-connector-validate-never-dials-a-server`](D-2026-08-29-connector-validate-never-dials-a-server.md).
Its finding stands in full and is not reopened: `make connector-validate` opens no socket, its
served-versus-declared rule resolves the bundle's own in-tree `server/` module and returns `[]` for
`chem` and `safety`, and the checks that do reach a server are `make live-template-args` and
`Chemclaw3-mcp`'s `assert_manifest_matches`. What is corrected here is the sweep's own arithmetic.

## The count

That ADR opens *"Five documents in this tree say, in almost identical words…"* and then lists
**six** bullets. Grepped for the claim rather than counted from memory, the real total is **eight**:

| Document | Kind | State |
| --- | --- | --- |
| `src/chemclaw/connectors/chem/connector.yaml` | manifest comment | corrected by that ADR |
| `src/chemclaw/connectors/safety/connector.yaml` | manifest comment | corrected by that ADR |
| `D-2026-08-15-a-bundle-we-declare-is-not-a-bundle-we-run` (line 69) | merged ADR | text stands; superseded on that sentence |
| `D-2026-08-15-capability-moves-judgment-and-declaration-stay` (line 71) | merged ADR | text stands; superseded on that sentence |
| `D-2026-08-15-safety-is-a-tool-not-a-gate` (line 146) | merged ADR | text stands; superseded on that sentence |
| `D-2026-08-25-the-loop-is-a-composite-not-a-template` (line 154) | merged ADR | text stands; superseded on that sentence |
| **`D-2026-08-26-a-torsion-is-named-not-indexed` (line 134)** | merged ADR | **missed**; text stands; superseded on that sentence here |
| **`tests/test_templates.py` (line 378)** | test docstring, editable | **missed**; corrected in this commit to name `make live-template-args` |

So it was five merged ADRs rather than four, and the claim had also reached a file that is not an
ADR at all and could simply have been fixed.

One further occurrence is deliberately **not** counted in the eight, and is recorded so the next
grep does not have to decide again: `tasks/audit-2026-08-16/findings/round1/contract-connector-manifest.md`
(line 255) *quotes* the two manifests' sentence in order to refute it. It asserts nothing; it is a
record of the claim being audited, and an archived finding is not rewritten.

## The part that is worse than the count

That audit finding is dated **2026-08-16**. It names
`cli/validate_connectors.py`'s `if module is None: return []`, states that `server_tools_module`
returns `None` for `chem` and `safety` and a module for `calc`, and concludes: *"The claim quoted
above is false … it never dials a running server."* It even proposes the two fixes — an opt-in
`--live` mode, or a `note:` line so that "0 problems" stops implying "checked".

That is the whole of the 08-29 finding, thirteen days earlier, in a file nobody carried into a
decision. The correction sweep is right and it is also a **re-discovery**, which is a fact about how
findings leave `tasks/` rather than about `connector-validate`. Nothing here changes that; it is
named so the next reader knows the ADR is the second sighting, not the first.

## Why this is a new ADR rather than an edit

A merged ADR is never edited here, and that rule does not carry an exception for a merged ADR that
is itself a correction. Two reasons it should not:

- The record of what a sweep *found* is evidence about the sweep. Rewriting "five" to "eight" in
  place would leave a document claiming it named the torsion ADR when `git log` shows it did not,
  which is the same class of thing as the false sentence being corrected — a claim about a control,
  propagated by editing rather than by copy.
- This repository already has the shape for it and uses it: `D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`
  exists because an earlier sweep missed one. A follow-up ADR is the established move, and the "By
  topic" table in `docs/decisions/README.md` is what points a reader at the current state rather than
  at whichever document they opened first.

## The rule worth carrying

The parent ADR's rule is *a gate's name is not evidence about its reach*. Its own defect adds the
neighbouring one: **a sweep that corrects a copied claim has to be driven by a search for the claim,
not by the list of places somebody remembered.** The prose count and the bullet list disagreed with
each other inside one document, which is the tell — six bullets under the word "five" is a list that
was edited after the sentence, and neither was checked against a grep.

A grep for the claim is also what would have found the 08-16 finding, and with it the fact that this
was already known. The two failures have one cause.

## Consequences

- `tests/test_templates.py` no longer names `make connector-validate` as the check that
  argument-checks a template step on a bundle we declare and do not run. It names
  `make live-template-args`, which does.
- Five merged ADRs now carry a sentence superseded on this point, not four. Each keeps its text.
- Nothing about `connector-validate` itself changes, in either ADR. It is correct for the six
  bundles whose server is in this tree and honest about the two whose is not.
- No test enforces this. A grep-driven sweep is a discipline, and claiming otherwise would be the
  same failure one level up.
