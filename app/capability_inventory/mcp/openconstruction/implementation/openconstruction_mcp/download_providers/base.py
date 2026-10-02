from __future__ import annotations

from abc import ABC
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class ProviderCapabilities:
    """Machine-readable behavior exposed to every OC interface."""

    access_mode: str
    executable: bool
    supports_multiple_files: bool = False
    supports_resume: bool = False
    supports_versions: bool = False
    supports_auth: bool = False

    def __post_init__(self) -> None:
        if self.access_mode not in {"direct", "programmatic", "assisted"}:
            raise ValueError(f"Invalid provider access_mode: {self.access_mode}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProviderRuntime(Protocol):
    """Safe shared primitives available to provider implementations."""

    def download_url(
        self,
        job: Any,
        url: str,
        target: Path,
        *,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
    ) -> dict[str, Any]: ...

    def fetch_public_json(self, url: str) -> Any: ...

    def probe_url_size(self, url: str) -> int | None: ...

    def persist_provider_job(self, job: Any, *, force: bool = False) -> None: ...


@dataclass(frozen=True)
class DownloadContext:
    job: Any
    target: Path
    runtime: ProviderRuntime

    @property
    def plan(self) -> Mapping[str, Any]:
        return self.job.plan


class DownloadProvider(ABC):
    """Stable provider boundary used by acquisition planning and execution."""

    method: str
    provider_ids: tuple[str, ...] = ()
    handles_distributions: bool = False
    capabilities: ProviderCapabilities

    def validate(
        self,
        metadata: Mapping[str, Any],
        distributions: list[dict[str, Any]],
    ) -> None:
        """Raise ValueError when a catalog source cannot be handled safely."""

    def configuration_error(
        self,
        metadata: Mapping[str, Any],
        distributions: list[dict[str, Any]],
    ) -> str | None:
        try:
            self.validate(metadata, distributions)
        except ValueError as exc:
            return str(exc)
        return None

    def source_identity(
        self,
        metadata: Mapping[str, Any],
        distributions: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Return an immutable provider resource identity when one is available."""

        return None

    def instructions(
        self,
        dataset_id: str,
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "summary": f"Use the OpenConstruction {self.method} provider.",
            "command": None,
        }

    def execute(self, context: DownloadContext) -> None:
        raise ValueError(f"Provider method {self.method} requires an assisted workflow")
