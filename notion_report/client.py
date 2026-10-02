from __future__ import annotations

import json
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


NOTION_API_VERSION = "2026-03-11"
NOTION_API_BASE = "https://api.notion.com/v1"


class NotionAPIError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


class NotionClient:
    """Small Notion REST client with bounded retries and injectable transport."""

    def __init__(
        self,
        token: str,
        *,
        api_version: str = NOTION_API_VERSION,
        base_url: str = NOTION_API_BASE,
        timeout: float = 30.0,
        max_retries: int = 2,
        opener: Callable[..., Any] = urlopen,
    ):
        if not token or not token.strip():
            raise NotionAPIError("Notion access token is empty")
        self.token = token.strip()
        self.api_version = api_version
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.opener = opener

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            f"{self.base_url}/{path.lstrip('/')}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": self.api_version,
                "Content-Type": "application/json",
                "User-Agent": "AgentUI-NotionReport/1.0",
            },
        )
        for attempt in range(self.max_retries + 1):
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
            except HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                try:
                    detail = json.loads(raw)
                except json.JSONDecodeError:
                    detail = {}
                retryable = exc.code == 429 or exc.code >= 500
                if retryable and attempt < self.max_retries:
                    retry_after = float(exc.headers.get("Retry-After", 0) or 0)
                    time.sleep(max(retry_after, 0.25 * (2**attempt)))
                    continue
                message = detail.get("message") or raw or str(exc)
                raise NotionAPIError(
                    f"Notion API {exc.code}: {message}",
                    status=exc.code,
                    code=str(detail.get("code") or ""),
                ) from exc
            except URLError as exc:
                if attempt < self.max_retries:
                    time.sleep(0.25 * (2**attempt))
                    continue
                raise NotionAPIError(f"Cannot reach Notion API: {exc.reason}") from exc
        raise NotionAPIError("Notion API request failed")

    def get_self(self) -> dict:
        return self.request("GET", "/users/me")

    def retrieve_page(self, page_id: str) -> dict:
        return self.request("GET", f"/pages/{page_id}")

    def create_page(self, parent_page_id: str, title: str, children: list[dict]) -> dict:
        if not parent_page_id:
            raise NotionAPIError("Refusing workspace-root page creation: parent_page_id is required")
        payload = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "properties": {
                "title": {
                    "type": "title",
                    "title": [{"type": "text", "text": {"content": title}}],
                }
            },
        }
        if children:
            payload["children"] = children[:100]
        page = self.request("POST", "/pages", payload)
        if len(children) > 100:
            self.append_children(page["id"], children[100:])
        return page

    def create_page_markdown(
        self,
        parent_page_id: str,
        title: str,
        markdown: str,
    ) -> dict:
        """Create one child page through Notion's enhanced Markdown API."""
        if not parent_page_id:
            raise NotionAPIError(
                "Refusing workspace-root page creation: parent_page_id is required"
            )
        if not isinstance(markdown, str) or not markdown.strip():
            raise NotionAPIError("Refusing to create a report with empty markdown")
        return self.request(
            "POST",
            "/pages",
            {
                "parent": {"type": "page_id", "page_id": parent_page_id},
                "properties": {
                    "title": {
                        "type": "title",
                        "title": [
                            {"type": "text", "text": {"content": title}}
                        ],
                    }
                },
                "markdown": markdown,
            },
        )

    def retrieve_page_markdown(self, page_id: str) -> dict:
        """Read a page through the enhanced Markdown endpoint for verification."""
        return self.request("GET", f"/pages/{page_id}/markdown")

    def append_children(self, block_id: str, children: list[dict]) -> None:
        for start in range(0, len(children), 100):
            self.request(
                "PATCH",
                f"/blocks/{block_id}/children",
                {"children": children[start : start + 100], "position": {"type": "end"}},
            )

    def archive_block(self, block_id: str) -> dict:
        return self.request("PATCH", f"/blocks/{block_id}", {"archived": True})

    def list_block_children(self, block_id: str) -> list[dict]:
        results: list[dict] = []
        cursor = ""
        while True:
            query = {"page_size": 100}
            if cursor:
                query["start_cursor"] = cursor
            page = self.request(
                "GET", f"/blocks/{block_id}/children?{urlencode(query)}")
            results.extend(page.get("results") or [])
            if not page.get("has_more"):
                return results
            cursor = str(page.get("next_cursor") or "")
            if not cursor:
                raise NotionAPIError("Notion returned has_more without next_cursor")
