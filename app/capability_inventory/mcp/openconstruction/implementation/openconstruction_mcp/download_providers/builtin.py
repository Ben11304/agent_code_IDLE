from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any, Mapping

from .base import DownloadContext, DownloadProvider, ProviderCapabilities


REPO_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")


def _safe_filename(value: str, fallback: str = "download.bin") -> str:
    name = Path(value).name
    if name in {"", ".", ".."} or not re.fullmatch(r"[A-Za-z0-9._ -]+", name):
        return fallback
    return name


def _distribution_url(item: Mapping[str, Any]) -> str:
    return str(item.get("content_url") or item.get("contentUrl") or item.get("url") or "")


def _valid_http_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(value)
    except ValueError:
        return False
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and not parsed.username
        and not parsed.password
    )


def _distributions(context: DownloadContext) -> list[dict[str, Any]]:
    value = context.plan.get("distributions")
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _paper_size(job: Any) -> tuple[bool, int | None]:
    paper = job.related_paper if isinstance(job.related_paper, dict) else None
    if not paper or paper.get("download_status") in {"disabled", "unavailable"}:
        return False, 0
    size = paper.get("content_size")
    return True, size if isinstance(size, int) and size >= 0 else None


def _set_total_size(context: DownloadContext, sizes: list[int | None]) -> None:
    paper_expected, paper_bytes = _paper_size(context.job)
    if not sizes or any(size is None for size in sizes) or (paper_expected and paper_bytes is None):
        return
    context.job.bytes_total = sum(size for size in sizes if size is not None) + int(paper_bytes or 0)
    context.runtime.persist_provider_job(context.job, force=True)


def _known_or_remote_size(context: DownloadContext, item: Mapping[str, Any]) -> int | None:
    declared = item.get("content_size")
    if isinstance(declared, int) and declared >= 0:
        return declared
    url = _distribution_url(item)
    completed = next(
        (
            record
            for record in context.job.files
            if isinstance(record, dict)
            and record.get("source_url") == url
            and isinstance(record.get("size"), int)
        ),
        None,
    )
    if completed:
        return int(completed["size"])
    return context.runtime.probe_url_size(url)


