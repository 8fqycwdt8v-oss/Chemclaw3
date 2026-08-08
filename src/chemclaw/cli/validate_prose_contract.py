"""Check that the agent's prose only names capability the agent actually has (gap IDEA-7).

Two of this codebase's real defects were the same shape — prose promising something the code could
not do, invisible to `mypy`, `pytest`, and `make skill-validate` (which only checks frontmatter):

- `skills/experiment-design/SKILL.md` told the agent to "reach for" `BoCampaignWorkflow`, which no
  tool exposed, so the instruction pointed at an uninvocable capability (gap RCH-2).
- The agent instructions advertised answers about "purity, impurities" while the canonical reaction
  schema carried no such field (gap KNW-2).

Both are cheap to catch mechanically, and this is the check that does it. It is also the
*deterministic half* of the deferred agent-behavior eval (AG-13): AG-13 waits on a live LLM to
observe tool **selection**, but whether a named tool exists at all needs no model.

Four rules, each deliberately narrow so the check stays true rather than noisy:

1. Every `name(`-style call mentioned in a skill or in the agent instructions must be a registered
   agent tool, a tool an enabled connector advertises, a generated template launcher, or an
   explicitly allowlisted helper.
2. Every bare `snake_case` identifier in that prose must satisfy the same rule. This is rule 1's
   missing half, and until D-117 it was the whole check's blind spot: rule 1's pattern requires a
   backtick immediately followed by `(`, and `_INSTRUCTIONS` — the single most important piece of
   agent-facing prose, and the one a tool rename breaks first — names every tool *bare*
   (`gather_evidence sweeps all internal sources`). It therefore matched **nothing at all** there,
   and only `SKILL.md` files were ever really validated. An underscore is what makes this safe to
   apply to English prose: `snake_case` does not occur in it, so the rule is precise rather than
   heuristic. Measured over the whole corpus at the time it was added, it produced exactly one
   false positive — an argument name inside a call — which the pattern now excludes.
3. A skill must not direct the agent at a `*Workflow` class. The agent cannot invoke a workflow;
   it can only call a tool. Naming one is always either a dangling pointer or a missing tool.
4. Every note type the prose tells the agent to *write* must be in `KNOWN_NOTE_TYPES`. This is rule
   1's shape applied to the other half of the write path, and it was missing: two skills instructed
   `propose_knowledge_note(type="protocol")` and `type="experiment-batch"`, neither of which is a
   known type, so an agent that followed either opened a PR that `kg-validate` then rejected — the
   capability was reachable and the artifact was not (D-164). A note type is named in the gated
   form **`type `x``** (the word, then the backticked slug); write it that way in prose so this
   rule can see it.

**Three more rules, over a second and much larger corpus: the operator-facing documents.** The four
rules above ask "does the agent's prose name capability the agent has". These ask the same question
of the prose a *human* operates from, and they exist because that corpus had drifted far further.
A verification pass over it found roughly 166 lines naming module paths that have not resolved since
the D-148 package move (`agents/`, `service/`, `workflows/`, `calc/`), a CI workflow file that does
not exist, and ADR ids with no file — none of which any gate could see.

5. Every backticked **path** must exist on disk (tried from the repo root, from `src/chemclaw/` and
   from the Helm chart, since the docs use all three spellings and there is exactly one of each).
   Only paths containing a `/` are checked, so a bare `SKILL.md` used as a noun is not mistaken for
   a file reference, and placeholder spellings (`sources/<name>/`, `*/SKILL.md`) cannot match.
6. Every **ADR id** must resolve to a shipped decision — a file in `docs/decisions/`, or a
   *sub-decision label* that some ADR defines. That second clause is the interesting one:
   `D-A5a` and `D-A6a` are real labels living inside `D-048`/`D-049`, so they read exactly like
   citations and resolve to no file. Prose naming one is better off citing the ADR *and* the label,
   which is what the fix does — so the rule accepts a label an ADR defines, derived by scanning
   them, and still rejects an invented one.
7. Every `CHEMCLAW_*` **config key** must be a field on `Settings`. Nothing was broken here — this
   rule is prophylactic, and that is its value: every key the operator corpus names is currently
   correct and nothing was keeping it that way. (The count of keys checked is not written here on
   purpose — it would only go stale the way `.env.example:3` did; if it matters, it is a `len()`
   in a test, not a number in this docstring.)

**`Makefile` and `.env.example` join the operator corpus for the same reason (F17).** Both are
operator-facing — a contributor reads them before either document above — and both were outside
the gate rules 5-7 exist to run: `.env.example:3` named the pre-split, bare-filename `config.py`
and no check saw it, which is exactly the shape rule 5 exists to catch. Two things make them
different from a `.md` file rather than one more glob entry:

   - **A `Makefile` recipe command's backtick is shell command substitution, not a code span**, so
     the file cannot be read whole the way `_operator_sources` reads everything else — a future
     recipe using `` `helm template …` `` would be misread as a path/ADR/key candidate. What is
     excluded is exactly that: a real recipe command line (`_makefile_prose`), not every line that
     is not a bare `#` comment — a target's trailing `## help text` is Make syntax the shell never
     sees, and it is where a real citation already lives (`explain: ## ... (D-166)`), so excluding
     it too would trade one blind spot for another.
   - **`.env.example` has no such split** — it is comments and `KEY=VALUE` throughout, with no
     third kind of line to exclude — so it is read whole.

Rule 5 itself is unchanged: it still only checks backticked spans containing a `/`, and widening it
to bare filenames is deliberately not how `` `config.py` `` was fixed — that would make
`` `SKILL.md` `` used as a noun fail too. The fix for that miss is the prose change (spelling the
path with a `/`, `core/config/fingerprints.py`); the fix for it having *stayed* missed for two
documents is this wider corpus.

**Why the rules are split by corpus rather than merged.** Rule 2 (every bare `snake_case` token must
be an agent tool) is safe over skills because that prose talks about tools and almost nothing else.
Over `SECURITY.md` or the runbook it would fire on every settings name, SQL column, metric and path
fragment — hundreds of false positives. So agent prose gets rules 1-4, operator prose gets 5-7, and
each rule names the one namespace it can resolve. That is also what keeps a new rule cheap to argue
for: it either has an authoritative resolver or it does not belong here.

**What deliberately stays out.** Counts ("three secrets", "six bundles") are the other half of the
drift and are *not* mechanically checkable — a number in prose has no syntactic marker. This
repository has twice discovered the right answer for those and written it down: delete the number
and let a test assert it. A regex cannot count, and teaching one to try would produce a rule that is
wrong more often than the prose.

Run via `make prose-validate`; gated in CI beside `kg-validate` and `skill-validate`.
"""

