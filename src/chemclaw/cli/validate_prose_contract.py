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

Three rules, each deliberately narrow so the check stays true rather than noisy:

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
4. Every knowledge-graph note *type* the prose names must be in `KNOWN_NOTE_TYPES`. Rules 1-3 cover
   the capability the agent invokes; this covers the capability it *writes into*, which had the
   same defect and no checker. Two skills told the agent to write `protocol` and `experiment-batch`
   notes; neither type existed, so `kg-validate` would have failed the PR — except nothing opens
   that PR, so the proposal died on a branch and the contradiction was visible from neither end.
   A named type that does not exist is exactly rule 1's defect one layer down.

Run via `make prose-validate`; gated in CI beside `kg-validate` and `skill-validate`.
"""

import re
import sys
from pathlib import Path

from chemclaw.agent.chemclaw_agent import _INSTRUCTIONS, available_tool_names
from chemclaw.connectors.registry import skills_dirs as connector_skills_dirs
from chemclaw.core.config import settings
from chemclaw.kg.note import KNOWN_NOTE_TYPES

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

# A note type named in prose, in the two forms the corpus actually uses: `type \`x\`` (how a skill
# tells the agent what to pass) and `` `x` note `` (how it refers to one in passing). Deliberately
# these two anchored forms rather than "every backticked kebab word" — a note type is a lowercase
# slug, which is indistinguishable from a dozen other things a skill legitimately backticks, so an
# unanchored pattern would be a false-positive generator. Narrow and true beats broad and noisy,
# the same trade the `_BARE` underscore rule makes.
_NOTE_TYPE = re.compile(r"`([a-z][a-z0-9-]*)`\s+notes?\b|\btypes?\s+`([a-z][a-z0-9-]*)`")

# The third form, and the one that matters most: the parenthesised enumeration that follows a
# `**type**` label. `knowledge-graph-write` is where the vocabulary is *taught*, so its list is the
# one an agent copies from — and it is a hand-maintained duplicate of `KNOWN_NOTE_TYPES` that had
# already drifted in both directions, naming two types that did not exist while omitting three that
# did. The two forms above cannot see it because the types appear as a bare backticked list rather
# than in a sentence. This matches the label, then the enumeration is mined for backticked slugs.
_NOTE_TYPE_LIST = re.compile(r"\*\*types?\*\*[^(\n]{0,80}\(([^)]*)\)", re.DOTALL)
_BACKTICKED_SLUG = re.compile(r"`([a-z][a-z0-9-]*)`")


def referenced_tool_names(text: str) -> set[str]:
    """Every tool name `text` promises the model, in either form it can take.

    Public because `tests/test_agent.py` asserts the same thing from the other direction — that
    everything the prose names is actually *available* — and a second extractor there would let the
    two disagree about what the prose even says. Allowlisted non-tools are excluded, so callers see
    only names that are meant to resolve to a tool.
    """
    names = set(_CALL.findall(text)) | set(_BARE.findall(text))
    return names - _ALLOWED_NON_TOOLS


def referenced_note_types(text: str) -> set[str]:
    """Every knowledge-graph note type `text` names, in either anchored form.

    Both regex groups are collected because one alternation matches `` `x` note `` and the other
    `` type `x` ``; a non-participating group yields `""`, which is dropped. The enumeration form
    contributes every backticked slug inside the parenthesised list it labels.
    """
    named = {name for match in _NOTE_TYPE.findall(text) for name in match if name}
    for enumeration in _NOTE_TYPE_LIST.findall(text):
        named |= set(_BACKTICKED_SLUG.findall(enumeration))
    return named


def _prose_sources() -> dict[str, str]:
    """The agent-facing prose to check: every SKILL.md plus the built-in instructions."""
    sources = {"agents/chemclaw_agent.py::_INSTRUCTIONS": _INSTRUCTIONS}
    for skills_dir in [*settings.skills_dirs, *connector_skills_dirs()]:
        for path in sorted(Path(skills_dir).glob("*/SKILL.md")):
            sources[str(path)] = path.read_text()
    return sources


def check_prose_contract() -> list[str]:
    """Return one problem string per violation; empty means the prose matches the tool surface."""
    # One definition of the union, shared with the two other validators and the agent itself, so a
    # tool cannot be "available" to one checker and unknown to another (D-117).
    tools = available_tool_names()
    problems: list[str] = []
    for origin, text in _prose_sources().items():
        for name in sorted(referenced_tool_names(text) - tools):
            problems.append(f"{origin}: names {name} but no such agent tool is registered")
        for workflow_name in sorted(set(_WORKFLOW.findall(text))):
            problems.append(
                f"{origin}: directs the agent at `{workflow_name}`, which it cannot invoke — "
                "name the tool that starts it instead"
            )
        for note_type in sorted(referenced_note_types(text) - KNOWN_NOTE_TYPES):
            problems.append(
                f"{origin}: tells the agent to write a {note_type!r} note, but that type is not in "
                "chemclaw.kg.note.KNOWN_NOTE_TYPES — kg-validate would reject the PR it opens"
            )
    return problems


def main() -> int:
    """CLI: report every prose/tool mismatch; non-zero exit fails the CI gate."""
    problems = check_prose_contract()
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"\n{len(problems)} prose/tool mismatch(es)", file=sys.stderr)
        return 1
    print("prose contract OK: every named tool exists")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
