from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse


_UUID_HEX_RE = re.compile(r"(?i)([0-9a-f]{32})")
_UUID_DASHED_RE = re.compile(
    r"(?i)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_AGENT_RE = re.compile(r"^[A-Z][A-Z0-9_-]*$")

# One optional workspace-level binding can act as the parent for every AgentUI
# project.  A project's first Notion use then creates/reuses exactly one direct
# child below this hub and persists a normal project-level destination.  Agents
# never receive workspace-wide authority: after provisioning they remain locked
# to their own project subtree.
SYSTEM_PROJECT_SLUG = "agentui-system"
SYSTEM_AGENT_ID = "ROOT"


class DestinationError(ValueError):
    """Raised when a Notion destination is absent or unsafe."""


def _dashed_uuid(hex_value: str) -> str:
    raw = hex_value.replace("-", "").lower()
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def extract_page_id(value: str) -> str:
    """Extract and normalize a Notion page UUID from a URL or raw UUID.

    Both regular ``notion.so/<title>-<id>`` links and ``app.notion.com/p/<id>``
    links are accepted. A workspace URL without a page id is rejected.
    """
    text = unquote(str(value or "").strip())
    if not text:
        raise DestinationError("Notion parent URL is required")
    parsed = urlparse(text)
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        raise DestinationError("Notion parent must be an HTTPS URL or page UUID")
    if parsed.netloc and not (
        parsed.netloc.lower() == "notion.so"
        or parsed.netloc.lower().endswith(".notion.so")
        or parsed.netloc.lower() == "notion.site"
        or parsed.netloc.lower().endswith(".notion.site")
        or parsed.netloc.lower() == "app.notion.com"
    ):
        raise DestinationError("Notion parent URL has an unsupported host")
    dashed = _UUID_DASHED_RE.search(text)
    if dashed:
        return _dashed_uuid(dashed.group(1))
    compact = _UUID_HEX_RE.search(text)
    if compact:
        return _dashed_uuid(compact.group(1))
    raise DestinationError("Notion parent URL does not contain a page ID")


@dataclass(frozen=True)
class Destination:
    project_slug: str
    agent_id: str
    parent_url: str
    parent_page_id: str
    token_env: str
    workspace_id: str
    workspace_name: str = ""
    parent_title: str = ""

    def __post_init__(self) -> None:
        if not _KEY_RE.fullmatch(self.project_slug):
            raise DestinationError("project_slug must use lowercase letters, digits, _ or -")
        if not _AGENT_RE.fullmatch(self.agent_id):
            raise DestinationError("agent_id must be uppercase letters, digits, _ or -")
        if not _ENV_NAME_RE.fullmatch(self.token_env):
            raise DestinationError("token_env must be a valid uppercase environment variable name")
        normalized_parent = extract_page_id(self.parent_page_id or self.parent_url)
        normalized_workspace = extract_page_id(self.workspace_id)
        object.__setattr__(self, "parent_page_id", normalized_parent)
        object.__setattr__(self, "workspace_id", normalized_workspace)

    @property
    def key(self) -> str:
        return f"{self.project_slug}:{self.agent_id}"

    @classmethod
    def from_dict(cls, value: dict) -> "Destination":
        return cls(**{k: value.get(k, "") for k in cls.__dataclass_fields__})


class DestinationStore:
    """JSON-backed destination registry. Tokens are never stored here."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "destinations": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DestinationError(f"Cannot read destination registry: {exc}") from exc
        if data.get("version") != 1 or not isinstance(data.get("destinations"), dict):
            raise DestinationError("Unsupported destination registry format")
        return data

    def get(self, project_slug: str, agent_id: str) -> Destination:
        raw = self._read()["destinations"].get(f"{project_slug}:{agent_id}")
        if not raw:
            raise DestinationError(
                f"No Notion destination bound for {project_slug}:{agent_id}; bind one before writing"
            )
        return Destination.from_dict(raw)

    def resolve(self, project_slug: str, agent_id: str) -> Destination:
        """Resolve an exact binding or inherit the project's sole binding.

        A project has one Notion reporting parent. The root agent owns and may
        rebind it; workers inherit it without duplicating destination records.
        Multiple project bindings are rejected for workers because silently
        choosing one would make report routing ambiguous.
        """
        data = self._read()["destinations"]
        exact = data.get(f"{project_slug}:{agent_id}")
        if exact:
            return Destination.from_dict(exact)
        matches = [
            Destination.from_dict(raw)
            for raw in data.values()
            if raw.get("project_slug") == project_slug
        ]
        if not matches:
            raise DestinationError(
                f"No Notion destination bound for project {project_slug}; "
                "bind the project root agent once before writing"
            )
        if len(matches) > 1:
            owners = ", ".join(sorted(destination.agent_id for destination in matches))
            raise DestinationError(
                f"Ambiguous Notion destinations for project {project_slug}: {owners}; "
                "keep one project-level binding or bind this agent explicitly"
            )
        return matches[0]

    def list(self) -> list[Destination]:
        return [Destination.from_dict(v) for v in self._read()["destinations"].values()]

    def upsert(self, destination: Destination) -> None:
        data = self._read()
        data["destinations"][destination.key] = asdict(destination)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