import re
import sys
from pathlib import Path

from chemclaw.agent.chemclaw_agent import _INSTRUCTIONS, available_tool_names
from chemclaw.connectors.registry import skills_dirs as connector_skills_dirs
from chemclaw.core.config import Settings, settings
from chemclaw.kg.note import known_note_types

# Symbols a skill may legitimately name in call form that are not agent tools: library/graph
# internals a skill explains conceptually. Kept explicit and short — adding one is a review
# decision, which is the friction this check exists to create.
_ALLOWED_NON_TOOLS = frozenset(
    {
        "neighborhood",  # kg.graph traversal primitive, explained conceptually by the query skill
    }
)

_CALL = re.compile(r"`([a-z_][a-z0-9_]*)\(")
_WORKFLOW = re.compile(r"`([A-Za-z][A-Za-z0-9]*Workflow)`")
# A bare snake_case identifier: at least one underscore, so English prose cannot produce it.
# The lookbehind skips anything already carried by another form — a backticked span (rule 1 owns
# those), a path or dotted attribute, and an argument position inside a call, where the name is a
# parameter rather than a tool (`similar_reactions(reaction_smiles)`). The lookahead skips a
# trailing `(`, which is rule 1's pattern, so the two rules never double-report one name.
_BARE = re.compile(r"(?<![\w`/.,(-])([a-z][a-z0-9]*(?:_[a-z0-9]+)+)(?![\w(])")
# `type `x`` / `types `x``: the one phrasing that means "write a note of this kind". Deliberately
# anchored on the word rather than matching every backticked slug, which in this prose is mostly
# tools, fields and chemistry — the narrowness is what keeps the rule true instead of noisy, at
# the cost of a convention prose has to follow. It is stated in the module docstring and in
# `skills/README.md`.
_NOTE_TYPE = re.compile(r"\btypes?\s+`([a-z][a-z0-9-]*)`")


