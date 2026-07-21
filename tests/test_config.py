"""Behavioral tests for the single config source (plan step 0.3, gate G3).

These prove the two contracts the rest of the system relies on: sane defaults
load with no `.env`, and any value is overridable via a prefixed env var.
"""

import os
from collections.abc import Iterator

import pytest

from chemclaw.config import Settings


def test_defaults_load_without_env() -> None:
    """A fresh checkout with no `.env` yields the documented dev defaults."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.temporal_address == "localhost:7233"
    assert settings.hpc_task_queue == "hpc-jobs"
    assert settings.background_task_queue == "background-jobs"
    assert settings.postgres_dsn.startswith("postgresql://")


def test_env_var_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `CHEMCLAW_`-prefixed env var overrides the field it maps to."""
    monkeypatch.setenv("CHEMCLAW_TEMPORAL_ADDRESS", "temporal.internal:7233")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.temporal_address == "temporal.internal:7233"


def test_unknown_field_is_rejected() -> None:
    """`extra="forbid"` turns a typo'd setting into a startup error, not a silent no-op."""
    with pytest.raises(ValueError):
        Settings(_env_file=None, unknown_setting="x")  # type: ignore[call-arg]


def test_skills_dirs_splits_the_path_list() -> None:
    """`skills_dirs` splits `skills_dir` on the OS path separator (like PATH), dropping empties."""
    single = Settings(_env_file=None)  # type: ignore[call-arg]
    assert single.skills_dirs == ["skills"]  # the default is one directory

    multi = Settings(_env_file=None, skills_dir=os.pathsep.join(["skills", "/opt/team"]))  # type: ignore[call-arg]
    assert multi.skills_dirs == ["skills", "/opt/team"]

    # A trailing separator (an easy admin typo) yields no empty entry.
    trailing = Settings(_env_file=None, skills_dir="skills" + os.pathsep)  # type: ignore[call-arg]
    assert trailing.skills_dirs == ["skills"]


def test_absolute_knowledge_dir_is_rejected() -> None:
    """An absolute `knowledge_dir` fails at startup (it would escape the note repo)."""
    with pytest.raises(ValueError, match="knowledge_dir must be relative"):
        Settings(_env_file=None, knowledge_dir="/etc/knowledge")  # type: ignore[call-arg]


def test_relative_knowledge_dir_is_accepted() -> None:
    """A relative `knowledge_dir` (the default kind) loads fine."""
    assert Settings(_env_file=None, knowledge_dir="knowledge").knowledge_dir == "knowledge"  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def _clear_prefixed_env() -> Iterator[None]:
    """Isolate each test from any CHEMCLAW_* vars present in the ambient shell."""
    saved = {k: v for k, v in os.environ.items() if k.startswith("CHEMCLAW_")}
    for key in saved:
        del os.environ[key]
    yield
    os.environ.update(saved)
