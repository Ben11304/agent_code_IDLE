from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX cross-process flock is unavailable on Windows.
    fcntl = None

from .acquisition import AcquisitionPlan
from .download_providers import DownloadContext, ProviderRegistry, get_default_provider_registry
from .provider_auth import auth_required_response, provider_auth_status
from .research import create_research_bundle
from .storage import DatasetStore


DEFAULT_MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024
USER_AGENT = "OpenConstruction-MCP/0.1"
PROGRESS_BAR_WIDTH = 20
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "auth_required"}
STATE_SCHEMA_VERSION = "1.0"
STATE_PERSIST_INTERVAL_SECONDS = 1.0
JOB_ID_PATTERN = re.compile(r"(?:dl|rp)_[a-f0-9]{16}")


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{int(amount)} B" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"


def validate_public_download_url(url: str) -> None:
    try:
        parsed = urllib.parse.urlparse(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Download URL is malformed") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only HTTP(S) download URLs are allowed")
    if parsed.username or parsed.password:
        raise ValueError("Credentials are not allowed in download URLs")
    if port not in {None, 80, 443}:
        raise ValueError("Non-standard download URL ports are not allowed")
    hostname = parsed.hostname.casefold()
    if hostname == "localhost" or hostname.endswith(".local"):
        raise ValueError("Local network download URLs are not allowed")
    try:
        addresses = socket.getaddrinfo(hostname, port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"Download hostname cannot be resolved: {hostname}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise ValueError("Download URL resolves to a non-public network address")


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        validate_public_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass
class DownloadJob:
    id: str
    dataset_id: str
    destination: str
    plan: dict[str, Any]
    library_dir: str | None = None
    status: str = "queued"
    bytes_downloaded: int = 0
    bytes_total: int | None = None
    files: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    auth: dict[str, Any] | None = None
    retry: dict[str, Any] | None = None
    include_papers: bool = True
    related_paper: dict[str, Any] | None = None
    workflow: str = "dataset_download"
    create_bundle: bool = False
    research_bundle: dict[str, Any] | None = None
    source_fingerprint: str | None = None
    execution_fingerprint: str | None = None
    resumed: bool = False
    resume_supported: bool = True
    current_file: dict[str, Any] | None = None
    license_accepted_at: int | None = None
    warnings: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    process: subprocess.Popen[Any] | None = field(default=None, repr=False)
    lock_handle: Any = field(default=None, repr=False)
    last_persist_at: float = field(default=0.0, repr=False)

    def progress_dict(self) -> dict[str, Any]:
        finished = self.status in TERMINAL_STATUSES
        total = self.bytes_total if isinstance(self.bytes_total, int) and self.bytes_total >= 0 else None
        if self.status == "completed":
            percent: float | None = 100.0
        elif total is not None and total > 0:
            percent = round(min(100.0, self.bytes_downloaded * 100 / total), 1)
        else:
            percent = None

        if percent is None:
            bar = "[" + "?" * PROGRESS_BAR_WIDTH + "]"
        else:
            filled = min(PROGRESS_BAR_WIDTH, max(0, round(percent * PROGRESS_BAR_WIDTH / 100)))
            bar = "[" + "█" * filled + "░" * (PROGRESS_BAR_WIDTH - filled) + "]"

        end_time = self.completed_at if finished and self.completed_at else time.time()
        elapsed = max(0.0, end_time - self.started_at) if self.started_at else 0.0
        speed = int(self.bytes_downloaded / elapsed) if self.bytes_downloaded > 0 and elapsed > 0 else None
        if self.status == "completed":
            eta = 0
        elif total is not None and speed:
            eta = max(0, math.ceil((total - self.bytes_downloaded) / speed))
        else:
            eta = None

        label = self.status.replace("_", " ").capitalize()
        downloaded = _format_bytes(self.bytes_downloaded)
        if percent is None:
            progress_text = f"{label} {bar} {downloaded} received — total size unknown"
        else:
            total_text = _format_bytes(total if total is not None else self.bytes_downloaded)
            progress_text = f"{label} {bar} {percent:.1f}% — {downloaded} / {total_text}"

        return {
            "progress_percent": percent,
            "progress_bar": bar,
            "progress_indeterminate": percent is None,
            "speed_bytes_per_second": speed,
            "eta_seconds": eta,
            "progress_text": progress_text,
        }

    def public_dict(self) -> dict[str, Any]:
        return {
            "download_id": self.id,
            "dataset_id": self.dataset_id,
            "dataset_name": self.plan.get("dataset_name"),
            "dataset_license": self.plan.get("license"),
            "dataset_source": self.plan.get("url"),
            "destination": self.destination,
            "library_dir": self.library_dir,
            "status": self.status,
            "bytes_downloaded": self.bytes_downloaded,
            "bytes_total": self.bytes_total,
            "files": list(self.files),
            "error": self.error,
            "auth": self.auth,
            "retry": self.retry,
            "include_papers": self.include_papers,
            "related_paper": dict(self.related_paper) if self.related_paper else None,
            "workflow": self.workflow,
            "research_bundle": dict(self.research_bundle) if self.research_bundle else None,
            "source_fingerprint": self.source_fingerprint,
            "execution_fingerprint": self.execution_fingerprint,
            "resumed": self.resumed,
            "resume_supported": self.resume_supported,
            "resume_available": self.status in {"failed", "cancelled", "auth_required", "interrupted"}
            or self.current_file is not None,
            "license_accepted_at": self.license_accepted_at,
            "warnings": list(self.warnings),
            "created_at": int(self.created_at),
            "started_at": int(self.started_at) if self.started_at else None,
            "completed_at": int(self.completed_at) if self.completed_at else None,
            **self.progress_dict(),
        }


class DownloadManager:
    def __init__(
        self,
        root: str | Path | None = None,
        max_download_bytes: int | None = None,
        url_validator: Callable[[str], None] = validate_public_download_url,
        provider_registry: ProviderRegistry | None = None,
    ) -> None:
        self.store = DatasetStore(root)
        self.root = self.store.root
        configured_max = os.environ.get("OC_MAX_DOWNLOAD_BYTES")
        self.max_download_bytes = max_download_bytes or int(configured_max or DEFAULT_MAX_DOWNLOAD_BYTES)
        self.url_validator = url_validator
        self.provider_registry = provider_registry or get_default_provider_registry()
        self._jobs: dict[str, DownloadJob] = {}
        self._lock = threading.Lock()

    @property
    def state_root(self) -> Path:
        return self.root / ".openconstruction-state"

    def source_fingerprint(
        self,
        plan: AcquisitionPlan,
        *,
        include_papers: bool,
        paper_plan: dict[str, Any] | None,
    ) -> str:
        if not plan.source_identity:
            return self.execution_fingerprint(
                plan,
                include_papers=include_papers,
                paper_plan=paper_plan,
            )
        payload = {
            "dataset_id": plan.dataset_id,
            "source_identity": plan.source_identity,
            "license": plan.license,
            "include_papers": include_papers,
            "paper": self._paper_identity(paper_plan) if include_papers else None,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def execution_fingerprint(
        self,
        plan: AcquisitionPlan,
        *,
        include_papers: bool,
        paper_plan: dict[str, Any] | None,
    ) -> str:
        payload = {
            "dataset_id": plan.dataset_id,
            "kind": plan.kind,
            "provider": plan.provider,
            "method": plan.method,
            "url": plan.url,
            "filename": plan.filename,
            "license": plan.license,
            "estimated_size": plan.estimated_size,
            "distributions": plan.distributions,
            "programmatic_access": plan.programmatic_access,
            "include_papers": include_papers,
            "paper": self._paper_identity(paper_plan) if include_papers else None,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _paper_identity(paper: dict[str, Any] | None) -> dict[str, Any] | None:
        if not paper:
            return None
        return {
            "available": paper.get("available"),
            "content_url": paper.get("content_url"),
            "content_size": paper.get("content_size"),
            "sha256": paper.get("sha256"),
            "doi": paper.get("doi"),
        }

    def _state_path(self, target: Path) -> Path:
        return self.state_root / "jobs" / f"{self._target_state_key(target)}.json"

    def _lock_path(self, target: Path) -> Path:
        return self.state_root / "locks" / f"{self._target_state_key(target)}.lock"

    def _target_state_key(self, target: Path) -> str:
        if target.parent == self.root:
            return target.name
        digest = hashlib.sha256(str(target).encode("utf-8")).hexdigest()[:12]
        return f"{target.name}-{digest}"

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _persist_job(self, job: DownloadJob, *, force: bool = False) -> None:
        now = time.time()
        if not force and now - job.last_persist_at < STATE_PERSIST_INTERVAL_SECONDS:
            return
        state = {
            "schema_version": STATE_SCHEMA_VERSION,
            "source_fingerprint": job.source_fingerprint,
            "execution_fingerprint": job.execution_fingerprint,
            "plan": job.plan,
            "license_accepted": True,
            "license_accepted_at": job.license_accepted_at,
            "updated_at": int(now),
            "current_file": job.current_file,
            "job": job.public_dict(),
        }
        self._atomic_json(self._state_path(Path(job.destination)), state)
        job.last_persist_at = now

    def _load_target_state(self, target: Path) -> dict[str, Any] | None:
        state = self._read_json(self._state_path(target))
        if not state or state.get("schema_version") != STATE_SCHEMA_VERSION:
            return None
        job = state.get("job")
        if not isinstance(job, dict) or str(job.get("destination") or "") != str(target):
            return None
        job_id = str(job.get("download_id") or "")
        return state if JOB_ID_PATTERN.fullmatch(job_id) else None

    def _find_state_by_id(self, download_id: str) -> dict[str, Any] | None:
        if not JOB_ID_PATTERN.fullmatch(str(download_id or "")):
            return None
        jobs_dir = self.state_root / "jobs"
        if not jobs_dir.is_dir():
            return None
        for path in jobs_dir.glob("*.json"):
            state = self._read_json(path)
            job = state.get("job") if state else None
            if isinstance(job, dict) and job.get("download_id") == download_id:
                return state
        return None

    @staticmethod
    def _public_from_state(state: dict[str, Any], *, interrupted: bool = False) -> dict[str, Any]:
        stored = state.get("job")
        if not isinstance(stored, dict):
            raise ValueError("Persistent download state is invalid")
        allowed = {
            "download_id",
            "dataset_id",
            "dataset_name",
            "dataset_license",
            "dataset_source",
            "destination",
            "library_dir",
            "status",
            "bytes_downloaded",
            "bytes_total",
            "files",
            "error",
            "auth",
            "retry",
            "include_papers",
            "related_paper",
            "workflow",
            "research_bundle",
            "source_fingerprint",
            "execution_fingerprint",
            "resumed",
            "resume_supported",
            "resume_available",
            "license_accepted_at",
            "warnings",
            "created_at",
            "started_at",
            "completed_at",
            "progress_percent",
            "progress_bar",
            "progress_indeterminate",
            "speed_bytes_per_second",
            "eta_seconds",
            "progress_text",
        }
        result = {key: value for key, value in stored.items() if key in allowed}
        if interrupted and result.get("status") not in TERMINAL_STATUSES:
            result["status"] = "interrupted"
            result["resume_available"] = True
            result["speed_bytes_per_second"] = None
            result["eta_seconds"] = None
            result["progress_text"] = "Paused — saved progress is available and will resume on the next preparation request."
            result["retry"] = {
                "tool": "prepare_dataset_for_research" if result.get("workflow") == "research_preparation" else "download_dataset",
                "arguments": {
                    "dataset_id": result.get("dataset_id"),
                    "accept_license": True,
                },
            }
            if result.get("workflow") != "research_preparation":
                result["retry"]["arguments"]["destination"] = Path(str(result.get("destination"))).name
                if result.get("library_dir"):
                    result["retry"]["arguments"]["library_dir"] = result.get("library_dir")
                result["retry"]["arguments"]["include_papers"] = result.get("include_papers") is not False
        return result

    def has_accepted_resume(
        self,
        plan: AcquisitionPlan,
        destination: str | None,
        *,
        include_papers: bool,
        paper_plan: dict[str, Any] | None,
        library_dir: str | None = None,
    ) -> bool:
        target = self.resolve_destination(plan, destination, library_dir=library_dir)
        state = self._load_target_state(target)
        job = state.get("job") if state else None
        source_matches, execution_matches = self._checkpoint_compatibility(
            state,
            plan,
            include_papers=include_papers,
            paper_plan=paper_plan,
        )
        return bool(
            state
            and source_matches
            and (execution_matches or self._empty_failed_checkpoint(target, state))
            and state.get("license_accepted") is True
            and isinstance(job, dict)
            and job.get("dataset_license") == plan.license
            and (job.get("status") != "completed" or not (target / "research" / "bundle.json").is_file())
        )

    def _acquire_job_lock(self, target: Path) -> Any:
        lock_path = self._lock_path(target)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+", encoding="utf-8")
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                return None
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "acquired_at": int(time.time())}))
        handle.flush()
        return handle

    @staticmethod
    def _release_job_lock(job: DownloadJob) -> None:
        handle = job.lock_handle
        if handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            job.lock_handle = None

    def resolve_destination(
        self,
        plan: AcquisitionPlan,
        destination: str | None,
        *,
        library_dir: str | None = None,
    ) -> Path:
        return self.store.resolve_target(plan.dataset_id, destination, library_dir=library_dir)

    def inspect_installation(
        self,
        plan: AcquisitionPlan,
        destination: str | None = None,
        *,
        library_dir: str | None = None,
        include_papers: bool = True,
        paper_plan: dict[str, Any] | None = None,
        verify_checksums: bool = False,
    ) -> dict[str, Any]:
        target = self.resolve_destination(plan, destination, library_dir=library_dir)
        fingerprint = self.source_fingerprint(
            plan,
            include_papers=include_papers,
            paper_plan=paper_plan,
        )
        result = self.store.inspect(
            plan.dataset_id,
            target,
            source_fingerprint=fingerprint,
            verify_checksums=verify_checksums,
        ).to_dict()
        state = self._load_target_state(target)
        stored_job = state.get("job") if state else None
        source_matches, execution_matches = self._checkpoint_compatibility(
            state,
            plan,
            include_papers=include_papers,
            paper_plan=paper_plan,
        )
        resumable = bool(
            state
            and source_matches
            and execution_matches
            and state.get("license_accepted") is True
            and isinstance(stored_job, dict)
            and stored_job.get("status") != "completed"
        )
        if resumable:
            result["status"] = "partial"
            result["resume_available"] = True
            result["download_id"] = stored_job.get("download_id")
            result["bytes_present"] = int(stored_job.get("bytes_downloaded") or result["bytes_present"])
            result["message"] = "A matching interrupted download is available and can be resumed."
        elif source_matches and not execution_matches and self._empty_failed_checkpoint(target, state):
            result["status"] = "partial"
            result["resume_available"] = True
            result["download_id"] = stored_job.get("download_id") if isinstance(stored_job, dict) else None
            result["message"] = (
                "An empty failed checkpoint from an older download strategy will be reset automatically."
            )
        result["requested_source_fingerprint"] = fingerprint
        return result

    @staticmethod
    def _source_changed_response(
        plan: AcquisitionPlan,
        target: Path,
        existing_fingerprint: str | None,
        requested_fingerprint: str,
    ) -> dict[str, Any]:
        return {
            "status": "source_changed",
            "dataset_id": plan.dataset_id,
            "dataset_name": plan.dataset_name,
            "destination": str(target),
            "resume_supported": False,
            "resume_available": False,
            "existing_source_fingerprint": existing_fingerprint,
            "requested_source_fingerprint": requested_fingerprint,
            "error": (
                "The existing files belong to a different source/version. OpenConstruction will not overwrite them; "
                "use a new destination or explicitly repair the existing package."
            ),
        }

    def _checkpoint_compatibility(
        self,
        state: dict[str, Any] | None,
        plan: AcquisitionPlan,
        *,
        include_papers: bool,
        paper_plan: dict[str, Any] | None,
    ) -> tuple[bool, bool]:
        if not state:
            return False, False
        requested_source = self.source_fingerprint(
            plan,
            include_papers=include_papers,
            paper_plan=paper_plan,
        )
        requested_execution = self.execution_fingerprint(
            plan,
            include_papers=include_papers,
            paper_plan=paper_plan,
        )
        stored_source = str(state.get("source_fingerprint") or "")
        stored_execution = str(state.get("execution_fingerprint") or "")
        source_matches = stored_source in {requested_source, requested_execution}
        if not source_matches:
            source_matches = self._stored_identity_matches(
                state,
                plan,
                include_papers=include_papers,
                paper_plan=paper_plan,
            )
        execution_matches = (
            stored_execution == requested_execution
            if stored_execution
            else stored_source == requested_execution
        )
        return source_matches, execution_matches

    def _stored_identity_matches(
        self,
        state: dict[str, Any],
        plan: AcquisitionPlan,
        *,
        include_papers: bool,
        paper_plan: dict[str, Any] | None,
    ) -> bool:
        if not plan.source_identity:
            return False
        job = state.get("job")
        if not isinstance(job, dict):
            return False
        if str(job.get("dataset_id") or "").casefold() != plan.dataset_id.casefold():
            return False
        if job.get("dataset_license") != plan.license or (job.get("include_papers") is not False) != include_papers:
            return False
        if include_papers and self._paper_identity(job.get("related_paper")) != self._paper_identity(paper_plan):
            return False

        stored_plan = state.get("plan")
        stored_identity = stored_plan.get("source_identity") if isinstance(stored_plan, dict) else None
        if not isinstance(stored_identity, dict):
            stored_url = str(job.get("dataset_source") or "")
            distributions = [{"provider": plan.provider, "content_url": stored_url}]
            adapter = self.provider_registry.for_distributions(distributions)
            try:
                stored_identity = adapter.source_identity({}, distributions) if adapter else None
            except (TypeError, ValueError):
                stored_identity = None
        return isinstance(stored_identity, dict) and stored_identity == plan.source_identity

    @staticmethod
    def _empty_failed_checkpoint(target: Path, state: dict[str, Any] | None) -> bool:
        job = state.get("job") if state else None
        if not isinstance(job, dict) or job.get("status") not in {"failed", "cancelled", "interrupted"}:
            return False
        if int(job.get("bytes_downloaded") or 0) != 0 or job.get("files"):
            return False
        current_file = state.get("current_file") or job.get("current_file")
        if current_file:
            if not isinstance(current_file, dict):
                return False
            if int(current_file.get("bytes_downloaded") or 0) != 0:
                return False
        if not target.exists():
            return True
        if not target.is_dir():
            return False
        return not any(path.is_file() or path.is_symlink() for path in target.rglob("*"))

    @staticmethod
    def _remove_empty_target(target: Path) -> bool:
        if not target.exists():
            return True
        if not target.is_dir() or any(path.is_file() or path.is_symlink() for path in target.rglob("*")):
            return False
        try:
            for directory in sorted(
                (path for path in target.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                directory.rmdir()
            target.rmdir()
        except OSError:
            return False
        return True

    @staticmethod
    def _restored_files(target: Path, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        restored: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            if path == "." and item.get("kind") in {"git_repository", "huggingface_snapshot"}:
                if target.is_dir():
                    restored.append(dict(item))
                continue
            candidate = (target / path).resolve()
            try:
                candidate.relative_to(target.resolve())
            except ValueError:
                continue
            size = item.get("size")
            if candidate.is_file() and isinstance(size, int) and size >= 0 and candidate.stat().st_size == size:
                restored.append(dict(item))
        return restored

    @staticmethod
    def _restored_current_file(target: Path, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        partial_path = str(value.get("partial_path") or "")
        candidate = (target / partial_path).resolve()
        try:
            candidate.relative_to(target.resolve())
        except ValueError:
            return None
        if not candidate.is_file():
            return None
        restored = dict(value)
        restored["bytes_downloaded"] = candidate.stat().st_size
        return restored

    def start(
        self,
        plan: AcquisitionPlan,
        destination: str | None,
        accept_license: bool,
        *,
        include_papers: bool = True,
        paper_plan: dict[str, Any] | None = None,
        create_bundle: bool = False,
        library_dir: str | None = None,
    ) -> dict[str, Any]:
        if not plan.executable_locally:
            result = {"status": "instructions_required", "plan": plan.to_dict()}
            if plan.requires_auth:
                result["auth"] = provider_auth_status(plan.provider, plan.method, required=True)
            return result
        paper_size = paper_plan.get("content_size") if include_papers and paper_plan and paper_plan.get("available") else None
        estimated_size = plan.estimated_size
        if estimated_size is not None and isinstance(paper_size, int):
            estimated_size += paper_size
        elif include_papers and paper_plan and paper_plan.get("available") and paper_size is None:
            estimated_size = None
        if estimated_size is not None and estimated_size > self.max_download_bytes:
            raise ValueError("Dataset exceeds OC_MAX_DOWNLOAD_BYTES")
        target = self.resolve_destination(plan, destination, library_dir=library_dir)
        fingerprint = self.source_fingerprint(plan, include_papers=include_papers, paper_plan=paper_plan)
        execution_fingerprint = self.execution_fingerprint(
            plan,
            include_papers=include_papers,
            paper_plan=paper_plan,
        )
        installation = self.store.inspect(plan.dataset_id, target, source_fingerprint=fingerprint)
        if installation.installed and not create_bundle:
            manifest = self._read_json(target / ".openconstruction-manifest.json") or {}
            installed_state = self._load_target_state(target)
            installed_job = installed_state.get("job") if installed_state else None
            return {
                **installation.to_dict(),
                "status": "already_installed",
                "download_id": installed_job.get("download_id") if isinstance(installed_job, dict) else None,
                "dataset_name": plan.dataset_name,
                "dataset_license": plan.license,
                "reused": True,
                "resumed": False,
                "resume_supported": True,
                "resume_available": False,
                "bytes_downloaded": int(manifest.get("bytes_downloaded") or installation.bytes_present),
                "bytes_total": int(manifest.get("bytes_downloaded") or installation.bytes_present),
                "files": list(manifest.get("files") or []),
                "related_paper": (manifest.get("related_papers") or [None])[0],
                "progress_percent": 100.0,
                "progress_bar": "[████████████████████]",
                "progress_indeterminate": False,
                "progress_text": "Already installed — the matching dataset package is complete.",
                "speed_bytes_per_second": None,
                "eta_seconds": 0,
            }
        persisted = self._load_target_state(target)
        persisted_job = persisted.get("job") if persisted else None
        source_matches, execution_matches = self._checkpoint_compatibility(
            persisted,
            plan,
            include_papers=include_papers,
            paper_plan=paper_plan,
        )
        accepted_by_checkpoint = bool(
            persisted
            and source_matches
            and (execution_matches or self._empty_failed_checkpoint(target, persisted))
            and persisted.get("license_accepted") is True
            and isinstance(persisted_job, dict)
            and persisted_job.get("dataset_license") == plan.license
        )
        if accept_license is not True and not accepted_by_checkpoint:
            raise ValueError("Set accept_license=true only after the user reviews and accepts the dataset license and source terms")
        if plan.requires_auth:
            auth = provider_auth_status(plan.provider, plan.method, required=True)
            if auth["status"] == "auth_required":
                response = auth_required_response(
                    plan.provider,
                    plan.method,
                    dataset_id=plan.dataset_id,
                    destination=destination,
                    accept_license=accept_license,
                )
                response["retry"]["arguments"]["include_papers"] = include_papers
                if library_dir:
                    response["retry"]["arguments"]["library_dir"] = str(target.parent)
                return response
        if not include_papers:
            related_paper = {"dataset_id": plan.dataset_id, "download_status": "disabled"}
        elif paper_plan and paper_plan.get("available"):
            related_paper = {**paper_plan, "download_status": "queued", "local_path": None, "error": None}
        else:
            related_paper = {
                **(paper_plan or {"dataset_id": plan.dataset_id, "availability_status": "not_configured"}),
                "download_status": "unavailable",
                "local_path": None,
            }
        with self._lock:
            running = next(
                (
                    job
                    for job in self._jobs.values()
                    if job.destination == str(target) and job.status not in TERMINAL_STATUSES
                ),
                None,
            )
            if running:
                if running.source_fingerprint != fingerprint:
                    return self._source_changed_response(plan, target, running.source_fingerprint, fingerprint)
                return running.public_dict()

            lock_handle = self._acquire_job_lock(target)
            if lock_handle is None:
                current = self._load_target_state(target)
                if current:
                    result = self._public_from_state(current)
                    result["already_running"] = True
                    return result
                return {
                    "status": "already_running",
                    "dataset_id": plan.dataset_id,
                    "dataset_name": plan.dataset_name,
                    "destination": str(target),
                    "error": "Another OpenConstruction process is already preparing this destination.",
                }

            persisted = self._load_target_state(target)
            persisted_job = persisted.get("job") if persisted else None
            persisted_fingerprint = persisted.get("source_fingerprint") if persisted else None
            source_matches, execution_matches = self._checkpoint_compatibility(
                persisted,
                plan,
                include_papers=include_papers,
                paper_plan=paper_plan,
            )
            checkpoint_reset_warning: str | None = None
            reset_license_accepted_at: int | None = None
            strategy_changed_empty = bool(
                persisted
                and source_matches
                and not execution_matches
                and self._empty_failed_checkpoint(target, persisted)
            )
            if strategy_changed_empty:
                reset_license_accepted_at = int(persisted.get("license_accepted_at") or time.time())
                if not self._remove_empty_target(target):
                    if fcntl is not None:
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                    lock_handle.close()
                    return self._source_changed_response(
                        plan,
                        target,
                        str(persisted_fingerprint or "") or None,
                        fingerprint,
                    )
                checkpoint_reset_warning = (
                    "OpenConstruction reset an empty failed checkpoint created by an older download strategy."
                )
                persisted = None
                persisted_job = None
                persisted_fingerprint = None
                source_matches = False
                execution_matches = False
            elif target.exists() and persisted_fingerprint and (not source_matches or not execution_matches):
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()
                return self._source_changed_response(plan, target, str(persisted_fingerprint), fingerprint)

            can_resume = bool(
                persisted
                and source_matches
                and execution_matches
                and isinstance(persisted_job, dict)
                and persisted.get("license_accepted") is True
            )
            if target.exists() and not can_resume:
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()
                raise ValueError(
                    "Download destination already exists without a matching resumable OC checkpoint; choose another destination or repair it explicitly"
                )

            restored_files = self._restored_files(target, persisted_job.get("files") if can_resume else None)
            restored_current = self._restored_current_file(target, persisted.get("current_file") if can_resume else None)
            restored_bytes = sum(
                int(item.get("size") or 0) for item in restored_files if isinstance(item.get("size"), int)
            )
            if restored_current:
                restored_bytes += int(restored_current.get("bytes_downloaded") or 0)
            restored_total = persisted_job.get("bytes_total") if can_resume else None
            bytes_total = (
                restored_total
                if isinstance(restored_total, int) and restored_total >= restored_bytes
                else estimated_size
            )
            stored_id = str(persisted_job.get("download_id") or "") if can_resume else ""
            expected_prefix = "rp_" if create_bundle else "dl_"
            job_id = stored_id if stored_id.startswith(expected_prefix) and JOB_ID_PATTERN.fullmatch(stored_id) else f"{expected_prefix}{uuid.uuid4().hex[:16]}"
            stored_related = persisted_job.get("related_paper") if can_resume else None
            if isinstance(stored_related, dict) and stored_related.get("download_status") == "completed":
                related_paper = {**related_paper, "download_status": "queued", "local_path": None, "error": None}
            job = DownloadJob(
                id=job_id,
                dataset_id=plan.dataset_id,
                destination=str(target),
                plan=plan.to_dict(),
                library_dir=str(target.parent),
                status="queued",
                bytes_downloaded=restored_bytes,
                bytes_total=bytes_total,
                files=restored_files,
                include_papers=include_papers,
                related_paper=related_paper,
                workflow="research_preparation" if create_bundle else "dataset_download",
                create_bundle=create_bundle,
                source_fingerprint=fingerprint,
                execution_fingerprint=execution_fingerprint,
                resumed=can_resume,
                current_file=restored_current,
                license_accepted_at=(
                    int(persisted.get("license_accepted_at") or time.time())
                    if can_resume
                    else reset_license_accepted_at or int(time.time())
                ),
                warnings=(
                    list(persisted_job.get("warnings") or [])
                    if can_resume and isinstance(persisted_job.get("warnings"), list)
                    else [checkpoint_reset_warning] if checkpoint_reset_warning else []
                ),
                created_at=float(persisted_job.get("created_at") or time.time()) if can_resume else time.time(),
                lock_handle=lock_handle,
            )
            self._jobs[job.id] = job
            self._persist_job(job, force=True)
        thread = threading.Thread(target=self._run, args=(job,), name=f"oc-{job.id}", daemon=True)
        thread.start()
        return job.public_dict()

    def get(self, download_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(download_id)
            if job:
                return job.public_dict()
        state = self._find_state_by_id(download_id)
        if not state:
            raise ValueError(f"Unknown download id: {download_id}")
        return self._public_from_state(state, interrupted=True)

    def cancel(self, download_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(download_id)
            if not job:
                state = self._find_state_by_id(download_id)
                if not state:
                    raise ValueError(f"Unknown download id: {download_id}")
                return self._public_from_state(state, interrupted=True)
            if job.status in {"completed", "failed", "cancelled", "auth_required"}:
                return job.public_dict()
            job.cancel_event.set()
            process = job.process
        if process and process.poll() is None:
            process.terminate()
        return self.get(download_id)

    def _run(self, job: DownloadJob) -> None:
        terminal_status = "failed"
        job.status = "resolving"
        job.started_at = time.time()
        job.completed_at = None
        job.error = None
        self._persist_job(job, force=True)
        target = Path(job.destination)
        try:
            if job.cancel_event.is_set():
                raise InterruptedError("Download cancelled")
            provider = self.provider_registry.for_plan(job.plan)
            if provider is None:
                method = str(job.plan.get("method") or job.plan.get("kind") or "unknown")
                raise ValueError(f"No provider adapter is registered for method: {method}")
            provider.execute(DownloadContext(job=job, target=target, runtime=self))
            if job.cancel_event.is_set():
                raise InterruptedError("Download cancelled")
            self._download_related_paper(job, target)
            if job.cancel_event.is_set():
                raise InterruptedError("Download cancelled")
            self._write_manifest(job, target)
            if job.create_bundle:
                job.status = "preparing_bundle"
                self._persist_job(job, force=True)
                job.research_bundle = create_research_bundle(
                    target=target,
                    preparation_id=job.id,
                    dataset_id=job.dataset_id,
                    plan=job.plan,
                    files=job.files,
                    related_paper=job.related_paper,
                    warnings=job.warnings,
                    source_fingerprint=str(job.source_fingerprint or ""),
                )
            terminal_status = "completed"
            if not (job.related_paper and job.related_paper.get("download_status") == "failed"):
                job.current_file = None
        except InterruptedError as exc:
            terminal_status = "cancelled"
            job.error = str(exc)
        except Exception as exc:
            if self._is_auth_failure(exc):
                terminal_status = "auth_required"
                job.error = "Provider authentication is required or the local credential was rejected."
                job.auth = provider_auth_status(
                    str(job.plan.get("provider") or "") or None,
                    str(job.plan.get("method") or "") or None,
                    required=True,
                    detect_credentials=False,
                )
                job.retry = {
                    "tool": "prepare_dataset_for_research" if job.create_bundle else "download_dataset",
                    "arguments": {
                        "dataset_id": job.dataset_id,
                        "accept_license": True,
                    },
                }
                if not job.create_bundle:
                    job.retry["arguments"]["destination"] = Path(job.destination).name
                    job.retry["arguments"]["include_papers"] = job.include_papers
            else:
                terminal_status = "failed"
                job.error = str(exc)
        finally:
            try:
                with self._lock:
                    job.completed_at = time.time()
                    job.status = terminal_status
                    self._persist_job(job, force=True)
            finally:
                self._release_job_lock(job)

    def _opener(self) -> urllib.request.OpenerDirector:
        return urllib.request.build_opener(SafeRedirectHandler())

    def persist_provider_job(self, job: DownloadJob, *, force: bool = False) -> None:
        """Provider runtime hook that preserves the manager's checkpoint format."""

        self._persist_job(job, force=force)

    def download_url(
        self,
        job: DownloadJob,
        url: str,
        target: Path,
        *,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
    ) -> dict[str, Any]:
        """Provider runtime hook for safe, resumable HTTP downloads."""

        return self._download_url(
            job,
            url,
            target,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
        )

    def fetch_public_json(self, url: str) -> Any:
        """Fetch provider metadata through the same SSRF-safe HTTP policy."""

        self.url_validator(url)
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with self._opener().open(request, timeout=30) as response:
            return json.load(response)

    def probe_url_size(self, url: str) -> int | None:
        """Best-effort public HTTP size discovery without downloading the artifact."""

        self.url_validator(url)
        requests = (
            urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD"),
            urllib.request.Request(
                url,
                headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"},
                method="GET",
            ),
        )
        for request in requests:
            try:
                with self._opener().open(request, timeout=20) as response:
                    content_range = str(response.headers.get("Content-Range") or "")
                    match = re.fullmatch(r"bytes\s+\d+-\d+/(\d+)", content_range.strip())
                    if match:
                        return int(match.group(1))
                    content_length = response.headers.get("Content-Length")
                    if content_length and str(content_length).isdigit():
                        return int(content_length)
            except (OSError, ValueError, urllib.error.URLError):
                continue
        return None

    @staticmethod
    def _is_auth_failure(exc: Exception) -> bool:
        code = getattr(exc, "code", None)
        if code in {401, 403}:
            return True
        try:
            response = getattr(exc, "response", None)
        except (AttributeError, KeyError):
            response = None
        status_code = getattr(response, "status_code", None)
        return status_code in {401, 403}

    @staticmethod
    def _sha256_path(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _display_path(job: DownloadJob, target: Path) -> str:
        try:
            return target.relative_to(Path(job.destination)).as_posix()
        except ValueError:
            return target.name

    def _completed_record(
        self,
        job: DownloadJob,
        target: Path,
        url: str,
        expected_sha256: str | None,
    ) -> dict[str, Any] | None:
        display_path = self._display_path(job, target)
        record = next(
            (
                item
                for item in job.files
                if item.get("path") == display_path
                and isinstance(item.get("size"), int)
                and item.get("source_url") == url
            ),
            None,
        )
        if not record:
            return None
        if target.stat().st_size != record["size"]:
            raise ValueError(f"Existing file size no longer matches its checkpoint: {display_path}")
        actual_sha256 = self._sha256_path(target)
        recorded_sha256 = str(record.get("sha256") or "")
        if recorded_sha256 and actual_sha256.casefold() != recorded_sha256.casefold():
            raise ValueError(f"Existing file checksum no longer matches its checkpoint: {display_path}")
        if expected_sha256 and actual_sha256.casefold() != expected_sha256.casefold():
            raise ValueError(f"Existing file checksum does not match the current source: {display_path}")
        return dict(record)

    def _download_url(
        self,
        job: DownloadJob,
        url: str,
        target: Path,
        *,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
    ) -> dict[str, Any]:
        if job.cancel_event.is_set():
            raise InterruptedError("Download cancelled")
        self.url_validator(url)
        if target.exists():
            record = self._completed_record(job, target, url, expected_sha256)
            if record:
                job.current_file = None
                self._persist_job(job, force=True)
                return record
            raise ValueError(f"Refusing to overwrite untracked existing file: {target.name}")

        partial = target.with_name(target.name + ".part")
        digest = hashlib.sha256()
        offset = partial.stat().st_size if partial.is_file() else 0
        if expected_size is not None and offset > expected_size:
            raise ValueError(f"Partial file is larger than the declared source: {target.name}")
        if offset:
            with partial.open("rb") as existing:
                for chunk in iter(lambda: existing.read(CHUNK_SIZE), b""):
                    digest.update(chunk)
        written = offset
        display_path = self._display_path(job, target)
        partial_path = self._display_path(job, partial)
        previous = job.current_file if isinstance(job.current_file, dict) else {}
        previous_matches = previous.get("path") == display_path and previous.get("source_url") == url
        headers = {"User-Agent": USER_AGENT}
        if offset:
            headers["Range"] = f"bytes={offset}-"
            validator = (previous.get("etag") or previous.get("last_modified")) if previous_matches else None
            if validator:
                headers["If-Range"] = str(validator)
        job.current_file = {
            "path": display_path,
            "partial_path": partial_path,
            "source_url": url,
            "bytes_downloaded": offset,
            "expected_size": expected_size,
            "etag": previous.get("etag") if previous_matches else None,
            "last_modified": previous.get("last_modified") if previous_matches else None,
        }
        job.status = "downloading"
        self._persist_job(job, force=True)

        if expected_size is not None and offset == expected_size and offset > 0:
            actual_sha256 = digest.hexdigest()
            if expected_sha256 and actual_sha256.casefold() != expected_sha256.casefold():
                partial.unlink(missing_ok=True)
                job.bytes_downloaded = max(0, job.bytes_downloaded - offset)
                job.current_file = None
                self._persist_job(job, force=True)
                raise ValueError(f"Checksum mismatch for downloaded file: {target.name}")
            partial.replace(target)
            record = {"path": display_path, "size": written, "sha256": actual_sha256, "source_url": url}
            job.files = [item for item in job.files if item.get("path") != display_path]
            job.files.append(record)
            job.current_file = None
            self._persist_job(job, force=True)
            return record

        request = urllib.request.Request(url, headers=headers)
        try:
            with self._opener().open(request, timeout=30) as response:
                response_status = int(getattr(response, "status", 200) or 200)
                response_headers = response.headers
                content_range = response_headers.get("Content-Range")
                if offset and response_status == 206:
                    if not content_range:
                        raise ValueError("The source omitted Content-Range from its resume response")
                    match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", str(content_range).strip())
                    if not match or int(match.group(1)) != offset:
                        raise ValueError("The source returned an invalid resume range")
                    if expected_size is not None and match.group(3) != "*" and int(match.group(3)) != expected_size:
                        raise ValueError("The remote file size changed since the download started")
                    mode = "ab"
                else:
                    if offset:
                        job.bytes_downloaded = max(0, job.bytes_downloaded - offset)
                    offset = 0
                    written = 0
                    digest = hashlib.sha256()
                    mode = "wb"

                content_length = response.headers.get("Content-Length")
                remote_total = (offset + int(content_length)) if content_length else expected_size
                if remote_total is not None and remote_total > self.max_download_bytes:
                    raise ValueError("Remote file exceeds OC_MAX_DOWNLOAD_BYTES")
                new_etag = response_headers.get("ETag")
                new_last_modified = response_headers.get("Last-Modified")
                if offset and previous_matches and previous.get("etag") and new_etag and previous.get("etag") != new_etag:
                    raise ValueError("The remote file ETag changed since the download started")
                job.current_file.update(
                    {
                        "bytes_downloaded": offset,
                        "etag": new_etag or job.current_file.get("etag"),
                        "last_modified": new_last_modified or job.current_file.get("last_modified"),
                    }
                )
                with partial.open(mode) as output:
                    while True:
                        if job.cancel_event.is_set():
                            raise InterruptedError("Download cancelled")
                        chunk = response.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        written += len(chunk)
                        if job.bytes_downloaded + len(chunk) > self.max_download_bytes:
                            raise ValueError("Download exceeds OC_MAX_DOWNLOAD_BYTES")
                        output.write(chunk)
                        digest.update(chunk)
                        job.bytes_downloaded += len(chunk)
                        job.current_file["bytes_downloaded"] = written
                        self._persist_job(job)

            if expected_size is not None and written != expected_size:
                raise ValueError(
                    f"Incomplete download for {target.name}: received {written} of {expected_size} bytes"
                )
            actual_sha256 = digest.hexdigest()
            if expected_sha256 and actual_sha256.casefold() != expected_sha256.casefold():
                partial.unlink(missing_ok=True)
                job.bytes_downloaded = max(0, job.bytes_downloaded - written)
                job.current_file = None
                self._persist_job(job, force=True)
                raise ValueError(f"Checksum mismatch for downloaded file: {target.name}")
            partial.replace(target)
        except Exception:
            if partial.is_file() and job.current_file is not None:
                job.current_file["bytes_downloaded"] = partial.stat().st_size
            self._persist_job(job, force=True)
            raise
        record = {"path": display_path, "size": written, "sha256": digest.hexdigest(), "source_url": url}
        job.files = [item for item in job.files if item.get("path") != display_path]
        job.files.append(record)
        job.current_file = None
        self._persist_job(job, force=True)
        return record

    def _download_related_paper(self, job: DownloadJob, target: Path) -> None:
        paper = job.related_paper
        if not paper or paper.get("download_status") != "queued":
            return
        before = job.bytes_downloaded
        try:
            paper["download_status"] = "downloading"
            paper_url = str(paper.get("content_url") or "")
            paper_size = paper.get("content_size") if isinstance(paper.get("content_size"), int) else None
            if paper_size is None:
                paper_size = self.probe_url_size(paper_url)
            if paper_size is not None and job.bytes_total is None:
                job.bytes_total = job.bytes_downloaded + paper_size
                self._persist_job(job, force=True)
            paper_dir = target / "papers"
            paper_dir.mkdir(parents=True, exist_ok=True)
            record = self._download_url(
                job,
                paper_url,
                paper_dir / "paper.pdf",
                expected_sha256=str(paper.get("sha256") or "") or None,
                expected_size=paper_size,
            )
            paper["download_status"] = "completed"
            paper["local_path"] = record["path"]
            paper["downloaded_sha256"] = record["sha256"]
            paper["downloaded_bytes"] = record["size"]
        except InterruptedError:
            raise
        except Exception as exc:
            paper["download_status"] = "failed"
            paper["error"] = str(exc)
            paper["resume_available"] = bool(job.current_file)
            if job.create_bundle:
                raise
            job.bytes_downloaded = before
            declared_size = paper.get("content_size")
            if job.bytes_total is not None and isinstance(declared_size, int):
                job.bytes_total = max(0, job.bytes_total - declared_size)
            job.warnings.append("The dataset downloaded, but its related paper could not be downloaded from the OC source.")

    def _write_manifest(self, job: DownloadJob, target: Path) -> None:
        job.status = "verifying"
        manifest = {
            "schema_version": "1.0",
            "dataset_id": job.dataset_id,
            "provider": job.plan.get("provider"),
            "method": job.plan.get("method"),
            "license": job.plan.get("license"),
            "source": job.plan.get("url"),
            "source_identity": job.plan.get("source_identity"),
            "source_fingerprint": job.source_fingerprint,
            "execution_fingerprint": job.execution_fingerprint,
            "preparation_id": job.id if job.create_bundle else None,
            "downloaded_at": int(time.time()),
            "bytes_downloaded": job.bytes_downloaded,
            "files": job.files,
            "include_papers": job.include_papers,
            "related_papers": [job.related_paper] if job.related_paper else [],
            "warnings": job.warnings,
        }
        target.mkdir(parents=True, exist_ok=True)
        manifest_path = target / ".openconstruction-manifest.json"
        self._atomic_json(manifest_path, manifest)
