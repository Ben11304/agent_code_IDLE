from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable

from .catalog import DEFAULT_CACHE_TTL_SECONDS, DEFAULT_DATA_BASE_URL, clean_text, join_url


DEFAULT_PAPER_MANIFEST_URL = f"{DEFAULT_DATA_BASE_URL}/papers/manifest.json"
# Paper PDFs in OC-clone are tracked by Git LFS. raw.githubusercontent.com
# returns the small LFS pointer, while media.githubusercontent.com resolves it
# to the actual object stored by GitHub.
DEFAULT_PAPER_CONTENT_BASE_URL = (
    "https://media.githubusercontent.com/media/Ben11304/OC-clone/main/open-construction-data"
)
DEFAULT_INCLUDE_PAPERS = os.environ.get("OC_INCLUDE_PAPERS_DEFAULT", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}


def _http_url(value: Any) -> str | None:
    text = clean_text(value, 1000)
    if not text:
        return None
    try:
        parsed = urllib.parse.urlparse(text)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    return text


def _sha256(value: Any) -> str | None:
    text = clean_text(value, 80).lower()
    return text if re.fullmatch(r"[a-f0-9]{64}", text) else None


def _safe_manifest_path(value: Any, dataset_id: str) -> str | None:
    text = clean_text(value, 500)
    if not text:
        return None
    path = PurePosixPath(text)
    parts = path.parts
    if (
        path.is_absolute()
        or len(parts) != 3
        or parts[0] != "papers"
        or parts[1].casefold() != dataset_id.casefold()
        or parts[2].casefold() != "paper.pdf"
        or any(part in {"", ".", ".."} for part in parts)
    ):
        return None
    return "/".join(parts)


@dataclass
class PaperCatalogClient:
    manifest_url: str = os.environ.get("OC_PAPER_MANIFEST_URL", DEFAULT_PAPER_MANIFEST_URL)
    content_base_url: str = os.environ.get("OC_PAPER_CONTENT_BASE_URL", DEFAULT_PAPER_CONTENT_BASE_URL)
    cache_ttl_seconds: int = int(os.environ.get("PAPER_CATALOG_CACHE_TTL_SECONDS", DEFAULT_CACHE_TTL_SECONDS))
    fetch_json: Callable[[str], Any] | None = None

    def __post_init__(self) -> None:
        self._loaded_at = 0.0
        self._papers: dict[str, dict[str, Any]] = {}
        self._error: str | None = None

    def load(self, force: bool = False) -> dict[str, dict[str, Any]]:
        now = time.time()
        if self._loaded_at and not force and now - self._loaded_at < self.cache_ttl_seconds:
            return self._papers
        try:
            raw = self.fetch_json(self.manifest_url) if self.fetch_json else self._fetch()
            papers = raw.get("papers") if isinstance(raw, dict) else None
            if not isinstance(papers, dict):
                raise ValueError("OC paper manifest must contain a papers object")
            self._papers = {str(key): value for key, value in papers.items() if isinstance(value, dict)}
            self._error = None
        except Exception as exc:
            self._papers = {}
            self._error = str(exc)
        self._loaded_at = now
        return self._papers

    def resolve(self, dataset_id: str) -> dict[str, Any]:
        requested = clean_text(dataset_id, 180)
        papers = self.load()
        key = next((item for item in papers if item.casefold() == requested.casefold()), None)
        if key is None:
            return {
                "dataset_id": requested,
                "available": False,
                "availability_status": "manifest_unavailable" if self._error else "not_found",
                "manifest_url": self.manifest_url,
                "error": self._error,
                "warning": "The related paper will not block the dataset download.",
            }

        entry = papers[key]
        path = _safe_manifest_path(entry.get("path"), key)
        available = entry.get("status") == "available_local" and path is not None
        rights_status = clean_text(entry.get("redistribution_status"), 80) or "unreviewed"
        encoded_path = "/".join(urllib.parse.quote(part, safe="") for part in path.split("/")) if path else None
        content_url = join_url(self.content_base_url, encoded_path) if encoded_path else None
        size = entry.get("bytes")
        raw_authors = entry.get("authors")
        paper_authors = (
            [author for item in raw_authors[:50] if (author := clean_text(item, 200))]
            if isinstance(raw_authors, list)
            else []
        )
        return {
            "dataset_id": key,
            "available": available,
            "availability_status": "available" if available else clean_text(entry.get("status"), 80) or "unavailable",
            "access_mode": "oc_mirror" if available else "metadata_only",
            "paper_title": clean_text(entry.get("paper_title"), 500) or None,
            "paper_authors": paper_authors,
            "doi": clean_text(entry.get("doi"), 300) or None,
            "content_url": content_url if available else None,
            "original_source_url": _http_url(entry.get("source_url")),
            "filename": "paper.pdf" if available else None,
            "content_size": size if isinstance(size, int) and size >= 0 else None,
            "sha256": _sha256(entry.get("sha256")),
            "pages": entry.get("pages") if isinstance(entry.get("pages"), int) else None,
            "redistribution_status": rights_status,
            "paper_license": clean_text(entry.get("license"), 160) or None,
            "paper_license_url": _http_url(entry.get("license_url")),
            "rights_evidence": clean_text(entry.get("rights_evidence"), 1000) or None,
            "rights_verified_at": clean_text(entry.get("rights_verified_at"), 40) or None,
            "manifest_url": self.manifest_url,
            "warning": (
                "Paper redistribution rights are still unreviewed; this notice does not change the requested include_papers setting."
                if rights_status == "unreviewed"
                else None
            ),
        }

    def _fetch(self) -> Any:
        url = _http_url(self.manifest_url)
        if not url:
            raise ValueError("OC_PAPER_MANIFEST_URL must be an HTTP(S) URL")
        request = urllib.request.Request(url, headers={"User-Agent": "OpenConstruction-MCP/0.1"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
