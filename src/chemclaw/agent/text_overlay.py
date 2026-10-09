"""Replacement model-facing text read from a directory, so a candidate is measured unshipped.

A model-text batch is evaluated against the shipped text (`cli/model_text_eval.py`). The candidate
arm is a front door started with `CHEMCLAW_MODEL_TEXT_OVERLAY_DIR` naming a tree of replacements;
nothing in the repository is edited. Layout, one file per text:

- `tools/<tool name>.txt` — the tool's description (first-party and connector tools alike);
- `blocks/<index>.txt` and `safety/<index>.txt` — a prompt block of `_INSTRUCTION_BLOCKS` or
  `_SAFETY_BLOCKS`, by position.

Invariants: an overlay is process-wide and read-only; a block keeps the shipped block's leading and
trailing whitespace, since that separator is what a dropped block leaves no seam with; anything
outside the layout, or empty, is refused rather than ignored or applied as a blank, because a
file that silently applies nowhere would make the candidate arm a second control. Argument
descriptions in a tool's schema are not overlaid: they live in code, and a batch that changes them
is run from a checkout.
"""

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Final, Literal

from langchain_core.tools import BaseTool

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError

logger = logging.getLogger(__name__)

BlockGroup = Literal["blocks", "safety"]

_GROUPS: Final[tuple[BlockGroup, ...]] = ("blocks", "safety")
_SUFFIX: Final = ".txt"

#: How much of the digest a readiness body and a log line carry: enough to tell two overlays apart.
DIGEST_CHARS: Final = 16


@dataclass(frozen=True)
class Overlay:
    """One loaded overlay: the replacements by kind, and a digest naming exactly this content."""

    tools: dict[str, str]
    blocks: dict[BlockGroup, dict[int, str]]
    digest: str

    def __len__(self) -> int:
        """How many texts the overlay replaces."""
        return len(self.tools) + sum(len(group) for group in self.blocks.values())


def _text_files(root: Path, directory: str) -> dict[Path, bytes]:
    """The files of one layout directory, each read once; a stray or empty file is refused.

    Empty is refused because an empty replacement would blank the text it replaces, which is never
    what a candidate means and would read as a prompt with a hole in it.
    """
    found: dict[Path, bytes] = {}
    for path in sorted((root / directory).iterdir()) if (root / directory).is_dir() else []:
        if not path.is_file() or path.suffix != _SUFFIX:
            raise ChemclawError(
                f"model-text overlay {root}: {directory}/{path.name} is not a <name>{_SUFFIX} file"
            )
        content = path.read_bytes()
        if not content.decode("utf-8").strip():
            raise ChemclawError(f"model-text overlay {root}: {directory}/{path.name} is empty")
        found[path] = content
    return found


@cache
def load_overlay(root: str) -> Overlay:
    """Read the overlay tree at `root`, refusing a layout it cannot apply.

    Each file is read once and the digest is computed from those same bytes, so it names exactly
    the text applied.

    Raises:
        ChemclawError: `root` is not a directory, holds an entry outside the layout or an empty
            file, or names a block by something other than a non-negative integer.
    """
    base = Path(root)
    if not base.is_dir():
        raise ChemclawError(f"model-text overlay {root!r} is not a directory")
    layout = {"tools", *_GROUPS}
    stray = sorted(entry.name for entry in base.iterdir() if entry.name not in layout)
    if stray:
        raise ChemclawError(
            f"model-text overlay {root}: {stray} are outside the layout {sorted(layout)}"
        )
    read = {directory: _text_files(base, directory) for directory in ("tools", *_GROUPS)}
    tools = {path.stem: raw.decode("utf-8").strip() for path, raw in read["tools"].items()}
    blocks: dict[BlockGroup, dict[int, str]] = {"blocks": {}, "safety": {}}
    for group in _GROUPS:
        for path, raw in read[group].items():
            if not path.stem.isdecimal():
                raise ChemclawError(
                    f"model-text overlay {root}: {group}/{path.name} must be named by block index"
                )
            blocks[group][int(path.stem)] = raw.decode("utf-8").strip()
    digest = hashlib.sha256()
    for files in read.values():
        for path, raw in files.items():
            digest.update(path.relative_to(base).as_posix().encode())
            digest.update(b"\0" + raw + b"\0")
    return Overlay(tools=tools, blocks=blocks, digest=digest.hexdigest())


def active() -> Overlay | None:
    """The overlay this process runs under, or `None` when `model_text_overlay_dir` is empty."""
    root = settings.model_text_overlay_dir
    return load_overlay(root) if root else None


def block_text(group: BlockGroup, index: int, shipped: str) -> str:
    """The text of one prompt block: the overlay's, set in the shipped block's own separators."""
    overlay = active()
    if overlay is None or index not in overlay.blocks[group]:
        return shipped
    lead = shipped[: len(shipped) - len(shipped.lstrip())]
    tail = shipped[len(shipped.rstrip()) :]
    return f"{lead}{overlay.blocks[group][index]}{tail}"


def overlaid(tools: Sequence[BaseTool]) -> list[BaseTool]:
    """`tools` with the overlay's descriptions applied; copies, never edits a shared tool.

    The copy matters because a first-party `BaseTool` is cached once per process
    (`tool_schema.as_structured_tool`) and would otherwise carry the candidate text everywhere.
    """
    overlay = active()
    if overlay is None:
        return list(tools)
    return [
        tool.model_copy(update={"description": overlay.tools[tool.name]})
        if tool.name in overlay.tools
        else tool
        for tool in tools
    ]


def check_applies(tool_names: Sequence[str], block_counts: dict[BlockGroup, int]) -> None:
    """Refuse an overlay that names a tool or block this deployment does not have, and say so once.

    Called at startup with the whole surface. Without it a misspelt file would apply nowhere and the
    candidate arm would be a second control.

    Raises:
        ChemclawError: A file names a tool that is not on the surface or a block index out of range.
    """
    overlay = active()
    if overlay is None:
        return
    unknown = sorted(set(overlay.tools) - set(tool_names))
    if unknown:
        raise ChemclawError(
            f"model-text overlay names tools this deployment does not bind: {unknown}"
        )
    for group in _GROUPS:
        beyond = sorted(index for index in overlay.blocks[group] if index >= block_counts[group])
        if beyond:
            raise ChemclawError(
                f"model-text overlay {group}/ names blocks {beyond}; "
                f"there are {block_counts[group]}"
            )
    logger.warning(
        "running under a model-text overlay (%d text(s), sha256 %s) — this process is a candidate "
        "arm, not the shipped text",
        len(overlay),
        overlay.digest,
    )
