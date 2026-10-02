from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


DEFAULT_DATA_BASE_URL = "https://raw.githubusercontent.com/Ben11304/OC-clone/main/open-construction-data"
DEFAULT_SITE_BASE_URL = "https://www.openconstruction.org"
DEFAULT_CACHE_TTL_SECONDS = 600

CATALOG_SOURCES: tuple[tuple[str, str], ...] = (
    ("dataset", "datasets.json"),
    ("model", "models.json"),
    ("workflow", "use-cases.json"),
    ("oer", "oer.json"),
    ("tool", "tools.json"),
    ("guide", "guides.json"),
    ("contributor", "contributors.json"),
    ("benchmark", "benchmark-results.json"),
    ("vocabulary", "task-vocabulary.json"),
)


def clean_text(value: Any, max_length: int = 500) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        value = " ".join(clean_text(item, max_length) for item in value)
    elif isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return " ".join(str(value).split())[:max_length]


def to_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [clean_text(item, 120) for item in value if clean_text(item, 120)]
    text = clean_text(value, 120)
    return [text] if text else []


def join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def resource_href(resource_type: str, resource_id: str) -> str:
    encoded = urllib.parse.quote(resource_id)
    if resource_type == "dataset":
        return f"{DEFAULT_SITE_BASE_URL}/datasets/detail.html?id={encoded}"
    if resource_type == "model":
        return f"{DEFAULT_SITE_BASE_URL}/models/details.html?id={encoded}"
    if resource_type == "workflow":
        return f"{DEFAULT_SITE_BASE_URL}/workflows/details.html?id={encoded}"
    if resource_type == "oer":
        return f"{DEFAULT_SITE_BASE_URL}/oers/details.html?id={encoded}"
    if resource_type == "guide":
        return f"{DEFAULT_SITE_BASE_URL}/guides.html#{encoded}"
    if resource_type == "contributor":
        return f"{DEFAULT_SITE_BASE_URL}/contributors.html#{encoded}"
    if resource_type == "benchmark":
        return f"{DEFAULT_SITE_BASE_URL}/benchmark_results.html#{encoded}"
    if resource_type == "vocabulary":
        return f"{DEFAULT_SITE_BASE_URL}/taxonomy.html#{encoded}"
    return f"{DEFAULT_SITE_BASE_URL}/tools.html"


def looks_like_record(row: dict[str, Any]) -> bool:
    return any(row.get(key) for key in ("id", "name", "title", "summary", "abstract", "url", "source", "link_url"))


