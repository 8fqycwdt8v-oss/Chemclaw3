"""Record the architecture programme's baseline: the numbers every later wave is judged against.

Takes each wave's exit-criterion measurement (`tasks/todo.md`) and writes them as JSON. Offline: the
agent is compiled over a scripted chat model, enough to count middleware and time the build.

Usage: `make architecture-baseline` (writes `docs/planning/architecture-baseline-<date>.json`).
"""

from __future__ import annotations

import ast
import io
import json
import re
import subprocess
import sys
import time
import tokenize
from datetime import date
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src" / "chemclaw"


def _import_seconds(module: str) -> float:
    """Wall time of importing `module` in a fresh interpreter."""
    start = time.perf_counter()
    subprocess.run([sys.executable, "-c", f"import {module}"], check=True, cwd=REPO)
    return round(time.perf_counter() - start, 2)


def _prose_split(path: Path) -> tuple[int, int, int]:
    """Count (docstring, comment, code) lines of one module; blank lines count as none."""
    text = path.read_text(encoding="utf-8")
    doc_lines: set[int] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    end = body[0].end_lineno or body[0].lineno
                    doc_lines.update(range(body[0].lineno, end + 1))
    comment_lines: set[int] = set()
    code_lines: set[int] = set()
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type == tokenize.COMMENT:
            comment_lines.add(tok.start[0])
        elif tok.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
            code_lines.update(range(tok.start[0], tok.end[0] + 1))
    code_lines -= doc_lines
    comment_lines -= doc_lines | code_lines
    return len(doc_lines), len(comment_lines), len(code_lines)


def _prose_by_package() -> dict[str, dict[str, float]]:
    """Prose share and code LOC per top-level `chemclaw` package, plus a total."""
    totals: dict[str, list[int]] = {}
    for path in SRC.rglob("*.py"):
        package = path.relative_to(SRC).parts[0].removesuffix(".py")
        doc, comment, code = _prose_split(path)
        bucket = totals.setdefault(package, [0, 0, 0])
        bucket[0] += doc
        bucket[1] += comment
        bucket[2] += code
    out: dict[str, dict[str, float]] = {}
    grand = [0, 0, 0]
    for package, (doc, comment, code) in sorted(totals.items()):
        prose = doc + comment
        out[package] = {"code_lines": code, "prose_share": round(prose / max(prose + code, 1), 3)}
        grand = [grand[0] + doc, grand[1] + comment, grand[2] + code]
    prose = grand[0] + grand[1]
    share = round(prose / max(prose + grand[2], 1), 3)
    out["__total__"] = {"code_lines": grand[2], "prose_share": share}
    return out


def _agent_build() -> dict[str, Any]:
    """Time the first and a steady-state compile, and count the root graph's middleware."""
    import deepagents.graph as deep_graph
    from langchain_core.language_models.fake_chat_models import (
        GenericFakeChatModel,
    )

    from chemclaw.agent.langgraph_agent import build_langgraph_agent
    from chemclaw.agent.turn_ambient import turn_caps
    from chemclaw.agent.turn_usage import TurnUsage

    class _Model(GenericFakeChatModel):
        def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
            return self

    seen: list[int] = []
    original = getattr(deep_graph, "create_agent")  # noqa: B009 - not an explicit export

    def _counting(*args: Any, **kwargs: Any) -> Any:
        seen.append(len(kwargs.get("middleware", ())))
        return original(*args, **kwargs)

    setattr(deep_graph, "create_agent", _counting)  # noqa: B010
    try:
        timings = []
        for _ in range(4):
            start = time.perf_counter()
            with turn_caps(TurnUsage()):
                build_langgraph_agent(_Model(messages=iter([])))
            timings.append(round(time.perf_counter() - start, 3))
    finally:
        setattr(deep_graph, "create_agent", original)  # noqa: B010
    return {
        "first_build_s": timings[0],
        "steady_build_s": min(timings[1:]),
        "root_middleware": seen[0] if seen else None,
    }


def _count_lines(paths: list[Path]) -> int:
    """Total line count over `paths`."""
    return sum(len(p.read_text(encoding="utf-8", errors="replace").splitlines()) for p in paths)


def _static() -> dict[str, Any]:
    """Sizes of the documents and declarations the programme shrinks."""
    md = [p for p in REPO.rglob("*.md") if ".venv" not in p.parts and "node_modules" not in p.parts]
    settings_fields = 0
    for path in (SRC / "core" / "config").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                settings_fields += sum(isinstance(s, ast.AnnAssign) for s in node.body)
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    env_names = set()
    for path in SRC.rglob("*.py"):
        env_names.update(re.findall(r"CHEMCLAW_[A-Z0-9_]+", path.read_text(encoding="utf-8")))
    return {
        "claude_md_lines": _count_lines([REPO / "CLAUDE.md"]),
        "lessons_md_lines": _count_lines([REPO / "tasks" / "lessons.md"]),
        "markdown_lines": _count_lines(md),
        "adr_files": len(list((REPO / "docs" / "decisions").glob("D-*.md"))),
        "tasks_files": sum(1 for p in (REPO / "tasks").rglob("*") if p.is_file()),
        "test_files": len(list((REPO / "tests").rglob("test_*.py"))),
        "test_lines": _count_lines(list((REPO / "tests").rglob("*.py"))),
        "src_lines": _count_lines(list(SRC.rglob("*.py"))),
        "make_targets": len(re.findall(r"^[a-z][a-z0-9-]*:", makefile, re.M)),
        "settings_fields": settings_fields,
        "chemclaw_env_names_in_src": len(env_names),
        "helm_values_lines": _count_lines([REPO / "deploy" / "helm" / "chemclaw" / "values.yaml"]),
    }


def main() -> None:
    """Measure and write the baseline JSON; print its path."""
    result = {
        "date": date.today().isoformat(),
        "import_s": {
            "api_app_cold_first": _import_seconds("chemclaw.api.app"),
            "api_app_cold_second": _import_seconds("chemclaw.api.app"),
        },
        "agent_build": _agent_build(),
        "prose": _prose_by_package(),
        "static": _static(),
    }
    out = REPO / "docs" / "planning" / f"architecture-baseline-{result['date']}.json"
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
