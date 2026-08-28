"""The argument check `make template-validate` cannot make, taken against running servers.

`python -m chemclaw.cli.validate_template_args_live`, the live-lane half of the template gate.

A template is a *pinned* procedure, so a step passing an argument its tool does not take is a run
that spends compute and then fails on step four. `make template-validate` checks that offline by
reading a signature out of this tree — and for a bundle we **declare but do not run** there is no
signature here to read. Seven shipped tool steps are in that state today (`chem`'s five
enumerations, `safety`'s two `screen_hazards` calls): name-checked, arguments unchecked, and the
offline gate says so in a note rather than pretending otherwise.

**The missing authority is a live session, and there is exactly one.** A running MCP server
advertises each tool's `args_schema`, which is what the model is handed and what the call is
validated against — so this opens the connectors for real
(`chemclaw.connectors.registry.open_connector_specs`, the same function every turn uses) and checks
the same argument rule against what actually answered. The rule itself is not re-implemented:
`ToolArguments` and `argument_problems` come from `chemclaw.cli.validate_templates`, so both lanes
give the same verdict in the same words and only the authority differs.

**Why this is a live-lane target and not a `ci` one.** It needs a network, and `ci` must not: the
row that asked for this proposed putting it in `make connector-validate`, which is inside `ci` and
imports the bundle's *local* module — so it would have answered `[]` for exactly the bundles in
question while looking like it had checked them. `make template-validate` stays offline, keeps its
note, and this runs beside `make live-probes` against a deployment.

**What it refuses to do is count an unreached connector as checked.** That is
`D-2026-08-17-a-harness-that-starts-two-of-five-servers-is-a-harness-that-tests-two` exactly: a
harness that starts some of the fleet and prints one green line is a harness that tested some of
the fleet and said nothing about the rest. So the report has three parts, not two — the steps it
checked, the problems it found, and the steps it could not reach — and a run that reached nothing
exits non-zero with a distinct code, because "no problems found" over an empty check is the
sentence this whole module exists to prevent.

Read-only; it opens sessions and calls no tool.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import Any, NamedTuple

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from chemclaw.cli.chat import resolve_identity
from chemclaw.cli.validate_templates import ToolArguments, argument_problems
from chemclaw.connectors.registry import enabled, mcp_connections, open_connector_specs
from chemclaw.connectors.transport import ConnectorSpec
from chemclaw.core.config import settings
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.logging import configure_logging
from chemclaw.templates.manifest import Template, ToolStep
from chemclaw.templates.registry import discovered

logger = logging.getLogger(__name__)

# Exit 1 is "a template is wrong", exit 3 is "this run is not evidence". Two codes because they ask
# for two different actions — fix the template, or start the server and run it again — and one code
# would collapse them. 2 is skipped because `argparse` already owns it for a usage error.
EXIT_MISMATCH = 1
EXIT_INCOMPLETE = 3


class LiveReport(NamedTuple):
    """The three things a run of this check has to say, kept apart on purpose.

    `checked` is what the run is evidence about, `problems` is what it found, and `unreached` is
    what it is *not* evidence about. Folding the third into silence is the failure
    `D-2026-08-17-a-harness-that-starts-two-of-five-servers-is-a-harness-that-tests-two` names.
    """

    problems: list[str]
    checked: list[str]
    unreached: dict[str, list[str]]
    """Connector name -> the template steps it owed an answer for and did not give one."""


def _live_arguments(tool: BaseTool) -> ToolArguments:
    """Read what a *running* tool accepts, off the schema its session advertised.

    `tool_call_schema` rather than `args_schema`, because it is the shape the model is offered:
    injected arguments are already removed from it. MCP tools arrive with a plain JSON-schema dict
    (`langchain_mcp_adapters` converts the server's declaration); an in-process `@tool` arrives as a
    pydantic model. Both are handled, and both reduce to the same three facts.
    """
    schema: Any = tool.tool_call_schema
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        schema = schema.model_json_schema()
    properties: dict[str, Any] = schema.get("properties", {})
    required: list[str] = schema.get("required", [])
    return ToolArguments(
        accepted=frozenset(properties),
        required=frozenset(required),
        # An open schema absorbs any key, so the unknown-key half is vacuous — the same reduction
        # `**kwargs` gets offline. Only a literal `True` counts: a schema saying nothing about it
        # is closed, which is what `additionalProperties` absent means for a tool declaration.
        takes_any_key=schema.get("additionalProperties") is True,
    )


def connector_owners() -> dict[str, str]:
    """Every endpoint tool an enabled connector serves, mapped to the connector's name.

    This is the set whose arguments a live session can answer for, and the set the offline gate can
    only sometimes answer for. In-process tools are deliberately absent: their signatures are in
    this tree, `make template-validate` checks them there, and checking them again here would make
    a second answer to a settled question.

    One name cannot belong to two connectors — `registry._declared_tool_names` raises on that at
    load — so a flat mapping is sound rather than lossy.
    """
    return {
        tool: manifest.name
        for manifest in enabled()
        if manifest.endpoint is not None
        for tool in manifest.endpoint.tools
    }


def check_live_arguments(
    templates: Iterable[Template],
    owners: Mapping[str, str],
    live_tools: Mapping[str, BaseTool],
    unreachable: Collection[str],
) -> LiveReport:
    """Check every connector-served tool step against the tool as the running server describes it.

    Pure, so the whole decision is testable without a fleet: `main` supplies the live half and this
    supplies the judgment. Four outcomes per step, and each is recorded as itself:

    1. **Not a connector tool** — skipped silently. It has a local signature and the offline gate
       owns it.
    2. **Its connector did not come up** — recorded in `unreached` under that connector. Never a
       problem (nothing is known to be wrong) and never a pass (nothing was checked).
    3. **Its connector came up without it** — a problem. The template names a tool that server does
       not serve, which is a run that fails at the call, and the manifest saying otherwise is the
       drift both `connector.yaml` files warn about and no offline gate can see for these bundles.
    4. **It is there** — the argument rule applies, in `argument_problems`' words.

    Args:
        templates: The templates to check, normally `templates.registry.discovered().values()`.
        owners: Tool name -> connector name, from `connector_owners`.
        live_tools: Tool name -> the tool a reachable connector advertised.
        unreachable: The connectors that did not come up (`open_connector_specs`' second return).

    Returns:
        The problems found, the steps checked, and the steps left unchecked by connector.
    """
    problems: list[str] = []
    checked: list[str] = []
    unreached: dict[str, list[str]] = {}
    for template in templates:
        for step in template.steps:
            if not isinstance(step, ToolStep):
                continue
            connector = owners.get(step.tool)
            if connector is None:
                continue
            where = f"{template.name}/{step.id} -> {step.tool}"
            if connector in unreachable:
                unreached.setdefault(connector, []).append(where)
                continue
            live = live_tools.get(step.tool)
            if live is None:
                problems.append(
                    f"template {template.name!r} step {step.id!r} names tool {step.tool!r}, which "
                    f"connector {connector!r} declares and its running server does not serve"
                )
                continue
            problems.extend(argument_problems(template, step, _live_arguments(live)))
            checked.append(f"{where} ({connector})")
    return LiveReport(problems=problems, checked=checked, unreached=unreached)


def _specs_for(owners: Mapping[str, str], needed: Collection[str]) -> list[ConnectorSpec]:
    """The connection specs for just the connectors some template step actually names.

    Opening the whole enabled set would pay a connect timeout for every connector this check has no
    question for, and — worse — would report those as `unreached`, turning an honest signal about
    coverage into noise nobody reads. `owners` is passed rather than re-derived so the set this
    opens and the set `check_live_arguments` judges are the same one.
    """
    wanted = {owners[tool] for tool in needed if tool in owners}
    return [spec for spec in mcp_connections() if spec.name in wanted]


async def run() -> LiveReport:
    """Open the connectors the shipped templates name and check their steps against them.

    Identity comes from the CLI's own seam (`cli.chat.resolve_identity`) because the connector
    client stamps `X-Chemclaw-Actor` on every request and a server logs it; there is no anonymous
    way to open a session, and inventing an actor label here rather than reusing the one the CLI
    already resolves would put a second identity story in the tree.
    """
    templates = list(discovered().values())
    owners = connector_owners()
    named = {step.tool for t in templates for step in t.steps if isinstance(step, ToolStep)}
    actor, roles = resolve_identity(admin=True, actor=None)
    token = set_current_identity(actor, roles)
    try:
        async with contextlib.AsyncExitStack() as stack:
            tools, unreachable = await open_connector_specs(stack, _specs_for(owners, named))
            return check_live_arguments(
                templates, owners, {tool.name: tool for tool in tools}, unreachable
            )
    finally:
        reset_current_identity(token)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the live argument check and print all three parts of what it found.

    Parses even though it declares no option, for the reason its siblings do: an argument this
    cannot honour is refused rather than discarded under a green line. The knobs are
    `CHEMCLAW_TEMPLATES_DIR` and `CHEMCLAW_CONNECTOR_URLS`.
    """
    argparse.ArgumentParser(
        prog="python -m chemclaw.cli.validate_template_args_live",
        description="Check every template's tool arguments against the running connector servers. "
        "Needs the servers up; set CHEMCLAW_CONNECTOR_URLS for a deployment's addresses.",
    ).parse_args(argv)
    configure_logging()
    report = asyncio.run(run())
    for where in report.checked:
        print(f"checked {where}")
    for connector, steps in sorted(report.unreached.items()):
        print(
            f"UNREACHED: connector {connector!r} did not come up — {len(steps)} template step(s) "
            "were NOT checked:"
        )
        for step in steps:
            print(f"  - {step}")
    if report.problems:
        print("live template argument validation failed:")
        for problem in report.problems:
            print(f"  - {problem}")
        return EXIT_MISMATCH
    if report.unreached or not report.checked:
        # The green line is withheld deliberately. A pass here would be a claim about steps this
        # run never looked at — the whole point of D-2026-08-17.
        print(
            f"live template argument validation INCOMPLETE: {len(report.checked)} step(s) checked, "
            f"{sum(len(s) for s in report.unreached.values())} unreached "
            f"(templates from {settings.templates_dir!r})"
        )
        return EXIT_INCOMPLETE
    print(f"live template argument validation passed: {len(report.checked)} step(s) checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