def extract_rows(raw: Any, parent: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    parent = parent or {}
    if isinstance(raw, list):
        rows: list[dict[str, Any]] = []
        for item in raw:
            rows.extend(extract_rows(item, parent))
        return rows

    if not isinstance(raw, dict):
        return []

    rows: list[dict[str, Any]] = []
    if looks_like_record(raw):
        rows.append({**parent, **raw})

    child_parent = {
        "section": raw.get("title") or raw.get("name") or parent.get("section"),
        "kicker": raw.get("kicker") or parent.get("kicker"),
    }
    child_keys = ("sections", "items", "resources", "guides", "benchmarks", "entries", "use_cases", "useCases")
    for key in child_keys:
        rows.extend(extract_rows(raw.get(key), child_parent))

    skip_keys = {"schema", "schema_version", "about", "sections", *child_keys}
    for key, value in raw.items():
        if key in skip_keys or not isinstance(value, dict):
            continue
        rows.extend(extract_rows({"id": key, **value}, parent))

    return rows


def normalize_resource(row: dict[str, Any], resource_type: str, site_base_url: str) -> dict[str, Any] | None:
    resource_id = clean_text(row.get("id") or row.get("resource_id") or row.get("slug") or row.get("name") or row.get("title"), 160)
    title = clean_text(row.get("title") or row.get("name") or resource_id, 220)
    if not resource_id or not title:
        return None

    raw_href = clean_text(
        row.get("href") or row.get("url") or row.get("page_url") or row.get("source") or row.get("link_url") or row.get("data_url") or row.get("code_url"),
        300,
    )
    href = raw_href or resource_href(resource_type, resource_id)
    href = href.replace(DEFAULT_SITE_BASE_URL, site_base_url)

    tags = [
        *to_list(row.get("tags")),
        *to_list(row.get("tasks")),
        *to_list(row.get("modalities")),
        *to_list(row.get("data_modalities")),
        *to_list(row.get("topics")),
        *to_list(row.get("classes")),
        *to_list(row.get("objects")),
    ][:18]

    evidence = clean_text(
        " ".join(
            [
                clean_text(row.get("summary"), 400),
                clean_text(row.get("description"), 400),
                clean_text(row.get("abstract"), 500),
                clean_text(row.get("subtitle"), 300),
                clean_text(row.get("role"), 120),
                clean_text(row.get("affiliation"), 180),
                clean_text(row.get("provider"), 120),
                clean_text(row.get("publisher"), 120),
                " ".join(tags),
            ]
        ),
        1200,
    )

    return {
        "id": resource_id,
        "type": resource_type,
        "title": title,
        "summary": clean_text(row.get("summary") or row.get("description") or row.get("abstract") or row.get("subtitle") or row.get("role"), 600),
        "href": href,
        "url": clean_text(row.get("url") or row.get("source") or row.get("data_url") or row.get("code_url"), 300) or None,
        "year": row.get("year"),
        "license": clean_text(row.get("license"), 120) or None,
        "tags": tags,
        "source": row,
        "evidence": evidence,
    }


def score_resource(resource: dict[str, Any], query: str) -> int:
    terms = [term for term in clean_text(query, 1200).lower().replace("_", " ").split() if len(term) > 1]
    if not terms:
        return 0

    title = clean_text(resource.get("title"), 500).lower()
    summary = clean_text(resource.get("summary"), 800).lower()
    evidence = clean_text(resource.get("evidence") or resource.get("source"), 1800).lower()

    score = 0
    for term in terms:
        if term in title:
            score += 8
        if term in summary:
            score += 4
        if term in evidence:
            score += 1
    return score


@dataclass
class CatalogClient:
    data_base_url: str = os.environ.get("OPENCONSTRUCTION_DATA_BASE_URL", DEFAULT_DATA_BASE_URL)
    site_base_url: str = os.environ.get("OPENCONSTRUCTION_SITE_BASE_URL", DEFAULT_SITE_BASE_URL)
    cache_ttl_seconds: int = int(os.environ.get("CATALOG_CACHE_TTL_SECONDS", DEFAULT_CACHE_TTL_SECONDS))
    fetch_json: Callable[[str], Any] | None = None

    def __post_init__(self) -> None:
        self._loaded_at = 0.0
        self._resources: list[dict[str, Any]] = []
        self._errors: list[dict[str, str]] = []

    def load(self, force: bool = False) -> list[dict[str, Any]]:
        now = time.time()
        if self._resources and not force and now - self._loaded_at < self.cache_ttl_seconds:
            return self._resources

        resources: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for resource_type, path in CATALOG_SOURCES:
            try:
                raw = self._fetch(path)
            except Exception as exc:
                errors.append({"type": resource_type, "path": path, "error": str(exc)})
                continue
            for row in extract_rows(raw):
                normalized = normalize_resource(row, resource_type, self.site_base_url)
                if normalized:
                    resources.append(normalized)

        self._loaded_at = now
        self._resources = resources
        self._errors = errors
        return resources

    def search(self, query: str, types: list[str] | None = None, limit: int = 20) -> list[dict[str, Any]]:
        requested = set(types or [])
        scored = []
        for resource in self.load():
            if requested and resource["type"] not in requested:
                continue
            score = score_resource(resource, query)
            if score > 0:
                scored.append({**resource, "score": score})
        scored.sort(key=lambda item: (-item["score"], item["title"]))
        return scored[: max(1, min(limit, 100))]

    def get(self, resource_type: str, resource_id: str) -> dict[str, Any] | None:
        key = clean_text(resource_id, 180).lower()
        for resource in self.load():
            if resource["type"] != resource_type:
                continue
            if key in {resource["id"].lower(), resource["title"].lower()} or key in clean_text(resource.get("href"), 300).lower():
                return resource
        return None

    def compare(self, resources: list[dict[str, str]], goal: str = "") -> dict[str, Any]:
        found = [self.get(item.get("type", ""), item.get("id", "")) for item in resources]
        matches = [item for item in found if item]
        summary = (
            f"Compared {len(matches)} matching OpenConstruction resources for: {goal}."
            if len(matches) >= 2 and goal
            else f"Compared {len(matches)} matching OpenConstruction resources."
            if len(matches) >= 2
            else "At least two matching resources are needed for comparison."
        )
        return {"summary": summary, "resources": matches}

    def stats(self) -> dict[str, Any]:
        by_type: dict[str, int] = {}
        for resource in self.load():
            by_type[resource["type"]] = by_type.get(resource["type"], 0) + 1
        return {
            "source": self.data_base_url,
            "loaded_at": int(self._loaded_at),
            "resources": len(self._resources),
            "by_type": by_type,
            "load_errors": self._errors,
        }

    def answer(self, query: str) -> dict[str, Any]:
        resources = self.search(query, limit=8)
        answer = (
            "Top OpenConstruction matches: "
            + ", ".join(item["title"] for item in resources[:3])
            if resources
            else "No strong OpenConstruction catalog matches found. Try a broader task, modality, resource type, or object class."
        )
        return {
            "answer": answer,
            "resources": resources,
            "followups": [
                "Compare the top matches",
                "Filter by license or modality",
                "Show datasets only",
            ],
        }

    def _fetch(self, path: str) -> Any:
        if self.fetch_json:
            return self.fetch_json(path)
        url = join_url(self.data_base_url, path)
        with urllib.request.urlopen(url, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
