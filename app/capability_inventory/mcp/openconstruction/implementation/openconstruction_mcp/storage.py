from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


DATASET_DIRECTORY_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
DIRECTORY_RECORD_KINDS = {"git_repository", "huggingface_snapshot"}


def default_oc_home() -> Path:
    configured = os.environ.get("OC_HOME", "").strip()
    return Path(configured).expanduser().resolve() if configured else (Path.home() / ".openconstruction").resolve()


def default_dataset_root() -> Path:
    configured = (
        os.environ.get("OC_DATASETS_DIR", "").strip()
        or os.environ.get("OC_DOWNLOAD_ROOT", "").strip()
    )
    return Path(configured).expanduser().resolve() if configured else default_oc_home() / "datasets"


def safe_dataset_directory(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-.")
    return name or "dataset"


@dataclass(frozen=True)
class InstallationStatus:
    dataset_id: str
    status: str
    library_dir: str
    destination: str
    exists: bool
    installed: bool
    resume_available: bool = False
    source_matches: bool | None = None
    manifest_path: str | None = None
    files_checked: int = 0
    files_missing: list[str] = field(default_factory=list)
    bytes_present: int = 0
    source_fingerprint: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DatasetStore:
    """Resolves OC library locations and inspects installed dataset packages."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser().resolve() if root is not None else default_dataset_root()
        self._validate_library_dir(self.root)

    @staticmethod
    def _validate_library_dir(path: Path) -> None:
        if path == Path(path.anchor):
            raise ValueError("The filesystem root cannot be used as an OpenConstruction dataset library")
        if path.exists() and not path.is_dir():
            raise ValueError("The OpenConstruction dataset library path is not a directory")

    def resolve_library_dir(self, library_dir: str | Path | None = None) -> Path:
        if library_dir is None or not str(library_dir).strip():
            return self.root
        value = Path(str(library_dir).strip()).expanduser()
        resolved = value.resolve() if value.is_absolute() else (Path.cwd() / value).resolve()
        self._validate_library_dir(resolved)
        return resolved

    def resolve_target(
        self,
        dataset_id: str,
        destination: str | None = None,
        *,
        library_dir: str | Path | None = None,
    ) -> Path:
        base = self.resolve_library_dir(library_dir)
        directory = destination.strip() if isinstance(destination, str) and destination.strip() else safe_dataset_directory(dataset_id)
        candidate = Path(directory)
        if candidate.is_absolute() or len(candidate.parts) != 1 or not DATASET_DIRECTORY_PATTERN.fullmatch(candidate.name):
            raise ValueError(
                "destination must be one safe dataset directory name inside OC_DOWNLOAD_ROOT or library_dir; "
                "use library_dir to select its parent"
            )
        if candidate.name.casefold() == ".openconstruction-state":
            raise ValueError("destination uses OpenConstruction's reserved state directory")
        target = (base / candidate.name).resolve()
        try:
            target.relative_to(base)
        except ValueError as exc:
            raise ValueError("destination must stay inside the selected OpenConstruction dataset library") from exc
        return target

    @staticmethod
    def _read_manifest(path: Path) -> dict[str, Any] | None:
        try:
            if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def inspect(
        self,
        dataset_id: str,
        target: Path,
        *,
        source_fingerprint: str | None = None,
        verify_checksums: bool = False,
    ) -> InstallationStatus:
        library_dir = str(target.parent)
        if not target.exists():
            return InstallationStatus(
                dataset_id=dataset_id,
                status="not_installed",
                library_dir=library_dir,
                destination=str(target),
                exists=False,
                installed=False,
                message="No dataset directory exists at the selected location.",
            )
        if not target.is_dir():
            return InstallationStatus(
                dataset_id=dataset_id,
                status="conflict",
                library_dir=library_dir,
                destination=str(target),
                exists=True,
                installed=False,
                message="The selected destination exists but is not a directory.",
            )

        manifest_path = target / ".openconstruction-manifest.json"
        manifest = self._read_manifest(manifest_path)
        if manifest is None:
            return InstallationStatus(
                dataset_id=dataset_id,
                status="unmanaged_directory",
                library_dir=library_dir,
                destination=str(target),
                exists=True,
                installed=False,
                manifest_path=str(manifest_path),
                message="The directory exists but has no valid OpenConstruction manifest.",
            )

        manifest_dataset_id = str(manifest.get("dataset_id") or "")
        manifest_fingerprint = str(manifest.get("source_fingerprint") or "") or None
        identity_matches = manifest_dataset_id.casefold() == dataset_id.casefold()
        source_matches = source_fingerprint is None or manifest_fingerprint == source_fingerprint
        if not identity_matches or not source_matches:
            return InstallationStatus(
                dataset_id=dataset_id,
                status="source_conflict",
                library_dir=library_dir,
                destination=str(target),
                exists=True,
                installed=False,
                source_matches=False,
                manifest_path=str(manifest_path),
                source_fingerprint=manifest_fingerprint,
                message="The installed package belongs to a different dataset source or version.",
            )

        files = manifest.get("files")
        records = [item for item in files if isinstance(item, dict)] if isinstance(files, list) else []
        missing: list[str] = []
        checked = 0
        bytes_present = 0
        for item in records:
            relative = str(item.get("path") or "")
            if relative == "." and item.get("kind") in DIRECTORY_RECORD_KINDS:
                checked += 1
                if not target.is_dir():
                    missing.append(".")
                continue
            candidate = (target / relative).resolve()
            try:
                candidate.relative_to(target)
            except ValueError:
                missing.append(relative or "<invalid-path>")
                continue
            checked += 1
            size = item.get("size")
            if not candidate.is_file() or (isinstance(size, int) and candidate.stat().st_size != size):
                missing.append(relative)
                continue
            bytes_present += candidate.stat().st_size
            checksum = str(item.get("sha256") or "")
            if verify_checksums and checksum and self._sha256(candidate).casefold() != checksum.casefold():
                missing.append(relative)

        installed = bool(records) and not missing
        return InstallationStatus(
            dataset_id=dataset_id,
            status="installed" if installed else "incomplete",
            library_dir=library_dir,
            destination=str(target),
            exists=True,
            installed=installed,
            source_matches=True,
            manifest_path=str(manifest_path),
            files_checked=checked,
            files_missing=missing,
            bytes_present=bytes_present,
            source_fingerprint=manifest_fingerprint,
            message=(
                "A matching OpenConstruction dataset package is already installed."
                if installed
                else "The package manifest exists, but one or more downloaded files are missing or changed."
            ),
        )
