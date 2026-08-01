# D-2026-08-01-a-path-in-prose-is-a-claim-a-gate-can-check — A path in prose is a claim, and a gate can check it

**Status:** accepted · **Date:** 2026-08-01 · **Implements:** the "widen `prose-validate` to
operator prose" backlog row · **Extends:** D-117 (the prose contract), D-164 (the note-type rule),
D-148 (the package move whose drift this measures)

## Context

`validate_prose_contract.py` has checked *agent-facing* prose since D-117: a skill or the built-in
instructions may not name a tool that does not exist. The operator-facing documents — the ones a
human runs this system from — had no such gate, and had drifted much further.

A verification pass found, in the operator corpus alone: **33 backticked module paths that have not
resolved since the D-148 package move** (`agents/`, `service/`, `workflows/`, `calc/`, bare
`chemclaw/`), a CI workflow file `.github/workflows/deploy.yml` that has never existed, and ADR
citations with no file. None was visible to `mypy`, `pytest` or any validator.

**The strongest argument for the rule is that the validator was itself an instance of the defect.**
`_prose_sources()` labelled its own first entry `agents/chemclaw_agent.py::_INSTRUCTIONS` — a path
dead since D-148. The check that exists to catch prose naming things that do not exist was naming a
thing that did not exist.

## Decision

**Three rules over a second corpus, each resolving one namespace against an authority.** A
backticked path must exist on disk; an ADR id must resolve to a shipped decision; a `CHEMCLAW_*` key
must be a `Settings` field.

**Split by corpus, not merged.** Rule 2 (every bare `snake_case` token must be an agent tool) is
safe over skills, where the prose talks about tools and little else. Over `SECURITY.md` or the
runbook it would fire on every settings name, SQL column, metric and path fragment — hundreds of
false positives. So agent prose keeps rules 1-4, operator prose gets 5-7, and each new rule has to
name the one authority it resolves against. That requirement is the design: a rule with no
authoritative resolver is a heuristic, and a heuristic in a gate is argued with rather than obeyed.

**A path is checked only when it contains a `/`.** A bare `SKILL.md` or `connector.yaml` is a noun
in this prose, not a reference to one file. Placeholder spellings (`ingest/sources/<name>/…`,
`*/SKILL.md`, `knowledge/{id}.md`) cannot match the pattern, so prose keeps the ability to say "put
it here" without naming something that exists.

**Three resolution roots: the repo, `src/chemclaw/`, and the Helm chart.** The docs use all three
spellings and each is unambiguous — there is exactly one package and one chart, and a document about
the chart naturally writes `templates/podmonitor.yaml`.

**A sub-decision label is accepted when some ADR defines it, and the list is derived rather than
allowlisted.** This is the rule's most interesting clause and the first version got it wrong.
`D-A5a` and `D-A6a` are *real* labels living inside `D-048` and `D-049`; they read exactly like
citations and resolve to no file. Rejecting them outright would have forced the docs to drop a label
that says precisely which half of a two-part decision is meant. So the fix cites the ADR **and**
keeps the label — `ADR D-048 (Teilentscheidung D-A5a)` — and the rule scans the decision files for
the labels they define. An invented `D-A77b` is still caught.

**`docs/planning/` is deliberately out of scope, and that is a decision rather than an oversight.**
Turning these rules on over it reports **175 further mismatches**. They are a different defect: a
ticket that says "create `agents/qm_tools.py`" names a file D-118 later deleted, so there is no path
to correct it *to* — the sentence has to be reworded, one judgement at a time. Mechanically
rewriting each to the nearest surviving module would falsify the build record the tickets exist to
be. The count is in the backlog so the remainder is visible rather than quietly dropped, and there
is a test asserting the exclusion so it cannot be undone by accident.

`docs/decisions/` and `docs/archive/` are excluded on a stronger ground: a merged ADR is never
edited, and an archived document is accurate as of its date. Validating either would demand
rewriting history to satisfy a gate.

## Why not the alternatives

**Check counts too** — "three secrets", "six bundles", "eight validators". These are the other half
of the drift and there are nine of them, every one wrong. They are also not mechanically checkable:
a number in prose has no syntactic marker, and a regex cannot count. This repository has already
found the right answer twice and written it down — `values.yaml` says "the count now lives in the
test rather than in prose", and `CLAUDE.md` says "a count is not written here, because the one that
was said 23 while the file held 28". The fix is to delete the number and let a test assert it, which
is its own row.

**A deny-list of retired terms** (`hpc-jobs`, "MCP server" as a deployable, `services/chemclaw/`).
It would catch a real class — six findings — and it is a curated list rather than a derivation, so
it carries the same review friction as `_ALLOWED_NON_TOOLS` without the same justification. Those
six are semantic corrections to make by hand; if the class recurs, the list is cheap to add then.

**Rewrite `docs/planning/` mechanically anyway** to get one green sweep. See above: it would produce
175 edits, a proportion of which assert that a deleted module still exists under a new name. A gate
bought with false statements is worse than the gap.

**Apply rule 2 to operator prose** and allowlist the false positives. Hundreds of entries, each one
a claim nobody checked, in a file whose value is that its allowlist is short enough to read.

## Consequences

- 33 stale paths, one nonexistent workflow file and three unresolvable ADR citations are corrected,
  and `make prose-validate` now fails if any returns.
- `.github/workflows/deploy.yml` is replaced in `architektur.md` §6 by what actually runs, including
  the two corrections next to it: there is no rollout job (D-117 deleted the stub whose body was an
  `echo`), and `helm lint` runs nowhere — the chart check is `helm template | kubeconform`.
- The validator's own stale self-label is fixed, which is the smallest and most pointed evidence
  that the rule was needed.
- **Not closed: `docs/planning/`**, 175 mismatches across `BACKLOG.md` and
  `implementation-tickets.md`, needing per-line judgement.
- **Not closed: the nine wrong counts**, and **not closed: six retired concepts** still asserted in
  `architektur.md` §6/§7 and `CLAUDE.md` — including two task queues where there is now one plus a
  derived per-bundle queue, and "MCP servers hold capability" where connectors do. Both are their
  own rows.
