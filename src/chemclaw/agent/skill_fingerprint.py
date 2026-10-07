"""How a skill is named in a row that outlives the person who wrote it.

`turn_costs.skills_loaded` answers an equality question (was this skill already acting?), so it
holds a digest rather than the name: a personal skill's name is a chemist's own words, and
`turn_costs` is retained through erasure. Both the writer (`api/runner.py`) and the reader
(`agent/distiller.py`) import this one function so the two hashes cannot diverge. Shared skills
are digested too, so the column has one meaning.
"""

from chemclaw.core.ids import stable_hash


def skill_fingerprint(name: str) -> str:
    """The stable identity of one skill name, for a row that must not carry the name itself.

    Args:
        name: The skill's name, as the frontmatter declares it.

    Returns:
        A digest stable across processes and restarts, so a guard comparing two runs' rows agrees.
    """
    return stable_hash(name)
