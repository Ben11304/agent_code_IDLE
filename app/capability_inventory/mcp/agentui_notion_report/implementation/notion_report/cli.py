from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .config import DestinationError, DestinationStore
from .tool import NotionReportTool, ReportSpec


DEFAULT_CONFIG = Path(__file__).with_name("destinations.json")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="notion-report",
        description="Bind and use workspace-locked Notion report destinations.",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("NOTION_REPORT_CONFIG", str(DEFAULT_CONFIG)),
        help="destination registry path (default: notion_report/destinations.json)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    bind = sub.add_parser("bind", help="verify and bind a parent page")
    bind.add_argument("--project", required=True)
    bind.add_argument("--agent", default="BOSS")
    bind.add_argument("--parent-url", required=True)
    bind.add_argument("--token-env", required=True)

    status = sub.add_parser("status", help="show or live-verify a destination")
    status.add_argument("--project", required=True)
    status.add_argument("--agent", default="BOSS")
    status.add_argument("--verify", action="store_true")

    create = sub.add_parser("create", help="create a report from a JSON specification")
    create.add_argument("--project", required=True)
    create.add_argument("--agent", default="BOSS")
    create.add_argument("--report", required=True, help="path to report JSON")
    create.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    tool = NotionReportTool(DestinationStore(args.config))
    try:
        if args.command == "bind":
            result = tool.bind_destination(args.project, args.agent, args.parent_url, args.token_env)
        elif args.command == "status":
            result = tool.status(args.project, args.agent, verify=args.verify)
        else:
            report_path = Path(args.report)
            report = ReportSpec.from_dict(json.loads(report_path.read_text(encoding="utf-8")))
            result = tool.create_report(args.project, args.agent, report, dry_run=args.dry_run)
    except (DestinationError, ValueError, OSError, json.JSONDecodeError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
