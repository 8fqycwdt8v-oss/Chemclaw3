"""CLI entrypoint for `make audit-verify`: verify the tamper-evident audit hash chain and print it.

The chain-walking implementation (`verify_chain`, `check_chain`, `ChainRow`/`ChainCheck`) is
durable-layer code and lives in `chemclaw.durable.audit_chain` — `AuditChainVerifyWorkflow`
(`chemclaw.durable.audit_verify`) imports it directly for the scheduled check (gap SCH-5), and this
module is only the manual CLI wrapper around the same function: argument parsing, printing the
problems, and the optional `--reseal`. Run as `python -m chemclaw.cli.verify_audit_chain`; it exits
non-zero if the chain is broken, so it can gate a compliance check in CI or an audit.
"""

import argparse
import asyncio
from collections.abc import Sequence

from chemclaw.agent.audit_anchor import parse_anchor, take_anchor
from chemclaw.core.config import settings
from chemclaw.durable.audit_chain import verify_chain


def _parser() -> argparse.ArgumentParser:
    """The CLI surface: verify, optionally against a recovered anchor, optionally re-anchoring."""
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    parser.add_argument(
        "--anchor",
        default="",
        help=(
            "an anchor recovered from the logs (the JSON after `audit_chain_anchor=`, or the whole "
            "log line) to hold the trail against, instead of the one in the database. This is the "
            "form to use after a restore, which rolled the stored anchors back too."
        ),
    )
    parser.add_argument(
        "--reseal",
        default="",
        help=(
            "record a NEW anchor over the trail as it stands now, with this reason. Only after a "
            "verified-clean chain, and only as a deliberate act: it accepts the current trail as "
            "the baseline, so re-sealing over a gap makes that gap permanent and unremarked. The "
            "reason is stored with the anchor — a trail may be shortened by a legitimate recovery "
            "and may never pretend it was not."
        ),
    )
    parser.add_argument(
        "--reseal-by",
        default=settings.cli_admin_actor,
        help="who accepted the re-seal (stored beside the reason).",
    )
    return parser


async def _run(argv: Sequence[str] | None) -> int:
    """Verify, print, optionally re-seal; return the exit code."""
    args = _parser().parse_args(argv)
    supplied = parse_anchor(args.anchor) if args.anchor else None
    problems = await verify_chain(anchor=supplied)
    for problem in problems:
        print(problem)
    if problems:
        print(f"\n{len(problems)} problem(s) — the audit trail hash chain is BROKEN")
        if args.reseal:
            # Refusing here is the point of the flag existing at all. Re-sealing over a break would
            # sign the damage and make it the new baseline, which is worse than never having
            # anchored: the trail would then verify clean forever.
            print("refusing to re-seal a broken chain — resolve the problems above first")
        return 1
    print("OK: the audit trail hash chain is intact")
    if args.reseal:
        anchor = await take_anchor(reseal_reason=args.reseal, reseal_by=args.reseal_by)
        if anchor is None:
            print("no CHEMCLAW_AUDIT_ANCHOR_SECRET configured — nothing to re-seal with")
            return 1
        print(f"re-sealed at {anchor.row_count} rows (id {anchor.max_event_id}): {args.reseal}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: verify the audit chain; print problems; return the exit code."""
    return asyncio.run(_run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