class DirectDownloadProvider(DownloadProvider):
    method = "direct_download"
    provider_ids = ("http", "https")
    capabilities = ProviderCapabilities(
        access_mode="direct",
        executable=True,
        supports_multiple_files=True,
        supports_resume=True,
    )

    def validate(self, metadata: Mapping[str, Any], distributions: list[dict[str, Any]]) -> None:
        if not distributions:
            raise ValueError("Direct downloads require at least one distribution")
        if any(not _valid_http_url(_distribution_url(item)) for item in distributions):
            raise ValueError("Every direct distribution must declare a valid HTTP(S) content URL")

    def instructions(self, dataset_id: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
        return {"summary": "Download the declared public dataset files.", "command": None}

    def execute(self, context: DownloadContext) -> None:
        items = _distributions(context)
        self.validate({}, items)
        context.target.mkdir(parents=True, exist_ok=context.job.resumed)
        sizes = [_known_or_remote_size(context, item) for item in items]
        _set_total_size(context, sizes)
        for index, item in enumerate(items, start=1):
            filename = _safe_filename(str(item.get("filename") or ""), f"download-{index}.bin")
            size = sizes[index - 1]
            context.runtime.download_url(
                context.job,
                _distribution_url(item),
                context.target / filename,
                expected_sha256=str(item.get("sha256") or "") or None,
                expected_size=size,
            )


class HttpFilesProvider(DirectDownloadProvider):
    method = "http_files"
    provider_ids = ("http", "https")
    capabilities = ProviderCapabilities(
        access_mode="programmatic",
        executable=True,
        supports_multiple_files=True,
        supports_resume=True,
    )

    def instructions(self, dataset_id: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
        return {"summary": "The local OC provider can download all declared HTTP files.", "command": None}


class ZenodoFilesProvider(DirectDownloadProvider):
    method = "zenodo_files"
    provider_ids = ("zenodo",)
    handles_distributions = True
    capabilities = ProviderCapabilities(
        access_mode="programmatic",
        executable=True,
        supports_multiple_files=True,
        supports_resume=True,
        supports_versions=True,
    )

    _record_pattern = re.compile(r"^/api/records/(\d+)/(?:files-archive|files(?:/.*)?/content)$")

    @classmethod
    def _record_id(cls, url: str) -> str | None:
        try:
            parsed = urllib.parse.urlparse(url)
        except ValueError:
            return None
        if parsed.scheme != "https" or parsed.hostname != "zenodo.org":
            return None
        match = cls._record_pattern.fullmatch(parsed.path)
        return match.group(1) if match else None

    def validate(self, metadata: Mapping[str, Any], distributions: list[dict[str, Any]]) -> None:
        super().validate(metadata, distributions)
        record_ids = {self._record_id(_distribution_url(item)) for item in distributions}
        if None in record_ids or len(record_ids) != 1:
            raise ValueError("Zenodo distributions must use one public zenodo.org record")

    def source_identity(
        self,
        metadata: Mapping[str, Any],
        distributions: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        record_ids = {self._record_id(_distribution_url(item)) for item in distributions}
        if None in record_ids or len(record_ids) != 1:
            return None
        record_id = next(iter(record_ids))
        return {"provider": "zenodo", "record_id": record_id, "revision": record_id}

    def instructions(self, dataset_id: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "summary": "Enumerate the public Zenodo record and download each declared file with resume support.",
            "command": None,
        }

    @staticmethod
    def _target_path(target: Path, key: str, index: int) -> Path:
        normalized = str(key or "").replace("\\", "/").strip("/")
        parts = normalized.split("/") if normalized else []
        if not parts or any(part in {"", ".", ".."} or "\x00" in part for part in parts):
            return target / f"zenodo-{index}.bin"
        candidate = (target.joinpath(*parts)).resolve()
        try:
            candidate.relative_to(target.resolve())
        except ValueError as exc:
            raise ValueError("Zenodo returned an unsafe file path") from exc
        return candidate

    def execute(self, context: DownloadContext) -> None:
        distributions = _distributions(context)
        self.validate({}, distributions)
        archive_requested = any(
            urllib.parse.urlparse(_distribution_url(item)).path.endswith("/files-archive")
            for item in distributions
        )
        if not archive_requested:
            super().execute(context)
            return

        record_id = self._record_id(_distribution_url(distributions[0]))
        payload = context.runtime.fetch_public_json(f"https://zenodo.org/api/records/{record_id}")
        files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files, list) or not files:
            raise ValueError("Zenodo returned no public files")

        context.target.mkdir(parents=True, exist_ok=context.job.resumed)
        sizes = [
            item.get("size") if isinstance(item, dict) and isinstance(item.get("size"), int) else None
            for item in files
        ]
        _set_total_size(context, sizes)
        destinations: set[Path] = set()
        for index, item in enumerate(files, start=1):
            if not isinstance(item, dict):
                continue
            links = item.get("links") if isinstance(item.get("links"), dict) else {}
            url = str(links.get("content") or links.get("self") or "")
            if not _valid_http_url(url):
                raise ValueError("Zenodo returned a file without a public content URL")
            destination = self._target_path(context.target, str(item.get("key") or ""), index)
            if destination in destinations:
                raise ValueError("Zenodo returned duplicate file paths")
            destinations.add(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            size = item.get("size")
            context.runtime.download_url(
                context.job,
                url,
                destination,
                expected_size=size if isinstance(size, int) else None,
            )


class GitHubCloneProvider(DownloadProvider):
    method = "github_clone"
    provider_ids = ("github",)
    capabilities = ProviderCapabilities(
        access_mode="programmatic",
        executable=True,
        supports_resume=True,
        supports_versions=True,
        supports_auth=True,
    )

    def validate(self, metadata: Mapping[str, Any], distributions: list[dict[str, Any]]) -> None:
        if not REPO_ID_PATTERN.fullmatch(str(metadata.get("repo_id") or "")):
            raise ValueError("github_clone requires a valid owner/repository repo_id")

    def instructions(self, dataset_id: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
        repo_id = str(metadata.get("repo_id") or "")
        if self.configuration_error(metadata, []) is None:
            destination = f"./{re.sub(r'[^A-Za-z0-9._-]+', '-', dataset_id).strip('-.') or 'dataset'}"
            return {
                "summary": "Clone the versioned dataset repository with Git.",
                "command": f"git clone {shlex.quote(f'https://github.com/{repo_id}.git')} {shlex.quote(destination)}",
            }
        return super().instructions(dataset_id, metadata)

    def execute(self, context: DownloadContext) -> None:
        metadata = context.plan.get("programmatic_access") or {}
        self.validate(metadata, _distributions(context))
        repo_id = str(metadata.get("repo_id") or "")
        if shutil.which("git") is None:
            raise ValueError("git is required for github_clone downloads")
        target = context.target
        target.parent.mkdir(parents=True, exist_ok=True)
        revision = str(metadata.get("revision") or "").strip()
        if revision and not re.fullmatch(r"[A-Za-z0-9._/-]+", revision):
            raise ValueError("Invalid Git revision in catalog metadata")
        expected_remote = f"https://github.com/{repo_id}.git"
        context.job.status = "downloading"
        context.runtime.persist_provider_job(context.job, force=True)

        if (target / ".git").is_dir():
            remote = subprocess.run(
                ["git", "-C", str(target), "remote", "get-url", "origin"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
            normalized_remote = remote.stdout.strip().removesuffix("/")
            if (
                remote.returncode != 0
                or normalized_remote.removesuffix(".git") != expected_remote.removesuffix(".git")
            ):
                raise ValueError("Existing Git checkout does not match the current dataset source")
            head = subprocess.run(
                ["git", "-C", str(target), "rev-parse", "HEAD"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
            if head.returncode != 0:
                fetch_ref = revision or "HEAD"
                fetch = subprocess.run(
                    ["git", "-C", str(target), "fetch", "--depth", "1", "origin", fetch_ref],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                checkout = subprocess.run(
                    ["git", "-C", str(target), "checkout", "--detach", "FETCH_HEAD"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                if fetch.returncode != 0 or checkout.returncode != 0:
                    raise ValueError("The interrupted Git checkout could not be resumed safely")
                head = subprocess.run(
                    ["git", "-C", str(target), "rev-parse", "HEAD"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    check=False,
                )
            record = {
                "path": ".",
                "kind": "git_repository",
                "source": repo_id,
                "revision": revision or None,
                "commit": head.stdout.strip() or None,
            }
            context.job.files = [item for item in context.job.files if item.get("kind") != "git_repository"]
            context.job.files.append(record)
            context.runtime.persist_provider_job(context.job, force=True)
            return

        if target.exists() and any(target.iterdir()):
            raise ValueError("The interrupted Git destination is not a valid repository and will not be overwritten")
        command = ["git", "clone", "--depth", "1"]
        if revision and revision not in {"main", "master"}:
            command.extend(["--branch", revision])
        command.extend([expected_remote, str(target)])
        context.job.process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        while context.job.process.poll() is None:
            if context.job.cancel_event.wait(0.2):
                context.job.process.terminate()
                raise InterruptedError("Download cancelled")
        if context.job.process.returncode != 0:
            raise ValueError(f"Git clone failed with exit code {context.job.process.returncode}")
        head = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        context.job.files.append(
            {
                "path": ".",
                "kind": "git_repository",
                "source": repo_id,
                "revision": revision or None,
                "commit": head.stdout.strip() or None,
            }
        )
        context.runtime.persist_provider_job(context.job, force=True)


class HuggingFaceSnapshotProvider(DownloadProvider):
    method = "huggingface_snapshot"
    provider_ids = ("huggingface",)
    capabilities = ProviderCapabilities(
        access_mode="programmatic",
        executable=True,
        supports_multiple_files=True,
        supports_resume=True,
        supports_versions=True,
        supports_auth=True,
    )

    def validate(self, metadata: Mapping[str, Any], distributions: list[dict[str, Any]]) -> None:
        if not REPO_ID_PATTERN.fullmatch(str(metadata.get("repo_id") or "")):
            raise ValueError("huggingface_snapshot requires a valid owner/repository repo_id")

    def instructions(self, dataset_id: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
        repo_id = str(metadata.get("repo_id") or "")
        if self.configuration_error(metadata, []) is None:
            revision = str(metadata.get("revision") or "main")
            destination = f"./{re.sub(r'[^A-Za-z0-9._-]+', '-', dataset_id).strip('-.') or 'dataset'}"
            return {
                "summary": "Download the pinned Hugging Face dataset snapshot.",
                "command": (
                    "python -m pip install -U huggingface_hub\n"
                    f"hf download {shlex.quote(repo_id)} --repo-type dataset "
                    f"--revision {shlex.quote(revision)} --local-dir {shlex.quote(destination)}"
                ),
            }
        return super().instructions(dataset_id, metadata)

    def execute(self, context: DownloadContext) -> None:
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise ValueError("Install huggingface_hub to execute huggingface_snapshot downloads") from exc
        metadata = context.plan.get("programmatic_access") or {}
        self.validate(metadata, _distributions(context))
        repo_id = str(metadata.get("repo_id") or "")
        context.target.parent.mkdir(parents=True, exist_ok=True)
        context.job.status = "downloading"
        context.runtime.persist_provider_job(context.job, force=True)
        try:
            from huggingface_hub import HfApi

            info = HfApi().dataset_info(
                repo_id,
                revision=str(metadata.get("revision") or "main"),
                files_metadata=True,
                token=True if bool(context.plan.get("requires_auth")) else None,
            )
            siblings = list(info.siblings or [])
            sizes = [getattr(item, "size", None) for item in siblings]
            if siblings and all(isinstance(size, int) and size >= 0 for size in sizes):
                _set_total_size(context, sizes)
        except Exception:
            pass
        snapshot_download(
            repo_id=repo_id,
            repo_type="dataset",
            revision=str(metadata.get("revision") or "main"),
            local_dir=str(context.target),
            token=True if bool(context.plan.get("requires_auth")) else None,
        )
        context.job.files = [item for item in context.job.files if item.get("kind") != "huggingface_snapshot"]
        context.job.files.append({"path": ".", "kind": "huggingface_snapshot", "source": repo_id})
        context.runtime.persist_provider_job(context.job, force=True)


class FigshareFilesProvider(DownloadProvider):
    method = "figshare_files"
    provider_ids = ("figshare", "dtu_data")
    capabilities = ProviderCapabilities(
        access_mode="programmatic",
        executable=True,
        supports_multiple_files=True,
        supports_resume=True,
        supports_versions=True,
    )

    def validate(self, metadata: Mapping[str, Any], distributions: list[dict[str, Any]]) -> None:
        if not str(metadata.get("record_id") or "").isdigit():
            raise ValueError("figshare_files requires a numeric record_id")
        api_url = str(metadata.get("api_url") or "")
        if api_url:
            parsed = urllib.parse.urlparse(api_url)
            if parsed.scheme != "https" or parsed.hostname != "api.figshare.com":
                raise ValueError("Figshare API URL must use https://api.figshare.com")

    def execute(self, context: DownloadContext) -> None:
        metadata = context.plan.get("programmatic_access") or {}
        self.validate(metadata, _distributions(context))
        record_id = str(metadata.get("record_id") or "")
        api_url = str(metadata.get("api_url") or f"https://api.figshare.com/v2/articles/{record_id}")
        payload = context.runtime.fetch_public_json(api_url)
        files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files, list) or not files:
            raise ValueError("Figshare returned no public files")
        context.target.mkdir(parents=True, exist_ok=context.job.resumed)
        sizes = [
            item.get("size") if isinstance(item, dict) and isinstance(item.get("size"), int) else None
            for item in files
        ]
        _set_total_size(context, sizes)
        for index, item in enumerate(files, start=1):
            if not isinstance(item, dict):
                continue
            filename = _safe_filename(str(item.get("name") or ""), f"figshare-{index}.bin")
            size = item.get("size")
            context.runtime.download_url(
                context.job,
                str(item.get("download_url") or ""),
                context.target / filename,
                expected_size=size if isinstance(size, int) else None,
            )


class AssistedProvider(DownloadProvider):
    capabilities = ProviderCapabilities(
        access_mode="assisted",
        executable=False,
        supports_multiple_files=True,
        supports_auth=True,
    )

    def __init__(
        self,
        method: str,
        provider_ids: tuple[str, ...],
        summary: str,
    ) -> None:
        self.method = method
        self.provider_ids = provider_ids
        self.summary = summary

    def instructions(self, dataset_id: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "command": None,
            "documentation_url": str(metadata.get("documentation_url") or "") or None,
            "notice": str(metadata.get("notice") or "") or None,
        }


def builtin_providers() -> tuple[DownloadProvider, ...]:
    return (
        DirectDownloadProvider(),
        HttpFilesProvider(),
        ZenodoFilesProvider(),
        GitHubCloneProvider(),
        HuggingFaceSnapshotProvider(),
        FigshareFilesProvider(),
        AssistedProvider(
            "google_drive_folder",
            ("google_drive",),
            "Use the Google Drive API with the user's own credentials.",
        ),
        AssistedProvider(
            "dataverse_collection",
            ("dataverse",),
            "Enumerate the Dataverse collection and download its published dataset bundles.",
        ),
        AssistedProvider(
            "designsafe_globus",
            ("designsafe", "globus"),
            "Authenticate with Globus and select a user-controlled destination collection.",
        ),
        AssistedProvider(
            "dreamhouse_setup",
            ("dreamhouse",),
            "Install the official DreamHouse package and run its artifact setup command.",
        ),
        AssistedProvider(
            "roboflow_version",
            ("roboflow",),
            "Authenticate with Roboflow and request the declared versioned export.",
        ),
        AssistedProvider(
            "kaggle_competition",
            ("kaggle",),
            "Accept the competition rules and authenticate with the Kaggle CLI.",
        ),
        AssistedProvider(
            "baidu_share_transfer",
            ("baidu_pan",),
            "Authenticate locally with BaiduPCS-Go and transfer the shared dataset.",
        ),
    )
