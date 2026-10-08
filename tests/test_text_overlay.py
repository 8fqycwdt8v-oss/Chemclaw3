"""The model-text overlay: a candidate arm runs replacement text without a single repository edit.

The point of the mechanism is that a batch can be measured before it is committed, so these tests
drive the real prompt assembly and the real tool binding with an overlay in place and compare them
with the shipped text.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from chemclaw.agent import text_overlay
from chemclaw.agent.chemclaw_agent import (
    _INSTRUCTION_BLOCKS,
    _SAFETY_BLOCKS,
    _assemble,
    refuse_a_misapplied_overlay,
)
from chemclaw.agent.langgraph_agent import _bound_surface
from chemclaw.agent.profiles import get_profile
from chemclaw.agent.tool_schema import as_structured_tool
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.core.tool_registry import registered_tools


def _write(root: Path, relative: str, text: str) -> None:
    """One overlay file, parents created."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def _fresh_overlay_cache() -> Iterator[None]:
    """`load_overlay` is cached by path; no test may see another's tree."""
    text_overlay.load_overlay.cache_clear()
    yield
    text_overlay.load_overlay.cache_clear()


@pytest.fixture
def overlay_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty overlay directory that `settings` points at for the test."""
    monkeypatch.setattr(settings, "model_text_overlay_dir", str(tmp_path))
    return tmp_path


def test_without_an_overlay_nothing_changes() -> None:
    """The default is the shipped text, byte for byte."""
    assert settings.model_text_overlay_dir == ""
    assert text_overlay.active() is None
    assert text_overlay.block_text("blocks", 0, "shipped ") == "shipped "
    tools = [as_structured_tool(fn) for fn in registered_tools()[:3]]
    assert text_overlay.overlaid(tools) == tools


def test_a_replaced_block_keeps_the_shipped_blocks_separators(overlay_dir: Path) -> None:
    """A block carries its own trailing space so a dropped neighbour leaves no seam."""
    _write(overlay_dir, "blocks/0.txt", "New first block.\n")
    shipped = _INSTRUCTION_BLOCKS[0].text
    replaced = text_overlay.block_text("blocks", 0, shipped)
    assert replaced.startswith("New first block.")
    assert replaced.endswith(shipped[len(shipped.rstrip()) :])
    assert text_overlay.block_text("blocks", 1, "other") == "other"


def test_the_assembled_prompt_uses_the_overlay_in_each_group(overlay_dir: Path) -> None:
    """Both groups assemble through the overlay, safety floor included."""
    _write(overlay_dir, "blocks/0.txt", "CANDIDATE-BLOCK")
    _write(overlay_dir, "safety/1.txt", "CANDIDATE-SAFETY")
    assert "CANDIDATE-BLOCK" in _assemble(_INSTRUCTION_BLOCKS, None, durable_trail=True)
    assert "CANDIDATE-SAFETY" in _assemble(_SAFETY_BLOCKS, None, durable_trail=True, group="safety")
    assert "CANDIDATE-SAFETY" not in _assemble(_INSTRUCTION_BLOCKS, None, durable_trail=True)


def test_a_tool_description_is_replaced_on_a_copy_not_the_shared_tool(overlay_dir: Path) -> None:
    """The cached first-party tool stays shipped, or one graph's candidate leaks into the next."""
    fn = registered_tools()[0]
    shared = as_structured_tool(fn)
    _write(overlay_dir, f"tools/{shared.name}.txt", "Candidate description.\n")
    [seen] = text_overlay.overlaid([shared])
    assert seen.description == "Candidate description."
    assert shared.description != "Candidate description."
    assert seen.args_schema is shared.args_schema


def test_the_bound_surface_carries_the_overlay_for_first_party_tools(overlay_dir: Path) -> None:
    """The point where a graph takes its tools applies the overlay, not only the helper above."""
    fn = registered_tools()[0]
    name = as_structured_tool(fn).name
    _write(overlay_dir, f"tools/{name}.txt", "Candidate description.")
    bound = {tool.name: tool for tool in _bound_surface([fn], None)}
    assert bound[name].description == "Candidate description."


def test_the_digest_names_exactly_the_content(overlay_dir: Path) -> None:
    """Two overlays with different text have different digests; the same text has the same one."""
    _write(overlay_dir, "blocks/0.txt", "one")
    first = text_overlay.load_overlay(str(overlay_dir)).digest
    _write(overlay_dir, "blocks/0.txt", "two")
    text_overlay.load_overlay.cache_clear()
    assert text_overlay.load_overlay(str(overlay_dir)).digest != first
    _write(overlay_dir, "blocks/0.txt", "one")
    text_overlay.load_overlay.cache_clear()
    assert text_overlay.load_overlay(str(overlay_dir)).digest == first


@pytest.mark.parametrize(
    ("relative", "why"),
    [
        ("skills/x/SKILL.md", "outside the layout"),
        ("tools/notes.md", "is not a <name>.txt file"),
        ("blocks/first.txt", "must be named by block index"),
    ],
)
def test_a_file_outside_the_layout_is_refused_not_ignored(
    overlay_dir: Path, relative: str, why: str
) -> None:
    """A file that applies nowhere would turn the candidate arm into a second control."""
    _write(overlay_dir, relative, "text")
    text_overlay.load_overlay.cache_clear()
    with pytest.raises(ChemclawError, match=why):
        text_overlay.active()


def test_a_missing_directory_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A mistyped path must not start a front door on the shipped text."""
    monkeypatch.setattr(settings, "model_text_overlay_dir", str(tmp_path / "absent"))
    with pytest.raises(ChemclawError, match="not a directory"):
        text_overlay.active()


def test_startup_refuses_an_overlay_naming_a_tool_or_block_that_does_not_exist(
    overlay_dir: Path,
) -> None:
    """A misspelt file is caught when the front door starts, with the name in the message."""
    _write(overlay_dir, "tools/no_such_tool.txt", "text")
    with pytest.raises(ChemclawError, match="no_such_tool"):
        refuse_a_misapplied_overlay()
    (overlay_dir / "tools" / "no_such_tool.txt").unlink()
    _write(overlay_dir, f"blocks/{len(_INSTRUCTION_BLOCKS)}.txt", "text")
    text_overlay.load_overlay.cache_clear()
    with pytest.raises(ChemclawError, match="names blocks"):
        refuse_a_misapplied_overlay()


def test_startup_accepts_an_overlay_that_applies(overlay_dir: Path) -> None:
    """A real tool and a real block pass the startup check."""
    name = as_structured_tool(registered_tools()[0]).name
    _write(overlay_dir, f"tools/{name}.txt", "text")
    _write(overlay_dir, "blocks/0.txt", "text")
    refuse_a_misapplied_overlay()
    assert get_profile(None) is not None
