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

from pathlib import Path

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


# --------------------------------------------------------------------------------------------
# A gate that is green while checking nothing.
#
# Every one of these validators was watched refusing the thing it names. What none of the three
# below refused was the state where there is nothing to name: an empty corpus, an empty manifest
# directory, a path that is not a directory at all. `CHEMCLAW_NOTE_REPO_DIR`,
# `CHEMCLAW_RESULT_SINKS_DIR` and `CHEMCLAW_DELIVERY_CHANNELS_DIR` are all operator overrides, so
# a typo in one turned its gate off behind a success line — which is the `map_to_hpc_identity`
# shape this repository names in four other places, and which four sibling validators already
# refuse in these same words.
# --------------------------------------------------------------------------------------------


def test_the_graph_validator_refuses_a_corpus_with_no_notes_in_it(tmp_path: Path) -> None:
    """An empty notes directory is a gate that checked nothing, not a graph that is valid.

    This is the only check on `[[reaction-*]]` citations: since D-2026-08-25 `dangling_links`
    ignores that namespace on purpose because it cannot see the record store. A fresh clone, the
    wrong branch, or a PVC mounted after its directory was created reaches exactly this state.
    """
    from chemclaw.cli.validate_kg import main

    assert main([str(tmp_path)]) == 1


def test_the_graph_validator_refuses_a_notes_path_that_is_not_a_directory(tmp_path: Path) -> None:
    """`exists()` was the whole guard, so a *file* walked zero notes and printed the OK line."""
    from chemclaw.cli.validate_kg import main

    a_file = tmp_path / "README.md"
    a_file.write_text("# not a notes directory\n", encoding="utf-8")
    assert main([str(a_file)]) == 1


def test_the_sink_validator_refuses_a_directory_with_no_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero discovered manifests is the state `problems()`' own docstring argues against.

    That docstring records moving rules 2 and 3 from the *enabled* set to the *discovered* set,
    because iterating the enabled set "resolved zero drivers, bound zero config blocks and checked
    zero `*_env` names … a gate that could only fail on rule 1, which by construction was empty
    too." Zero discovered manifests reproduces that state exactly, one level out.
    """
    from chemclaw.cli.validate_sinks import problems
    from chemclaw.core.config import settings
    from chemclaw.publish.registry import discovered

    monkeypatch.setattr(settings, "result_sinks_dir", str(tmp_path))
    monkeypatch.setattr(settings, "result_sinks", "")
    discovered.cache_clear()
    try:
        found = problems()
    finally:
        discovered.cache_clear()
    assert found, "an empty sink directory must be a finding, not a pass"


def test_the_channel_validator_refuses_a_directory_with_no_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same state, same refusal — and here rule 4 is the plaintext-destination refusal."""
    from chemclaw.cli.validate_channels import problems
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "delivery_channels_dir", str(tmp_path))
    monkeypatch.setattr(settings, "delivery_channels", "")
    assert problems(), "an empty channel directory must be a finding, not a pass"


def test_a_malformed_manifest_is_a_problem_line_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two newest manifest gates raised where the two older ones report.

    `validate_connectors` and `validate_datasources` both answer unreadable YAML with
    `- <path>: unreadable or malformed …` and exit 1. `validate_sinks` let `ResultSinkError` out,
    and `validate_channels` let the bare `yaml.YAMLError` out — `deliver.registry._load` reads and
    parses without wrapping either, unlike `publish.registry._load`. The exit code was already 1
    in both cases; what an operator could act on was not.
    """
    from chemclaw.cli.validate_channels import problems as channel_problems
    from chemclaw.cli.validate_sinks import problems as sink_problems
    from chemclaw.core.config import settings
    from chemclaw.publish.registry import discovered

    malformed = "name: x\ndescription: y\ndriver: [a\n  b: c\n"
    sink = tmp_path / "sinks" / "postgres"
    sink.mkdir(parents=True)
    (sink / "sink.yaml").write_text(malformed, encoding="utf-8")
    channel = tmp_path / "channels" / "webhook"
    channel.mkdir(parents=True)
    (channel / "channel.yaml").write_text(malformed, encoding="utf-8")

    monkeypatch.setattr(settings, "result_sinks_dir", str(sink.parent))
    monkeypatch.setattr(settings, "result_sinks", "")
    monkeypatch.setattr(settings, "delivery_channels_dir", str(channel.parent))
    monkeypatch.setattr(settings, "delivery_channels", "")
    discovered.cache_clear()
    try:
        assert sink_problems(), "a malformed sink manifest must be a reported problem"
    finally:
        discovered.cache_clear()
    assert channel_problems(), "a malformed channel manifest must be a reported problem"
