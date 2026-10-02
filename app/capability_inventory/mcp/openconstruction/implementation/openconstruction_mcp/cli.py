from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence, TextIO

from .acquisition import AcquisitionPlan, resolve_dataset_download_plan
from .catalog import CatalogClient
from .downloads import DownloadManager, TERMINAL_STATUSES
from .papers import PaperCatalogClient


SUCCESS_STATUSES = {"completed", "already_installed"}
ACTIVE_STATUSES = {"queued", "resolving", "downloading", "verifying", "preparing_bundle"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oc",
        description="Install OpenConstruction datasets into a local dataset library.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    download = commands.add_parser("download", help="Download or resume a dataset installation.")
    download.add_argument("dataset_id", help="Exact OpenConstruction dataset id.")
    download.add_argument(
        "--paper",
        action="store_true",
        help="Also download the dataset's related paper when it is available.",
    )
    download.add_argument(
        "--library-dir",
        help="Dataset library directory (default: OC_DATASETS_DIR or ~/.openconstruction/datasets).",
    )
    download.add_argument(
        "--destination",
        help="Safe dataset-directory name inside the selected library (default: dataset id).",
    )
    download.add_argument(
        "--accept-license",
        action="store_true",
        help="Confirm that you reviewed and accept the dataset license and source terms.",
    )
    download.add_argument(
        "--poll-interval",
        type=float,
        default=0.5,
        metavar="SECONDS",
        help=argparse.SUPPRESS,
    )
    return parser


def _print_plan(plan: AcquisitionPlan, include_paper: bool, output: TextIO) -> None:
    print(f"Dataset: {plan.dataset_name} ({plan.dataset_id})", file=output)
    print(f"Provider: {plan.provider or plan.kind}", file=output)
    print(f"License: {plan.license or 'not declared'}", file=output)
    print(f"Related paper: {'requested' if include_paper else 'not requested'}", file=output)


def _print_instructions(result: dict[str, Any], output: TextIO) -> None:
    plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
    instructions = plan.get("instructions") if isinstance(plan.get("instructions"), dict) else {}
    summary = instructions.get("summary") or "This source requires a provider-specific download workflow."
    print(str(summary), file=output)
    command = instructions.get("command")
    if command:
        print("Provider command:", file=output)
        print(str(command), file=output)
    documentation_url = instructions.get("documentation_url")
    if documentation_url:
        print(f"Documentation: {documentation_url}", file=output)


def _show_progress(result: dict[str, Any], output: TextIO, previous: str | None) -> str | None:
    progress = result.get("progress_text")
    if not isinstance(progress, str) or not progress or progress == previous:
        return previous
    print(progress, file=output, flush=True)
    return progress


def _paper_summary(result: dict[str, Any], destination: Path, output: TextIO) -> bool:
    paper = result.get("related_paper")
    if not isinstance(paper, dict):
        print("Paper: no paper metadata was returned.", file=output)
        return False
    status = str(paper.get("download_status") or "unknown")
    if status == "completed" and paper.get("local_path"):
        print(f"Paper: {destination / str(paper['local_path'])}", file=output)
        return True
    availability = paper.get("availability_status")
    if status == "unavailable":
        print(f"Paper: unavailable ({availability or 'not found in the OC paper catalog'}).", file=output)
    elif status == "failed":
        print(f"Paper: download failed: {paper.get('error') or 'unknown error'}", file=output)
    else:
        print(f"Paper: {status}.", file=output)
    return False


def _finish(result: dict[str, Any], include_paper: bool, output: TextIO, error: TextIO) -> int:
    status = str(result.get("status") or "unknown")
    if status in SUCCESS_STATUSES:
        destination = Path(str(result.get("destination") or "."))
        label = "Already installed" if status == "already_installed" else "Installed"
        print(f"{label}: {destination}", file=output)
        print(f"Manifest: {destination / '.openconstruction-manifest.json'}", file=output)
        if include_paper:
            paper_ok = _paper_summary(result, destination, output)
            paper = result.get("related_paper")
            if isinstance(paper, dict) and paper.get("download_status") == "failed" and not paper_ok:
                return 1
        return 0

    if status == "instructions_required":
        _print_instructions(result, error)
        return 2
    if status == "auth_required":
        print(result.get("error") or "Provider authentication is required.", file=error)
        auth = result.get("auth") if isinstance(result.get("auth"), dict) else {}
        instructions = auth.get("instructions")
        if instructions:
            print(str(instructions), file=error)
        return 2
    if status == "source_changed":
        print(result.get("error") or "The requested source differs from the existing installation.", file=error)
        return 1
    if status == "already_running" or result.get("already_running") is True:
        print(result.get("error") or "Another OpenConstruction process is already using this destination.", file=error)
        return 2

    print(result.get("error") or f"Dataset download ended with status: {status}.", file=error)
    return 1


def run(
    argv: Sequence[str] | None = None,
    *,
    catalog: CatalogClient | None = None,
    paper_catalog: PaperCatalogClient | None = None,
    download_manager: DownloadManager | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    args = build_parser().parse_args(argv)
    output = stdout or sys.stdout
    error = stderr or sys.stderr
    catalog = catalog or CatalogClient()
    paper_catalog = paper_catalog or PaperCatalogClient()
    download_manager = download_manager or DownloadManager()

    if args.command != "download":  # pragma: no cover - argparse enforces this.
        return 2
    if args.poll_interval < 0:
        print("--poll-interval must be zero or greater.", file=error)
        return 2

    try:
        plan = resolve_dataset_download_plan(catalog, args.dataset_id)
        paper_plan = paper_catalog.resolve(plan.dataset_id) if args.paper else None
        _print_plan(plan, args.paper, output)
        result = download_manager.start(
            plan,
            args.destination,
            accept_license=args.accept_license,
            include_papers=args.paper,
            paper_plan=paper_plan,
            library_dir=args.library_dir,
        )
    except (OSError, ValueError) as exc:
        print(f"Cannot start download: {exc}", file=error)
        if not args.accept_license and "accept_license" in str(exc):
            print("Review the license above, then rerun with --accept-license.", file=error)
        return 2

    previous_progress = _show_progress(result, error, None)
    status = str(result.get("status") or "")
    if status in SUCCESS_STATUSES or status not in ACTIVE_STATUSES or result.get("already_running") is True:
        return _finish(result, args.paper, output, error)

    download_id = result.get("download_id")
    if not isinstance(download_id, str) or not download_id:
        print("The download started without a valid download id.", file=error)
        return 1

    try:
        while status not in TERMINAL_STATUSES:
            sleep(args.poll_interval)
            result = download_manager.get(download_id)
            previous_progress = _show_progress(result, error, previous_progress)
            status = str(result.get("status") or "")
    except KeyboardInterrupt:
        download_manager.cancel(download_id)
        print("Download cancelled; resumable progress was preserved.", file=error)
        return 130
    except (OSError, ValueError) as exc:
        print(f"Cannot read download status: {exc}", file=error)
        return 1

    return _finish(result, args.paper, output, error)


def main() -> int:
    return run()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
