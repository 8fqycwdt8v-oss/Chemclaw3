"""Mine which tools are used together, and propose an agent profile naming the strongest cluster.

The second proposer `tasks/todo.md` names, and the honest one to be clear about: **an accepted
profile proposal is a record, not an effect.** A profile is a file in `data/profiles/`,
git-resident and reviewed in a pull request, and no HTTP route can commit — so what this buys is a
person reading a rendered YAML and opening that pull request, instead of nobody noticing the
pattern at all. Saying that plainly is better than a queue implying otherwise
(`D-2026-09-18-a-proposal-is-not-a-skill-and-a-route-is-not-a-tool` states it too).

**What it measures, and what it deliberately does not.** Co-occurrence: which tools appear together
in one chemist's turns, more often than the corpus average would put them there. That is a fact
about how somebody works. It is *not* evidence that a narrower profile would answer better — this
repository has measured that question twice and both arms are on record
(`D-2026-08-12`, `D-2026-08-13`), so a proposal from here claims a pattern and never a benefit, and
the rationale it files says which of the two it is.

**Attenuation still holds by arithmetic.** A proposed profile names a subset of the tools its
evidence used, and `AgentProfile.tool_names` only ever narrows — so the worst outcome of accepting
one is an agent that can do less, which is the direction
`D-2026-08-10-a-subagent-is-an-attenuation-not-a-new-actor` requires, and the reason a
widening lever was refused in the roster.

**On demand and dry by default**, for `chemclaw/cli/distill.py`'s reasons exactly.
"""

import argparse
import asyncio
import json
from collections import Counter, defaultdict

from chemclaw.agent.behaviour_proposals import Proposal, content_hash, default_proposal_store

#: How many of one chemist's turns a pair of tools must share before it is a cluster rather than a
#: coincidence. Low, because the thing being proposed is a *record for a person to read* rather
#: than a change — and a threshold tuned to avoid false positives on evidence nobody acts on
#: automatically would only hide patterns from the person who should judge them.
MIN_SHARED_TURNS = 3

#: The most tools a proposed profile names. A profile that named everything would be `default`
#: under another name, which is `D-2026-08-12`'s identical-menu defect one layer over.
MAX_TOOLS = 12


def clusters(turns: list[tuple[str, frozenset[str]]]) -> dict[str, list[tuple[str, int]]]:
    """Per actor, the tools that most often appear beside the tools they appear with.

    Args:
        turns: `(actor, tools used in that turn)`, one entry per turn.

    Returns:
        `{actor: [(tool, turns it appeared in), ...]}`, strongest first, for every actor with a
        cluster at all. An actor whose tools never co-occur has no profile to propose and is
        absent rather than present-and-empty.
    """
    per_actor: dict[str, Counter[str]] = defaultdict(Counter)
    pairs: dict[str, Counter[tuple[str, str]]] = defaultdict(Counter)
    for actor, tools in turns:
        if len(tools) < 2:
            continue
        per_actor[actor].update(tools)
        ordered = sorted(tools)
        for index, left in enumerate(ordered):
            for right in ordered[index + 1 :]:
                pairs[actor][(left, right)] += 1
    found: dict[str, list[tuple[str, int]]] = {}
    for actor, counted in pairs.items():
        together = {
            tool
            for (left, right), shared in counted.items()
            if shared >= MIN_SHARED_TURNS
            for tool in (left, right)
        }
        if len(together) < 2:
            continue
        found[actor] = sorted(
            ((tool, per_actor[actor][tool]) for tool in together),
            key=lambda row: (-row[1], row[0]),
        )[:MAX_TOOLS]
    return found