def referenced_note_types(text: str) -> set[str]:
    """Every note type `text` tells the model to write, in the one gated phrasing.

    Public for the same reason `referenced_tool_names` is: the test suite asserts the contract
    from the other side, and a second extractor would let the two disagree about what the prose
    says.
    """
    return set(_NOTE_TYPE.findall(text))


def referenced_tool_names(text: str) -> set[str]:
    """Every tool name `text` promises the model, in either form it can take.

    Public because `tests/test_agent.py` asserts the same thing from the other direction — that
    everything the prose names is actually *available* — and a second extractor there would let the
    two disagree about what the prose even says. Allowlisted non-tools are excluded, so callers see
    only names that are meant to resolve to a tool.
    """
    names = set(_CALL.findall(text)) | set(_BARE.findall(text))
    return names - _ALLOWED_NON_TOOLS


# Repo root, derived from this file rather than from the cwd so the check behaves the same under
# `make`, under pytest, and from a subdirectory.
_ROOT = Path(__file__).resolve().parents[3]

# The documents a human operates this system from. `docs/decisions/` and `docs/archive/` are
# excluded on purpose: a merged ADR is never edited (CLAUDE.md), and an archived document is a
# record of what was true then — validating either would demand rewriting history to satisfy a
# gate.
_OPERATOR_DOCS = (
    "README.md",
    "ARCHITECTURE.md",
    "SECURITY.md",
    "CLAUDE.md",
    "deploy/README.md",
    "skills/README.md",
    "docs/README.md",
)
# `docs/planning/` is maintained (`docs/README.md`) and is deliberately **not** here yet. Turning
# this on over it reports 175 further mismatches, and they are a different kind of defect: a ticket
# that says "create `agents/qm_tools.py`" names a file D-118 later deleted, so there is no path to
# correct it to — the sentence has to be reworded, one judgement at a time. Rewriting them to the
# nearest surviving module would falsify the build record the tickets exist to be. Tracked as its
# own backlog row, with the count, so the remainder is visible rather than quietly out of scope.
_OPERATOR_DOC_GLOBS = ("docs/guides/*.md", "docs/reference/*.md")

# Two more operator documents, handled outside `_OPERATOR_DOCS`'s uniform "read the whole file"
# path because neither is prose the way a `.md` file is (F17). Their absence is exactly how the
# `.env.example:3` `config.py` staleness survived a gate built to catch precisely that: the
# corpus above never looked at either file.
_MAKEFILE = "Makefile"
_ENV_EXAMPLE = ".env.example"
# A Makefile recipe command is the only line shape that can run a backtick as shell command
# substitution, so it is the only thing excluded — not "every line that isn't a `#` comment".
# That distinction matters: a target's trailing `## help text` (e.g. `explain: ## ... (D-166)`)
# and a bare `#`/`@#` comment are both Make-level syntax the shell never sees (shell treats
# everything after an unquoted `#` as inert too, same as a recipe's `@# ...` line), so both are
# safe prose exactly like a `.md` code span — only a real recipe command line is not. Make
# requires a literal tab to start a recipe line (verified with `cat -A` against this repo's
# Makefile); a tab immediately followed by `#`/`@#` is still a comment, so the lookahead excludes
# it from what counts as a "recipe" for this purpose.
_MAKEFILE_RECIPE = re.compile(r"^\t(?!@?#)")


def _makefile_prose(text: str) -> str:
    """The Makefile's non-recipe lines, joined — the part safe to scan as prose.

    Excludes only an actual shell command; every comment (top-level, a recipe's `@#`, or a
    target's trailing `## ...`) and every non-recipe line (targets, `.PHONY`, variables) passes
    through, since none of it is ever handed to a shell.
    """
    return "\n".join(line for line in text.splitlines() if not _MAKEFILE_RECIPE.match(line))


