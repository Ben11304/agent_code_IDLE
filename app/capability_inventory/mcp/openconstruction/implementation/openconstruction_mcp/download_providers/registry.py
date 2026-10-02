from __future__ import annotations

import re
from functools import lru_cache
from importlib import metadata as importlib_metadata
from typing import Any, Iterable, Mapping

from .base import DownloadProvider


ENTRY_POINT_GROUP = "openconstruction.download_providers"
METHOD_PATTERN = re.compile(r"[a-z][a-z0-9_]{1,79}")


class ProviderRegistry:
    """Maps catalog methods to adapters without coupling callers to providers."""

    def __init__(self, providers: Iterable[DownloadProvider] | None = None) -> None:
        self._providers: dict[str, DownloadProvider] = {}
        self._distribution_providers: dict[str, DownloadProvider] = {}
        self.discovery_errors: list[str] = []
        for provider in providers or ():
            self.register(provider)

    def register(self, provider: DownloadProvider, *, replace: bool = False) -> None:
        method = str(getattr(provider, "method", "") or "").strip().casefold()
        if not METHOD_PATTERN.fullmatch(method):
            raise ValueError(f"Invalid provider method: {method or '<empty>'}")
        if method in self._providers and not replace:
            raise ValueError(f"Provider method is already registered: {method}")
        self._providers[method] = provider
        if provider.handles_distributions:
            for provider_id in provider.provider_ids:
                key = str(provider_id or "").strip().casefold()
                existing = self._distribution_providers.get(key)
                if existing and existing is not provider and not replace:
                    raise ValueError(f"Distribution provider is already registered: {key}")
                self._distribution_providers[key] = provider

    def get(self, method: str | None) -> DownloadProvider | None:
        return self._providers.get(str(method or "").strip().casefold())

    def for_plan(self, plan: Mapping[str, Any]) -> DownloadProvider | None:
        if plan.get("kind") == "direct":
            specialized = self.get(str(plan.get("method") or ""))
            if specialized and specialized.handles_distributions:
                return specialized
            return self.get("direct_download")
        key = plan.get("method")
        return self.get(str(key or ""))

    def for_distributions(self, distributions: Iterable[Mapping[str, Any]]) -> DownloadProvider | None:
        provider_ids = {
            str(item.get("provider") or "").strip().casefold()
            for item in distributions
            if str(item.get("provider") or "").strip()
        }
        if len(provider_ids) != 1:
            return None
        return self._distribution_providers.get(next(iter(provider_ids)))

    def methods(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def public_capabilities(self) -> dict[str, dict[str, Any]]:
        return {
            method: {
                "method": method,
                "provider_ids": list(provider.provider_ids),
                **provider.capabilities.to_dict(),
            }
            for method, provider in sorted(self._providers.items())
        }

    def discover(self) -> None:
        try:
            entries = importlib_metadata.entry_points()
            selected = (
                entries.select(group=ENTRY_POINT_GROUP)
                if hasattr(entries, "select")
                else entries.get(ENTRY_POINT_GROUP, [])
            )
        except Exception as exc:  # pragma: no cover - environment metadata failure.
            self.discovery_errors.append(f"Could not inspect provider plugins: {exc}")
            return
        for entry in selected:
            try:
                loaded = entry.load()
                provider = loaded() if isinstance(loaded, type) else loaded
                if callable(provider) and not isinstance(provider, DownloadProvider):
                    provider = provider()
                if not isinstance(provider, DownloadProvider):
                    raise TypeError("entry point did not return a DownloadProvider")
                self.register(provider)
            except Exception as exc:  # pragma: no cover - third-party plugin failure.
                self.discovery_errors.append(f"{entry.name}: {exc}")


def build_default_provider_registry(*, load_plugins: bool = True) -> ProviderRegistry:
    from .builtin import builtin_providers

    registry = ProviderRegistry(builtin_providers())
    if load_plugins:
        registry.discover()
    return registry


@lru_cache(maxsize=1)
def get_default_provider_registry() -> ProviderRegistry:
    return build_default_provider_registry()


def register_provider(provider: DownloadProvider, *, replace: bool = False) -> None:
    """Register an adapter for the running process (use entry points for packages)."""

    get_default_provider_registry().register(provider, replace=replace)
