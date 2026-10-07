"""The marker a module-level string constant carries when a model is sent it.

The prose guards in `tests/test_prose_contract.py` read enumerable classes of model-facing text
(tool docstrings, bundle tools, launchers, `SKILL.md`, system-prompt blocks, profiles). Prompt text
held as a constant in an ordinary module reaches the model through middleware arguments,
concatenation and `.format`, which cannot be derived reliably, so it is declared with this marker
(D-2026-09-26-prompt-prose-outside-the-agent-module-is-declared-not-derived).

A `str` subclass, so the marker survives use of the constant and is found by `isinstance` without
parsing source; it behaves as a plain `str`, and `+` or `.format` returns one. It may appear only as
a module-level constant or a value in a module-level mapping or tuple;
`cli/validate_prose_contract.check_marked_prose_is_reachable` refuses it elsewhere. Nothing forces
the marker onto a new prompt constant, and inline f-string prompts are outside it.
"""


class ModelProse(str):
    """A string constant this system sends to a model, which the prose guards therefore read."""

    __slots__ = ()