def document(actor: str, cluster: list[tuple[str, int]]) -> str:
    """The profile YAML a cluster proposes — deterministic, for the queue's content key.

    A name derived from the cluster rather than from the person: a profile is a *role*, and one
    named after a chemist would be an identity in a file every deployment reads. The tools are
    sorted so two runs over one corpus produce one proposal rather than a family of them.
    """
    tools = sorted(tool for tool, _ in cluster)
    listed = "\n".join(f"  - {tool}" for tool in tools)
    return (
        f"# Proposed from observed co-occurrence across one chemist's turns.\n"
        f"# This names a pattern in how somebody works. It is NOT evidence that a narrower\n"
        f"# surface answers better — that question is measured in `evals/`, and both existing\n"
        f"# arms are on record in D-2026-08-12 and D-2026-08-13.\n"
        f"name: {_name(tools)}\n"
        f"description: >-\n"
        f"  Observed working set: {', '.join(tools)}. Written by the profile proposer from\n"
        f"  co-occurrence alone; the instructions below are a gap for a person to fill.\n"
        f"instructions: ''\n"
        f"tool_names:\n{listed}\n"
    )


def _name(tools: list[str]) -> str:
    """A stable profile name for a tool set — the first two tools, which is what a reader scans."""
    return "-".join(tool.replace("_", "-") for tool in tools[:2])[:48].rstrip("-") or "observed"


async def _turns() -> list[tuple[str, frozenset[str]]]:
    """Each turn's actor and the tools it called, from the audit trail.

    `audit_events` rather than `session_messages`, because the question is which *tools ran* and the
    trail is the one place every call is recorded — including the ones a transcript does not carry.
    """
    from chemclaw.core import db
    from chemclaw.core.config import settings

    by_turn: dict[tuple[str, str], set[str]] = defaultdict(set)
    async with db.connection(settings.postgres_dsn) as conn:
        cursor = await conn.execute(
            "SELECT actor, correlation_id, tool FROM audit_events "
            "WHERE actor <> '' AND correlation_id <> '' AND tool <> ''"
        )
        for actor, correlation_id, tool in await cursor.fetchall():
            by_turn[(str(actor), str(correlation_id))].add(str(tool))
    return [(actor, frozenset(tools)) for (actor, _), tools in by_turn.items()]


async def _run(write: bool, as_json: bool) -> int:
    """Mine, report, and — with `--propose` — file. Returns the process exit code."""
    turns = await _turns()
    found = clusters(turns)
    filed: list[dict[str, object]] = []
    for actor, cluster in sorted(found.items()):
        body = document(actor, cluster)
        names = [tool for tool, _ in cluster]
        entry: dict[str, object] = {"actor": actor, "tools": names, "turns": len(turns)}
        if write:
            outcome = await default_proposal_store().propose(
                Proposal(
                    kind="profile",
                    name=_name(sorted(tool for tool, _ in cluster)),
                    content_hash=content_hash(body),
                    content=body,
                    rationale=(
                        f"{len(cluster)} tools co-occur across this chemist's turns. A pattern in "
                        "how somebody works, not evidence that a narrower surface answers better. "
                        "Accepting records the request; a profile is git-resident, so somebody "
                        "still opens the pull request."
                    ),
                    actor=actor,
                    session_id="",
                    correlation_id="",
                )
            )
            entry["state"] = outcome.state
        filed.append(entry)

    if as_json:
        print(json.dumps({"turns": len(turns), "clusters": filed}, indent=2))
        return 0
    print(f"turns with two or more tools: {sum(1 for _, tools in turns if len(tools) >= 2)}")
    if not filed:
        print(
            "no cluster: either this database holds no tool calls, or no pair of tools is used "
            "together often enough to be a working set rather than a coincidence."
        )
        return 0
    for entry in filed:
        verb = entry.get("state", "would propose")
        tools = entry["tools"]
        listed = ", ".join(tools) if isinstance(tools, list) else str(tools)
        print(f"  {entry['actor']}: {listed} — {verb}")
    return 0


def main() -> None:
    """Run the profile proposer over the configured database."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--propose", action="store_true", help="file what it finds into the proposal queue"
    )
    parser.add_argument("--json", action="store_true", help="emit the machine-readable form")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.propose, args.json)))


if __name__ == "__main__":  # pragma: no cover - the module entrypoint
    main()
