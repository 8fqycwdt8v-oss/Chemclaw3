"""The argument check `make template-validate` cannot make, taken against running servers.

`python -m chemclaw.cli.validate_template_args_live`, the live-lane half of the template gate. For
a bundle declared here but served elsewhere there is no local signature, so this opens the
connectors for real (`connectors.registry.open_connector_specs`, as a turn does) and checks each
step's arguments against the `args_schema` the server advertises. The rule (`ToolArguments`,
`argument_problems`) is shared with `validate_templates`, so both lanes answer in the same words.

Not in `ci`, because it needs a network. An unreached connector is never counted as checked
(`D-2026-08-17-a-harness-that-starts-two-of-five-servers-is-a-harness-that-tests-two`): the report
lists what was checked, what was wrong and what was unreached, and a run that reached nothing exits
with a distinct code.

Read-only; it opens sessions and calls no tool.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import NamedTuple

from langchain_core.tools import BaseTool

from chemclaw.agent.template_surface import normalise_tool_schema
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

# Exit 1 means "a template is wrong" (fix it); exit 3 means "this run is not evidence" (start the
# server and re-run). 2 is argparse's usage error.
EXIT_MISMATCH = 1
EXIT_INCOMPLETE = 3


class LiveReport(NamedTuple):
    """The three things a run of this check has to say, kept apart on purpose.

    `checked` is what the run is evidence about, `problems` what it found, and `unreached` what it
    is not evidence about.
    """

    problems: list[str]
    checked: list[str]
    unreached: dict[str, list[str]]
    """Connector name -> the template steps it owed an answer for and did not give one."""


def connector_owners() -> dict[str, str]:
    """Every endpoint tool an enabled connector serves, mapped to the connector's name.

    In-process tools are absent; `make template-validate` checks their local signatures. One name
    cannot belong to two connectors (the registry raises at load), so a flat mapping is sound.
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

    Pure, so the decision is testable without a fleet. Per step:

    1. **Not a connector tool** — skipped; the offline gate owns it.
    2. **Its connector did not come up** — recorded in `unreached`, neither a problem nor a pass.
    3. **Its connector came up without it** — a problem: the call would fail.
    4. **It is there** — `argument_problems` applies.

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
            accepts = ToolArguments.of_schema(normalise_tool_schema(live) or {})
            problems.extend(argument_problems(template, step, accepts))
            checked.append(f"{where} ({connector})")
    return LiveReport(problems=problems, checked=checked, unreached=unreached)


def _specs_for(owners: Mapping[str, str], needed: Collection[str]) -> list[ConnectorSpec]:
    """The connection specs for just the connectors some template step actually names.

    Opening the whole enabled set would pay connect timeouts and pad `unreached` with irrelevant
    connectors. `owners` is passed in so this opens exactly the set `check_live_arguments` judges.
    """
    wanted = {owners[tool] for tool in needed if tool in owners}
    return [spec for spec in mcp_connections() if spec.name in wanted]


async def run() -> LiveReport:
    """Open the connectors the shipped templates name and check their steps against them.

    Identity comes from `cli.chat.resolve_identity`: the connector client stamps `X-Chemclaw-Actor`
    on every request, and there is no anonymous session.
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

    Parses arguments though it declares none, so an unsupported argument is refused rather than
    ignored. The knobs are `CHEMCLAW_TEMPLATES_DIR` and `CHEMCLAW_CONNECTOR_URLS`.
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
        # The green line is withheld deliberately: a pass would be a claim about steps this run
        # never looked at.
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