# A backticked path. Requires a `/`, so a bare filename used as a noun (`SKILL.md`, or a
# `connector.yaml`)
# is not read as a reference to one particular file. Placeholder spellings (`sources/<name>/`,
# `knowledge/{id}.md`, `*/SKILL.md`) cannot match, because the character class excludes their
# markers — which is what lets prose keep using them.
_PATH = re.compile(r"`([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\.[a-z]{2,7})`")
# An ADR id in either shipped form: the frozen `D-NNN` sequence, or the dated `D-YYYY-MM-DD-<slug>`
# convention that replaced it. `D-A5a`-style sub-decision labels match the first alternative's
# neighbourhood deliberately — they look exactly like citations and resolve to nothing.
_ADR = re.compile(r"\b(D-(?:\d{4}-\d{2}-\d{2}-[a-z0-9-]+|\d{3}|A\d+[a-z]?))\b")
# A `CHEMCLAW_*` env key. The final character may not be `_`, so a prefix written in prose
# (`CHEMCLAW_SERVICE_*`) is not read as a key whose name happens to end there.
_SUB_DECISION = re.compile(r"\b(D-A\d+[a-z]?)\b")
_ENV_KEY = re.compile(r"\b(CHEMCLAW_[A-Z0-9_]*[A-Z0-9])\b")

# Environment variables that are legitimately not `Settings` fields. Explicit and short, for the
# same reason `_ALLOWED_NON_TOOLS` is: adding one is a review decision.
_NON_SETTINGS_ENV = frozenset(
    {
        "CHEMCLAW_COMPONENT",  # read by deploy/entrypoint.sh to pick a role, never by Settings
        "CHEMCLAW_REVISION",  # a Containerfile build ARG, exported as CHEMCLAW_DEPLOYMENT_REVISION
        # Documented *as removed*, so the prose naming them is correct and must stay readable.
        "CHEMCLAW_ENTRA_CLIENT_ID",
        "CHEMCLAW_MCP_SERVERS",
    }
)


def _prose_sources() -> dict[str, str]:
    """The agent-facing prose to check: every SKILL.md plus the built-in instructions."""
    sources = {"src/chemclaw/agent/chemclaw_agent.py::_INSTRUCTIONS": _INSTRUCTIONS}
    for skills_dir in [*settings.skills_dirs, *connector_skills_dirs()]:
        for path in sorted(Path(skills_dir).glob("*/SKILL.md")):
            sources[str(path)] = path.read_text()
    return sources


def _operator_sources() -> dict[str, str]:
    """The operator-facing documents: the ones a human runs this system from.

    `Makefile` and `.env.example` join the corpus here rather than in `_OPERATOR_DOCS`, because
    neither can be read whole like a `.md` file: only the `Makefile`'s non-recipe lines are prose
    (`_makefile_prose`), and `.env.example` has no such split — it is comments plus `KEY=VALUE`
    throughout, so it is read in full.
    """
    paths = [_ROOT / name for name in _OPERATOR_DOCS]
    for pattern in _OPERATOR_DOC_GLOBS:
        paths.extend(sorted(_ROOT.glob(pattern)))
    sources = {
        str(path.relative_to(_ROOT)): path.read_text(encoding="utf-8")
        for path in paths
        if path.is_file()
    }
    makefile = _ROOT / _MAKEFILE
    if makefile.is_file():
        sources[_MAKEFILE] = _makefile_prose(makefile.read_text(encoding="utf-8"))
    env_example = _ROOT / _ENV_EXAMPLE
    if env_example.is_file():
        sources[_ENV_EXAMPLE] = env_example.read_text(encoding="utf-8")
    return sources


def _decision_files() -> list[Path]:
    """The ADR files, which are the authority on which ids resolve."""
    return sorted((_ROOT / "docs" / "decisions").glob("D-*.md"))


