from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable


BUNDLE_SCHEMA_VERSION = "1.0"
PREPARATION_ID_PATTERN = re.compile(r"rp_[a-f0-9]{16}")


def _clean_line(value: Any, fallback: str = "Unknown") -> str:
    text = " ".join(str(value or "").split())
    return text[:500] or fallback


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _paper_record(paper: dict[str, Any], target: Path) -> dict[str, Any] | None:
    if paper.get("download_status") != "completed":
        return None
    local_path = str(paper.get("local_path") or "")
    candidate = (target / local_path).resolve()
    try:
        candidate.relative_to(target.resolve())
    except ValueError as exc:
        raise ValueError("Related paper path escapes the research package") from exc
    if not candidate.is_file():
        raise FileNotFoundError("Related paper is missing from the completed download")
    return {
        "title": paper.get("paper_title"),
        "authors": paper.get("paper_authors") or [],
        "doi": paper.get("doi"),
        "license": paper.get("paper_license"),
        "license_url": paper.get("paper_license_url"),
        "redistribution_status": paper.get("redistribution_status"),
        "rights_evidence": paper.get("rights_evidence"),
        "rights_verified_at": paper.get("rights_verified_at"),
        "source_url": paper.get("original_source_url") or paper.get("content_url"),
        "path": f"../{local_path}",
        "mime_type": "application/pdf",
        "bytes": candidate.stat().st_size,
        "sha256": paper.get("downloaded_sha256") or paper.get("sha256"),
        "pages": paper.get("pages"),
        "content_extracted": False,
    }


def create_research_bundle(
    *,
    target: Path,
    preparation_id: str,
    dataset_id: str,
    plan: dict[str, Any],
    files: list[dict[str, Any]],
    related_paper: dict[str, Any] | None,
    warnings: list[str],
    source_fingerprint: str,
) -> dict[str, Any]:
    if not PREPARATION_ID_PATTERN.fullmatch(preparation_id):
        raise ValueError("Invalid research preparation id")
    target = target.resolve()
    download_manifest = target / ".openconstruction-manifest.json"
    if not download_manifest.is_file():
        raise FileNotFoundError("The verified OpenConstruction download manifest is missing")

    research_dir = target / "research"
    if not re.fullmatch(r"[a-f0-9]{64}", source_fingerprint):
        raise ValueError("Invalid research source fingerprint")
    research_dir.mkdir(parents=False, exist_ok=True)
    paper = _paper_record(related_paper or {}, target)
    papers = [paper] if paper else []
    bundle_id = preparation_id.replace("rp_", "rb_", 1)
    bundle = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "bundle_id": bundle_id,
        "preparation_id": preparation_id,
        "source_fingerprint": source_fingerprint,
        "status": "ready",
        "created_at": int(time.time()),
        "dataset": {
            "id": dataset_id,
            "name": plan.get("dataset_name"),
            "license": plan.get("license"),
            "provider": plan.get("provider"),
            "method": plan.get("method"),
            "source_url": plan.get("url"),
            "root": "..",
            "download_manifest": "../.openconstruction-manifest.json",
        },
        "papers": papers,
        "downloaded_files": files,
        "document_handling": {
            "original_pdfs_preserved": True,
            "pdf_to_markdown": False,
            "content_extraction": False,
            "search_index_created": False,
        },
        "warnings": warnings,
        "generator": {"name": "openconstruction", "version": "0.1.3"},
    }

    title = _clean_line(plan.get("dataset_name"), dataset_id)
    lines = [
        f"# {title} research package",
        "",
        "This OpenConstruction package preserves the downloaded dataset and original PDF files without document conversion or content extraction.",
        "",
        f"- Dataset ID: `{dataset_id}`",
        f"- Dataset license: {_clean_line(plan.get('license'))}",
        f"- Dataset source: {_clean_line(plan.get('url'))}",
        f"- Related papers included: {len(papers)}",
    ]
    for item in papers:
        lines.extend(
            [
                "",
                f"## {_clean_line(item.get('title'), 'Related paper')}",
                "",
                f"- DOI: {_clean_line(item.get('doi'))}",
                f"- License: {_clean_line(item.get('license'))}",
                f"- Rights status: {_clean_line(item.get('redistribution_status'))}",
                f"- Original PDF: `{item['path']}`",
            ]
        )
    lines.extend(["", "See `bundle.json` for checksums, provenance, and machine-readable metadata.", ""])

    readme_path = research_dir / "README.md"
    bundle_path = research_dir / "bundle.json"
    _atomic_write(readme_path, "\n".join(lines))
    _atomic_write(bundle_path, json.dumps(bundle, ensure_ascii=False, indent=2) + "\n")
    return bundle_summary(bundle, target)


def bundle_summary(bundle: dict[str, Any], target: Path) -> dict[str, Any]:
    papers = bundle.get("papers") if isinstance(bundle.get("papers"), list) else []
    dataset = bundle.get("dataset") if isinstance(bundle.get("dataset"), dict) else {}
    paper_paths: list[str] = []
    for item in papers:
        if not isinstance(item, dict):
            continue
        candidate = (target / "research" / str(item.get("path") or "")).resolve()
        try:
            candidate.relative_to(target.resolve())
        except ValueError:
            continue
        if candidate.is_file():
            paper_paths.append(str(candidate))
    return {
        "bundle_id": bundle.get("bundle_id"),
        "preparation_id": bundle.get("preparation_id"),
        "source_fingerprint": bundle.get("source_fingerprint"),
        "status": bundle.get("status"),
        "bundle_path": str(target / "research"),
        "manifest_path": str(target / "research" / "bundle.json"),
        "dataset_path": str(target),
        "dataset_id": dataset.get("id"),
        "dataset_name": dataset.get("name"),
        "dataset_license": dataset.get("license"),
        "papers": papers,
        "paper_paths": paper_paths,
        "original_pdfs_preserved": True,
        "content_extraction": False,
    }


def _find_research_bundle(root: Path, matches: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
    root = root.resolve()
    if not root.is_dir():
        return None
    for child in root.iterdir():
        if not child.is_dir():
            continue
        resolved_child = child.resolve()
        try:
            resolved_child.relative_to(root)
        except ValueError:
            continue
        bundle_path = resolved_child / "research" / "bundle.json"
        try:
            if not bundle_path.is_file() or bundle_path.stat().st_size > 2 * 1024 * 1024:
                continue
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if bundle.get("status") == "ready" and matches(bundle):
            return bundle_summary(bundle, resolved_child)
    return None


def find_research_bundle(root: Path, preparation_id: str) -> dict[str, Any] | None:
    if not PREPARATION_ID_PATTERN.fullmatch(preparation_id):
        return None
    return _find_research_bundle(root, lambda bundle: bundle.get("preparation_id") == preparation_id)


def find_research_bundle_for_dataset(
    root: Path,
    dataset_id: str,
    source_fingerprint: str | None = None,
) -> dict[str, Any] | None:
    requested = str(dataset_id or "").strip()
    if not requested or len(requested) > 180 or any(character in requested for character in "/\\\0"):
        return None

    def matches(bundle: dict[str, Any]) -> bool:
        dataset = bundle.get("dataset")
        return bool(
            isinstance(dataset, dict)
            and str(dataset.get("id") or "").casefold() == requested.casefold()
            and (source_fingerprint is None or bundle.get("source_fingerprint") == source_fingerprint)
        )

    return _find_research_bundle(root, matches)
