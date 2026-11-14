"""CLI: python -m server.monitors build|poll|show|teardown|pause|resume."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from server.monitors.fields import FIELDS, FREQUENCIES
from server.monitors.runner import FactSheetError, FactSheetRunner, render_text
from server.monitors.store import DEFAULT_DB, FactSheetStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m server.monitors", description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite file (default out/factsheets.db)")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="search every field and create one monitor per field")
    build.add_argument("--company", required=True)
    build.add_argument("--fields", nargs="*", choices=list(FIELDS), help="default: all")
    build.add_argument("--frequency", choices=list(FREQUENCIES), default="daily")
    build.add_argument("--no-monitors", action="store_true", help="build the sheet without monitors")
    build.add_argument("--webhook-base", default=os.getenv("MONITORS_WEBHOOK_BASE_URL"),
                       help="public https base of the webhook receiver (env MONITORS_WEBHOOK_BASE_URL)")
    for name, text in (("poll", "poll every monitor and check new items"), ("show", "print the sheet"),
                       ("teardown", "delete the monitors; the sheet stays"),
                       ("pause", "pause the monitors"), ("resume", "resume the monitors")):
        command = sub.add_parser(name, help=text)
        command.add_argument("--company", help="default: every sheet")
        if name == "show":
            command.add_argument("--json", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> int:
    store = FactSheetStore(args.db)
    runner = FactSheetRunner(store, frequency=getattr(args, "frequency", "daily"),
                             webhook_base=getattr(args, "webhook_base", None))
    try:
        if args.command == "build":
            sheet = await runner.build(args.company, args.fields, with_monitors=not args.no_monitors)
            sys.stdout.write(render_text(sheet))
        elif args.command == "poll":
            report = await runner.poll_all(args.company)
            sys.stdout.write(f"polled {report.polled}, failed {report.failed}, new items {report.new_items}, "
                             f"stale: {', '.join(report.stale_fields) or 'none'}\n")
        elif args.command == "show":
            companies = [args.company] if args.company else [sheet["company"] for sheet in store.sheets()]
            sheets = [runner.sheet(company) for company in companies]
            if args.json:
                sys.stdout.write(json.dumps(sheets, indent=2) + "\n")
            else:
                sys.stdout.write("\n".join(render_text(sheet) for sheet in sheets) or "no fact sheets\n")
        elif args.command == "teardown":
            report = await runner.teardown(args.company)
            sys.stdout.write(f"deleted {report.deleted} monitor(s), {report.failed} failed\n")
        else:
            done = await runner.set_status(args.company, "paused" if args.command == "pause" else "active")
            sys.stdout.write(f"{args.command}d {done} monitor(s)\n")
    except FactSheetError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    finally:
        await runner.aclose()
        store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and return its exit code."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(_run(_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