def _sub_decision_labels() -> set[str]:
    """Sub-decision labels an ADR actually defines, e.g. `D-A5a` inside `D-048`.

    Derived rather than allowlisted. These read exactly like citations and resolve to no file, which
    is the confusion the rule exists to surface — but the labels are real, and prose that names one
    *while also citing its ADR* is more precise than prose that drops it. So the rule accepts a
    label some decision document defines and still rejects an invented one.

    **Only the title line counts, and that is not a shortcut.** An ADR that *defines* a label names
    it in its heading — `D-048 — F5: real HPC execution … (D-A5, D-A5a)`. Scanning the whole body
    would make every mention definitional, so an ADR discussing a label (this rule's own ADR names
    an invented `D-A77b` as an example of what must fail) would silently license it. That is not
    hypothetical: it is how this function's first version was caught.
    """
    labels: set[str] = set()
    for path in _decision_files():
        title = path.read_text(encoding="utf-8").split("\n", 1)[0]
        labels |= set(_SUB_DECISION.findall(title))
    return labels


def _adr_resolves(adr_id: str, stems: set[str], labels: set[str]) -> bool:
    """Whether `adr_id` names a shipped ADR — an exact stem, a `D-NNN` prefix, or a known label."""
    return (
        adr_id in stems or adr_id in labels or any(stem.startswith(f"{adr_id}-") for stem in stems)
    )


def _path_resolves(candidate: str) -> bool:
    """Whether a backticked path names something on disk.

    Tried from the repo root, from `src/chemclaw/` and from the Helm chart, because the docs use
    all three spellings and each is unambiguous — there is exactly one package and one chart, and
    a document about the chart naturally writes `templates/podmonitor.yaml`.
    """
    return any(
        (_ROOT / base / candidate).exists() for base in ("", "src/chemclaw", "deploy/helm/chemclaw")
    )


def check_operator_prose() -> list[str]:
    """Rules 5-7 over the operator documents: paths, ADR ids and config keys must resolve."""
    stems = {path.stem for path in _decision_files()}
    labels = _sub_decision_labels()
    fields = set(Settings.model_fields)
    problems: list[str] = []
    for origin, text in _operator_sources().items():
        for candidate in sorted(set(_PATH.findall(text))):
            if not _path_resolves(candidate):
                problems.append(f"{origin}: names `{candidate}`, which does not exist")
        for adr_id in sorted(set(_ADR.findall(text))):
            if not _adr_resolves(adr_id, stems, labels):
                problems.append(
                    f"{origin}: cites {adr_id}, which has no file in docs/decisions/ — a "
                    "sub-decision label inside another ADR must cite that ADR instead"
                )
        for key in sorted(set(_ENV_KEY.findall(text)) - _NON_SETTINGS_ENV):
            if key.removeprefix("CHEMCLAW_").lower() not in fields:
                problems.append(f"{origin}: names {key}, which is not a Settings field")
    return problems


def check_prose_contract() -> list[str]:
    """Return one problem string per violation; empty means the prose matches the tool surface."""
    # One definition of the union, shared with the two other validators and the agent itself, so a
    # tool cannot be "available" to one checker and unknown to another (D-117).
    tools = available_tool_names()
    problems: list[str] = []
    for origin, text in _prose_sources().items():
        for name in sorted(referenced_tool_names(text) - tools):
            problems.append(f"{origin}: names {name} but no such agent tool is registered")
        # The *effective* vocabulary — core's set plus what the enabled bundles declare — because
        # that is what `kg-validate` will accept, and this check exists to predict its verdict.
        # Against core's half alone, prose naming `job-result` (minted by the `qm` bundle) would be
        # reported as unknown while the note it produces validates perfectly.
        for note_type in sorted(referenced_note_types(text) - known_note_types()):
            problems.append(
                f"{origin}: tells the agent to write a `{note_type}` note, which is not a known "
                "note type — the PR-gate would open a branch that `kg-validate` then rejects"
            )
        for workflow_name in sorted(set(_WORKFLOW.findall(text))):
            problems.append(
                f"{origin}: directs the agent at `{workflow_name}`, which it cannot invoke — "
                "name the tool that starts it instead"
            )
    return problems


def main() -> int:
    """CLI: report every prose/capability mismatch; non-zero exit fails the CI gate."""
    problems = check_prose_contract() + check_operator_prose()
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"\n{len(problems)} prose/capability mismatch(es)", file=sys.stderr)
        return 1
    print("prose contract OK: every named tool, note type, path, ADR id and config key resolves")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
