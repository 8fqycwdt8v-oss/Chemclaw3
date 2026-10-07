"""Every validator entrypoint answers its command line rather than quietly ignoring it.

A validator that accepts a directory argument and validates the configured corpus instead reports
success about something it never looked at. The directory stays an environment variable (each is
a `PATH`-style list), so a positional argument is refused with a message naming the variable.
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
# An empty corpus, an empty manifest directory or a path that is not a directory is refused, since
# these directories are operator overrides and a typo would otherwise turn the gate off.
# --------------------------------------------------------------------------------------------


def test_the_graph_validator_refuses_a_corpus_with_no_notes_in_it(tmp_path: Path) -> None:
    """An empty notes directory is a gate that checked nothing, not a valid graph.

    This is the only check on `[[reaction-*]]` citations, which `dangling_links` deliberately
    ignores.
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
    """Zero discovered manifests is refused rather than reported as a pass.

    Rules 2 and 3 iterate the discovered set, so an empty set would check nothing.
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
    """A malformed manifest is a problem line and exit 1, not a traceback.

    Matches `validate_connectors` and `validate_datasources`; `deliver.registry._load` does not wrap
    `yaml.YAMLError` itself.
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
