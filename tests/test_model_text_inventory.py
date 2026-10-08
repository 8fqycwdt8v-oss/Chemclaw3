"""`schema/model-text/inventory.json` is every string a model reads, and it may not go stale.

The staleness test is a tripwire on purpose: any edit to model-facing text (a tool docstring, a
schema description, a prompt block, a skill) changes the inventory, so the pull request that makes
it carries an inventory diff, and the text-edit evaluation
(`D-2026-10-08-model-facing-text-changes-ship-behind-an-evaluation`) cannot be skipped unnoticed.
"""

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from chemclaw.agent import text_overlay
from chemclaw.agent.chemclaw_agent import _capability_tools, withheld_tool_names
from chemclaw.agent.profile_discovery import load_profiles
from chemclaw.cli.model_text_inventory import (
    INVENTORY_PATH,
    RESIDENCES,
    SURFACE_SETTINGS,
    build_inventory,
    canonical,
    diff,
    main,
    model_facing_descriptions,
    running_python,
    target_python,
    tokens,
)
from chemclaw.connectors.registry import discovered
from chemclaw.core.config import settings
from chemclaw.core.tool_registry import registered_tool_names
from tests.test_context_floor import CEILINGS, _floor

_ROOT = Path(__file__).resolve().parents[1]

load_profiles()


@pytest.fixture(scope="module")
def current() -> dict[str, Any]:
    """The inventory as this tree would write it now."""
    return build_inventory()


def _committed() -> dict[str, Any]:
    loaded = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_the_committed_inventory_is_current(current: dict[str, Any]) -> None:
    """Any edit to model-facing text shows up here, naming what moved and how to record it."""
    committed = _committed()
    if committed.get("python") != running_python():
        pytest.skip(
            f"the inventory is measured under Python {committed.get('python')} (the image's); "
            f"this is {running_python()}, where docstrings are dedented at compile time and every "
            "tool costs fewer tokens. Run it with `uv run --python "
            f"{target_python()} pytest tests/test_model_text_inventory.py`."
        )
    changes = diff(committed, current)
    assert committed == current, (
        "schema/model-text/inventory.json is stale: model-facing text changed.\n"
        + "\n".join(changes[:40])
        + (f"\n… and {len(changes) - 40} more" if len(changes) > 40 else "")
        + "\nRun `make model-text` and commit the result. A change to what a model reads ships "
        "behind the evaluation in docs/guides/model-text-evaluation.md."
    )


def test_the_committed_inventory_was_measured_under_the_target_interpreter() -> None:
    """A file written under another interpreter would disagree with the image about every tool."""
    assert _committed()["python"] == target_python()


