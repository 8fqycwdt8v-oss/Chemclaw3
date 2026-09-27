"""The marker a module-level string constant carries when a model is sent it.

**Why a marker at all.** The prose guards in `tests/test_prose_contract.py` read six enumerable
classes of model-facing text — tool docstrings, served bundle tools, durable-job and template
launchers, `SKILL.md` bodies, the system prompt's blocks and the profiles. Prompt text written as a
constant in an ordinary module is none of those: no decorator, manifest or directory says "this
string is sent to a model", so a sentence describing a removed tier there was invisible to every
guard. A derived rule — follow what reaches a model call — was measured and does not work here:
the prose reaches the model through middleware arguments, system-prompt concatenation and
`.format` templates rather than through a message constructor in the same function, so a
sink-local walk found one true constant among six hits and missed the helper and peer briefs
entirely. The declaration is therefore explicit
(`D-2026-09-26-prompt-prose-outside-the-agent-module-is-declared-not-derived`).

**Why a `str` subclass rather than an annotation.** A value that *is* the marker survives every
use of the constant unchanged — `.format`, concatenation into a larger prompt, a pydantic
`Field(description=…)` — and is found by `isinstance`, so a loader needs no source parsing to read
it. It changes nothing at runtime: it is a `str` in every respect a caller can observe except its
type, and the first `+` or `.format` returns a plain `str` again.

**Where one may appear.** Only as a module-level constant, or as a value inside a module-level
mapping or tuple — the one place a loader can reach without calling code.
`cli/validate_prose_contract.check_marked_prose_is_reachable` refuses one anywhere else, so a
marker cannot sit inside a function looking applied while no guard reads it.

**The limit, stated.** Nothing forces the marker on a new prompt constant; it is only as good as
whoever remembers it. Prose written inline in a function body — an f-string assembled at the call —
is outside it too, until it is hoisted into a marked template.
"""


class ModelProse(str):
    """A string constant this system sends to a model, which the prose guards therefore read."""

    __slots__ = ()
