"""Every validator entrypoint answers its command line, rather than quietly ignoring it.

Six modules under `chemclaw.cli` are CI gates whose whole job is refusing a declaration that does
not match reality. Four of them — skills, connectors, templates, prose — took no arguments at all
and *accepted* anything on the command line, which is a validator with the failure mode it exists
to prevent: an operator who has just mounted a private skills or connector directory and runs
`python -m chemclaw.cli.validate_skills /mnt/skills` was told "SKILL.md validation passed." about
the configured corpus, having never looked at the one they named. Proven before the fix: the exact
directory that fails through `CHEMCLAW_SKILLS_DIR` passed as an argument. `validate_kg` read
`sys.argv[1]` directly, so `--help` was interpreted as a notes directory.

The directory stays an environment variable rather than becoming a second positional, because every
one of these is a `PATH`-style list (`CHEMCLAW_SKILLS_DIR`, `CHEMCLAW_CONNECTORS_DIR`,
`CHEMCLAW_TEMPLATES_DIR`) and one knob with two spellings is how the two spellings drift. What the
argument gets is a refusal that names the variable to set instead.
"""

import pytest

# The four that never parsed anything, plus `validate_kg`, whose positional is real and now
# declared. Each is exercised through its own `main`, which is what `make` and CI invoke.
_REFUSE_ARGUMENTS = [
    "chemclaw.cli.validate_skills",
    "chemclaw.cli.validate_connectors",
    "chemclaw.cli.validate_templates",
    "chemclaw.cli.validate_prose_contract",
]


@pytest.mark.parametrize("module_name", _REFUSE_ARGUMENTS)
def test_a_validator_refuses_an_argument_it_cannot_honour(module_name: str) -> None:
    """An unhonoured argument must exit non-zero, not be discarded under a green line."""
    from importlib import import_module

    module = import_module(module_name)
    with pytest.raises(SystemExit) as raised:
        module.main(["/mnt/somewhere-else"])
    assert raised.value.code == 2  # argparse's own "bad usage" status


@pytest.mark.parametrize("module_name", [*_REFUSE_ARGUMENTS, "chemclaw.cli.validate_kg"])
def test_a_validator_answers_help(module_name: str) -> None:
    """`--help` is what an operator tries first, and it must not be read as a directory name."""
    from importlib import import_module

    module = import_module(module_name)
    with pytest.raises(SystemExit) as raised:
        module.main(["--help"])
    assert raised.value.code == 0


def test_the_graph_validator_still_takes_the_notes_directory_it_documents(tmp_path: str) -> None:
    """`validate_kg`'s positional is real behaviour and stays — declared instead of read raw."""
    from chemclaw.cli.validate_kg import main

    assert main([str(tmp_path) + "/no-such-notes-dir"]) == 1
