"""How a skill is named in a row that outlives the person who wrote it.

`turn_costs.skills_loaded` is the self-confirmation guard's input: it asks whether a given skill was
already acting in a conversation, which is an *equality* question. So the column holds a digest
rather than the name, and this module is the one place that decision is spelled out — both the
writer (`api/runner.py`) and the reader (`agent/distiller.py`) import it, because two hashes that
merely happen to agree are a guard that fails open the day one of them changes.

**Why a digest at all.** A personal skill's name is a chemist's own words —
`project-nightingale-workup`
is a realistic one — and `turn_costs` is retained through erasure (`agent/leaver.py::_RETAINED`) and
refused by `durable/retention.py`. So a name written here is immortal and un-erasable, which is the
opposite of what the tier's own licence promises. Measured before this: a private skill name was
still in the table after `erase_actor(apply=True)` reported success, while the code beside it said
the row "is erased with that person".

Shared skills are digested too, although their names are in git and carry nothing private. One rule
is cheaper than a branch, and a column holding names for one tier and digests for the other is a
column nobody can read without knowing which.
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
