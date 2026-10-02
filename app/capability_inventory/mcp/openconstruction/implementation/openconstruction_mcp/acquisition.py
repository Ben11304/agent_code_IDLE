from __future__ import annotations

import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Any

from .catalog import CatalogClient, clean_text
from .download_providers import ProviderRegistry, get_default_provider_registry
from .provider_auth import provider_auth_status


def _http_url(value: Any) -> str | None:
    text = clean_text(value, 1000)
    if not text:
        return None
    try:
        parsed = urllib.parse.urlparse(text)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    return text


def _objects(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


@dataclass(frozen=True)
class AcquisitionPlan:
    dataset_id: str
    dataset_name: str
    kind: str
    provider: str | None = None
    method: str | None = None
    url: str | None = None
    filename: str | None = None
    license: str | None = None
    requires_auth: bool = False
    executable_locally: bool = False
    estimated_size: int | None = None
    distributions: list[dict[str, Any]] = field(default_factory=list)
    programmatic_access: dict[str, Any] | None = None
    instructions: dict[str, Any] | None = None
    auth: dict[str, Any] | None = None
    capabilities: dict[str, Any] | None = None
    source_identity: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def get_exact_dataset(catalog: CatalogClient, dataset_id: str) -> dict[str, Any]:
    requested = clean_text(dataset_id, 180).casefold()
    if not requested:
        raise ValueError("dataset_id is required")
    for resource in catalog.load():
        if resource.get("type") != "dataset":
            continue
        if clean_text(resource.get("id"), 180).casefold() == requested:
            return resource
    raise ValueError(f"Unknown dataset id: {dataset_id}")


def _instructions(
    dataset_id: str,
    method: str,
    metadata: dict[str, Any],
    registry: ProviderRegistry,
) -> dict[str, Any]:
    provider = registry.get(method)
    if provider:
        return provider.instructions(dataset_id, metadata)
    return {
        "summary": "Follow the provider-specific access instructions.",
        "command": None,
        "documentation_url": _http_url(metadata.get("documentation_url")),
        "notice": clean_text(metadata.get("notice"), 1000) or None,
    }


def resolve_dataset_download_plan(
    catalog: CatalogClient,
    dataset_id: str,
    *,
    provider_registry: ProviderRegistry | None = None,
) -> AcquisitionPlan:
    registry = provider_registry or get_default_provider_registry()
    resource = get_exact_dataset(catalog, dataset_id)
    source = resource.get("source") if isinstance(resource.get("source"), dict) else {}
    canonical_id = clean_text(resource.get("id"), 180)
    dataset_name = clean_text(resource.get("title"), 240) or canonical_id
    license_name = clean_text(source.get("license") or resource.get("license"), 160) or None
    distributions = _objects(source.get("distribution"))

    direct_urls = [
        _http_url(item.get("content_url") or item.get("contentUrl") or item.get("url"))
        for item in distributions
    ]
    if distributions and all(item.get("browser_download") is not False for item in distributions) and all(direct_urls):
        direct_provider = registry.for_distributions(distributions) or registry.get("direct_download")
        configuration_error = (
            direct_provider.configuration_error({}, distributions) if direct_provider else "No direct-download adapter is registered"
        )
        if configuration_error is None:
            sizes = [item.get("content_size") for item in distributions]
            estimated_size = sum(sizes) if sizes and all(isinstance(size, int) and size >= 0 for size in sizes) else None
            provider_names = {
                clean_text(item.get("provider"), 120) for item in distributions if clean_text(item.get("provider"), 120)
            }
            single = distributions[0] if len(distributions) == 1 else {}
            return AcquisitionPlan(
                dataset_id=canonical_id,
                dataset_name=dataset_name,
                kind="direct",
                provider=next(iter(provider_names)) if len(provider_names) == 1 else "http",
                method=(
                    direct_provider.method
                    if direct_provider and direct_provider.handles_distributions
                    else (
                        clean_text(single.get("download_method"), 80) or "navigate"
                        if len(distributions) == 1
                        else "direct_download"
                    )
                ),
                url=direct_urls[0] if len(direct_urls) == 1 else _http_url(source.get("access")),
                filename=clean_text(single.get("filename"), 240) or None,
                license=license_name,
                executable_locally=bool(direct_provider and direct_provider.capabilities.executable),
                estimated_size=estimated_size,
                distributions=distributions,
                instructions=direct_provider.instructions(canonical_id, {}) if direct_provider else None,
                capabilities=direct_provider.capabilities.to_dict() if direct_provider else None,
                source_identity=direct_provider.source_identity({}, distributions) if direct_provider else None,
            )

    methods = _objects(source.get("programmatic_access"))
    if methods:
        method_metadata = methods[0]
        method = clean_text(method_metadata.get("method"), 120)
        provider = clean_text(method_metadata.get("provider"), 120) or None
        requires_auth = method_metadata.get("requires_auth") is True
        adapter = registry.get(method)
        configuration_error = adapter.configuration_error(method_metadata, distributions) if adapter else None
        executable = bool(adapter and adapter.capabilities.executable and configuration_error is None)
        sizes = [item.get("content_size") for item in distributions]
        estimated_size = (
            sum(sizes) if sizes and all(isinstance(size, int) and size >= 0 for size in sizes) else None
        )
        warnings: list[str] = []
        if adapter is None:
            warnings.append(f"No provider adapter is registered for method: {method or 'unknown'}.")
        elif configuration_error:
            warnings.append(f"The provider configuration is invalid: {configuration_error}")
        elif not adapter.capabilities.executable:
            warnings.append("This provider still requires an assisted or provider-specific workflow.")
        if requires_auth:
            warnings.append("Provider authentication is required and must be completed locally by the user.")
        if estimated_size is None and executable:
            warnings.append("The source does not declare a total size, so disk usage cannot be fully checked before execution.")
        return AcquisitionPlan(
            dataset_id=canonical_id,
            dataset_name=dataset_name,
            kind="programmatic",
            provider=provider,
            method=method or None,
            url=_http_url(method_metadata.get("documentation_url")) or _http_url(source.get("access")),
            license=license_name,
            requires_auth=requires_auth,
            executable_locally=executable,
            estimated_size=estimated_size,
            distributions=distributions,
            programmatic_access=method_metadata,
            instructions=_instructions(canonical_id, method, method_metadata, registry),
            auth=(
                provider_auth_status(provider, method, required=True, detect_credentials=False)
                if requires_auth
                else None
            ),
            capabilities=adapter.capabilities.to_dict() if adapter else None,
            source_identity=adapter.source_identity(method_metadata, distributions) if adapter else None,
            warnings=warnings,
        )

    fallback = _http_url(source.get("access")) or _http_url(source.get("url")) or _http_url(resource.get("url"))
    return AcquisitionPlan(
        dataset_id=canonical_id,
        dataset_name=dataset_name,
        kind="site",
        url=fallback,
        license=license_name,
        instructions={"summary": "Open the source page and complete the provider's manual access workflow.", "command": None},
        warnings=["No machine-readable download route is currently available for this dataset."],
    )
