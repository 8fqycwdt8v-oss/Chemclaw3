"""Mine the stored conversations for recurring procedure and propose what survives the guard.

The consumer `chemclaw/cli/trajectory_census.py` never had. That command answers "is there enough
here to distil"; this one distils, and puts what it finds in the queue
`D-2026-09-18-a-proposal-is-not-a-skill-and-a-route-is-not-a-tool` built. Nothing it produces
changes any behaviour: a proposal waits for the person it belongs to.

**On demand, never on a timer**, for the rule `CLAUDE.md` states and the campaign and playbook
miners already follow: no Temporal Schedule opens a pull request, and knowledge never arrives on a
timer. A miner that ran hourly would fill a queue nobody asked it to fill.

**Dry by default.** Printing what it would propose costs nothing and is what somebody running this
for the first time wants; `--propose` is the flag that writes. That ordering is deliberate rather
than cautious — the output is the evidence for whether the guard is doing anything, and a run that
proposed before anybody had read it would be a miner whose effect precedes its explanation.

**On an empty corpus it proposes nothing and says so.** `make trajectory-census` reports zero on a
database nobody has used, so that is the expected first result rather than a fault, and the message
says which of the two it is.
"""

import argparse
import asyncio
import json
from collections import defaultdict

from chemclaw.agent.distiller import bounded, candidates, propose, scaffold
from chemclaw.agent.local_skills import personal_skills_available
from chemclaw.cli.trajectory_census import _stored, census


async def _skills_by_session() -> dict[str, frozenset[str]]:
    """What each session loaded, from `turn_costs.skills_loaded` — the guard's whole input.

    Session granularity because that is what the guard asks (`agent/distiller.py`: a skill read on
    a session's second turn shapes its tenth), so every turn's array is unioned into its session's.

    An empty map is the honest answer on a deployment that has never loaded a skill, and it makes
    the guard a no-op rather than a filter that silently drops everything — which is the direction
    that matters, since a guard erring toward *admitting* evidence would be the self-confirming
    failure it exists to prevent. That risk is real and bounded: with no rows, nothing has been
    accepted either, so there is no skill to be confirmed by.
    """
    from chemclaw.core import db
    from chemclaw.core.config import settings

    loaded: dict[str, set[str]] = defaultdict(set)
    async with db.connection(settings.session_store_dsn or settings.postgres_dsn) as conn:
        cursor = await conn.execute(
            "SELECT session_id, skills_loaded FROM turn_costs "
            "WHERE session_id <> '' AND cardinality(skills_loaded) > 0"
        )
        for session_id, skills in await cursor.fetchall():
            loaded[str(session_id)].update(str(skill) for skill in skills or ())
    return {session: frozenset(names) for session, names in loaded.items()}


async def _actor_of(sessions: list[str]) -> str:
    """Whose queue a proposal about these sessions belongs in.

    A proposal is per person — that is what makes one chemist's rejection not decide for another —
    so a distilled one needs an owner. It is read from the sessions the evidence came from rather
    than taken as an argument, because an operator running this command is not the person the
    trajectory belongs to and a proposal filed under the operator would act on the wrong turns.

    Returns the empty string when the sessions disagree or carry no actor, which the caller treats
    as "not attributable" and refuses to propose for.
    """
    from chemclaw.core import db
    from chemclaw.core.config import settings

    if not sessions:
        return ""
    async with db.connection(settings.session_store_dsn or settings.postgres_dsn) as conn:
        cursor = await conn.execute(
            "SELECT DISTINCT actor FROM turn_costs WHERE session_id = ANY(%s) AND actor <> ''",
            (list(sessions),),
        )
        actors = [str(row[0]) for row in await cursor.fetchall()]
    return actors[0] if len(actors) == 1 else ""


async def _run(write: bool, as_json: bool) -> int:
    """Mine, guard, report, and — with `--propose` — file. Returns the process exit code."""
    turns, failures = await _stored()
    report = census(turns, failures)
    occurrences: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for turn in turns:
        if len(turn.tools) >= 2:
            occurrences[turn.tools].append(turn.session_id)
    by_class = {tools: sorted(set(sessions)) for tools, sessions in occurrences.items()}
    found = bounded(candidates(report, by_class, await _skills_by_session()))

    if write and not personal_skills_available():
        # **Refused up front rather than filed and discovered later.** A proposal's only outcome is
        # `POST /proposals/skill/{name} {accepted: true}`, which needs the personal tier — so with
        # the tier off this would write durable rows that the one route able to act on them answers
        # 503 to, and the chemist would find that out after deciding. The dry run still works and is
        # the useful half here: it says what the corpus would propose.
        print(
            "refusing to file: this deployment keeps no personal skills, so nothing could accept "
            "what this would propose (needs CHEMCLAW_AGENT_MEMORY_ENABLED with a Postgres session "
            "store). Re-run without --propose to see what the corpus would say."
        )
        return 1

    filed: list[dict[str, object]] = []
    for candidate in found:
        actor = await _actor_of(list(candidate.sessions))
        entry: dict[str, object] = {
            "name": candidate.name,
            "tools": list(candidate.tools),
            "independent_sessions": len(candidate.sessions),
            "discounted_sessions": len(candidate.self_confirming),
            "actor": actor,
        }
        if not actor:
            entry["skipped"] = "the evidence spans more than one chemist, or names none"
        elif write:
            entry["state"] = (await propose(candidate, actor)).state
        else:
            entry["would_propose"] = len(scaffold(candidate))
        filed.append(entry)

    if as_json:
        print(json.dumps({"sessions": report["sessions"], "candidates": filed}, indent=2))
        return 0

    print(f"sessions: {report['sessions']}   turns: {report['turns']}")
    print(f"recurring classes: {len(report['recurring_classes'])}")
    if not report["sessions"]:
        print(
            "nothing to distil: this database holds no conversations. That is the expected "
            "first result, not a fault — `make trajectory-census` measures the same zero."
        )
        return 0
    if not found:
        print(
            "nothing survived: either no trajectory recurs across enough conversations, or the "
            "ones that do only recur where a skill of that name was already acting."
        )
        return 0
    for entry in filed:
        discounted = entry["discounted_sessions"]
        suffix = f", {discounted} discounted as self-confirming" if discounted else ""
        verb = entry.get("state") or ("would propose" if not entry.get("skipped") else "skipped")
        print(f"  {entry['name']}: {entry['independent_sessions']} sessions{suffix} — {verb}")
    return 0


def main() -> None:
    """Run the distiller over the configured database."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--propose",
        action="store_true",
        help="file what survives into the proposal queue (otherwise only report it)",
    )
    parser.add_argument("--json", action="store_true", help="emit the machine-readable form")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.propose, args.json)))


if __name__ == "__main__":  # pragma: no cover - the module entrypoint
    main()