def test_an_edit_to_model_facing_text_moves_the_inventory(
    current: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The tripwire fires: one reworded prompt block and one reworded tool both show up in a diff.

    Driven through the model-text overlay, which edits nothing on disk, and compared with the same
    tree's own inventory so the check is independent of the interpreter.
    """
    tool = next(row["id"] for row in current["entries"] if row["kind"] == "tool-schema")
    (tmp_path / "blocks").mkdir()
    (tmp_path / "tools").mkdir()
    (tmp_path / "blocks" / "0.txt").write_text("A reworded first block.", encoding="utf-8")
    (tmp_path / "tools" / f"{tool.removeprefix('tool:')}.txt").write_text(
        "A reworded tool description.", encoding="utf-8"
    )
    monkeypatch.setattr(settings, "model_text_overlay_dir", str(tmp_path))
    text_overlay.load_overlay.cache_clear()
    try:
        edited = build_inventory()
    finally:
        text_overlay.load_overlay.cache_clear()
    changes = diff(current, edited)
    assert edited["overlay"] == text_overlay.load_overlay(str(tmp_path)).digest[:16]
    assert current["overlay"] is None
    assert any(line.startswith("~ block:0:") for line in changes), changes
    assert any(line.startswith(f"~ {tool}:") for line in changes), changes
    assert edited["prefix"]["total"] < current["prefix"]["total"]
    assert diff(current, current) == []


def test_every_entry_is_well_formed_unique_and_in_order(current: dict[str, Any]) -> None:
    """The file's shape is the contract anything reading the inventory relies on."""
    rows = current["entries"]
    ids = [row["id"] for row in rows]
    assert ids == sorted(ids), "entries are written in id order so the file diffs cleanly"
    assert len(ids) == len(set(ids))
    for row in rows:
        assert row["residence"] in RESIDENCES
        assert row["tokens"] > 0 and row["chars"] > 0, row["id"]
        assert len(row["sha256"]) == 16
        assert set(row["also_counted_in"] if "also_counted_in" in row else []) <= set(ids), row[
            "id"
        ]


def test_every_owner_names_a_file_that_exists(current: dict[str, Any]) -> None:
    """`file:symbol` must lead somewhere, or the inventory cannot tell an editor where to go."""
    missing = []
    for row in current["entries"]:
        owner = row["owner"]
        if owner.startswith("upstream:"):
            continue
        if not (_ROOT / owner.partition(":")[0]).is_file():
            missing.append(f"{row['id']} -> {owner}")
    assert not missing, missing


def test_the_inventory_reaches_every_class_of_text_the_model_reads(current: dict[str, Any]) -> None:
    """Each kind of model-facing text contributes, and the middleware's own tools are named."""
    kinds = {row["kind"] for row in current["entries"]}
    assert kinds >= {
        "tool-schema",
        "mcp-tool-schema",
        "upstream-tool-schema",
        "schema-class",
        "response-format-schema",
        "tool-docstring",
        "mcp-docstring",
        "prompt-block",
        "profile-instructions",
        "profile-description",
        "skill-description",
        "skill-body",
        "prose-constant",
        "mcp-output-schema",
    }
    by_id = {row["id"]: row for row in current["entries"]}
    for name in ("task", "write_todos", "read_file"):
        assert by_id[f"tool:{name}"]["kind"] == "upstream-tool-schema"
    assert by_id["tool:task"]["residence"] == "every-request"
    assert by_id["skill-body:computational-evidence"]["residence"] == "on-demand"


def test_a_text_inside_another_is_never_added_twice(current: dict[str, Any]) -> None:
    """A docstring is part of its tool's schema: it names it and adds nothing to a total."""
    rows = {row["id"]: row for row in current["entries"]}
    docstring = rows["doc:find_notes"]
    assert docstring["also_counted_in"] == ["tool:find_notes"]
    summed = dict.fromkeys(RESIDENCES, 0)
    for row in rows.values():
        if "also_counted_in" not in row:
            summed[row["residence"]] += row["tokens"]
    assert {name: current["totals"][name]["tokens"] for name in RESIDENCES} == summed


def test_the_framing_nonce_does_not_leak_into_the_file() -> None:
    """The envelope tag differs per process; the inventory must not."""
    from chemclaw.agent.framing import ENVELOPE_TAG

    nonce = ENVELOPE_TAG.removeprefix("retrieved-note-")
    text = f"inside <{ENVELOPE_TAG}> and [system {nonce}]"
    assert nonce not in canonical(text)
    assert tokens(text) == tokens(text.replace(nonce, "f" * len(nonce)))
    assert nonce not in INVENTORY_PATH.read_text(encoding="utf-8")


def test_the_inventory_prices_every_tool_the_context_floor_does(current: dict[str, Any]) -> None:
    """Per tool, the inventory and the ratchet's own measurement are the same number.

    The floor reads the compiled graph's `ToolNode`; the inventory reads it too, so this holds for
    every tool bound, not a sample. What the floor charges and the inventory does not enumerate is
    named, and the inventory's prefix may never exceed the floor's.
    """
    total, parts = _floor("default")
    floor_tools = {k.removeprefix("tool:"): v for k, v in parts.items() if k.startswith("tool:")}
    inventory_tools = {
        row["id"].removeprefix("tool:"): row["tokens"]
        for row in current["entries"]
        if row["id"].startswith("tool:")
    }
    assert inventory_tools == floor_tools
    assert current["prefix"]["tool_schemas"] == sum(floor_tools.values())
    assert current["prefix"]["instructions"] == parts["instructions"]
    assert current["prefix"]["total"] <= total <= CEILINGS["__default__"]


@pytest.fixture(autouse=True)
def _no_overlay_leaks() -> Iterator[None]:
    """No test in this module may leave an overlay loaded."""
    yield
    text_overlay.load_overlay.cache_clear()


def test_the_shipped_inventory_records_its_environment_and_no_overlay() -> None:
    """Two inventories are comparable only if these agree, and the shipped one is shipped text."""
    committed = _committed()
    assert committed["overlay"] is None
    assert set(committed["environment"]) == set(SURFACE_SETTINGS)
    assert all(
        not str(value).startswith("/") for value in committed["environment"]["connectors_dirs"]
    ), "paths inside the repository are written relative to it, so a checkout elsewhere agrees"


def test_the_shipped_inventory_is_not_written_under_an_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An overlay in the environment must not be committed as the shipped text's inventory."""
    (tmp_path / "blocks").mkdir()
    (tmp_path / "blocks" / "0.txt").write_text("Candidate.", encoding="utf-8")
    monkeypatch.setattr(settings, "model_text_overlay_dir", str(tmp_path))
    text_overlay.load_overlay.cache_clear()
    before = INVENTORY_PATH.read_bytes()
    assert main([]) == 2
    assert main(["--check"]) == 2
    message = capsys.readouterr().err
    assert "CHEMCLAW_MODEL_TEXT_OVERLAY_DIR is set" in message
    assert "--output" in message
    assert INVENTORY_PATH.read_bytes() == before


def test_a_model_facing_text_the_deployment_withholds_is_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The artefact tools are the graph's to withhold, and the reading follows the same source."""
    assert "create_exhibit" in model_facing_descriptions()
    monkeypatch.setattr(settings, "agent_exhibits_enabled", False)
    assert "create_exhibit" not in model_facing_descriptions()
    assert withheld_tool_names() >= {"create_exhibit", "revise_exhibit", "read_exhibit"}


def test_the_inventory_does_not_depend_on_what_an_earlier_build_registered(
    current: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry only grows: a graph built under a wider connector set leaves its launchers.

    Built under every bundle enabled first, then the inventory under the shipped configuration
    must still be the one committed, with no launcher listed twice or listed at all.
    """
    before = set(registered_tool_names())
    with monkeypatch.context() as wider:
        wider.setattr(settings, "connectors_enabled", os.pathsep.join(discovered()))
        _capability_tools()
        grown = set(registered_tool_names()) - before
    assert grown, "no launcher was registered by the wider build, so this test proves nothing"
    after = build_inventory()
    assert after == current
    assert not {f"doc:{name}" for name in grown} & {row["id"] for row in after["entries"]}
