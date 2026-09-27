"""Scratch: drive the shipped helper-file bound at shipped widths with realistic helper outputs."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from deepagents.backends.utils import create_file_data
from langchain_core.messages import AIMessage
from langgraph.types import Command

from chemclaw.agent.tool_result_size import bound_tool_results
from chemclaw.core.config import settings

notes = sorted(len(p.read_text()) for p in Path("knowledge").rglob("*.md"))
sizes = {
    "median note in knowledge/": notes[len(notes) // 2],
    "largest note in knowledge/": notes[-1],
    "largest helper report measured (70,048)": 70_048,
    "half the budget": settings.agent_subagent_files_max_chars // 2,
}
print("budget", settings.agent_subagent_files_max_chars, "notes", len(notes))


def drive(width: int, size: int) -> int:
    asked = AIMessage(
        content="",
        tool_calls=[
            {"name": "task", "args": {}, "id": f"t-{i}", "type": "tool_call"} for i in range(width)
        ],
    )
    note = "z" * size

    async def _handler(_request: Any) -> Any:
        return Command(update={"files": {"/scratch/helper-note.md": create_file_data(note)}})

    request = SimpleNamespace(tool_call={"id": "t-0", "name": "task"}, state={"messages": [asked], "files": {}})
    bounded = asyncio.run(bound_tool_results.awrap_tool_call(request, _handler))  # type: ignore[arg-type]
    return sum(len(str(d.get("content", ""))) for d in bounded.update["files"].values())


for label, size in sizes.items():
    row = [f"{label} ({size})"]
    for width in (1, 2, 3, 4, 8):
        landed = drive(width, size)
        row.append(f"w{width}: {'whole' if landed == size else f'CUT->{landed}'}")
    print(" | ".join(row))
