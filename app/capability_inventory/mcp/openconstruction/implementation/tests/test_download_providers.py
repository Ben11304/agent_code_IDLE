import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from openconstruction_mcp.acquisition import resolve_dataset_download_plan
from openconstruction_mcp.catalog import CatalogClient
from openconstruction_mcp.download_providers import (
    DownloadContext,
    DownloadProvider,
    ProviderCapabilities,
    ProviderRegistry,
    build_default_provider_registry,
)
from openconstruction_mcp.downloads import DownloadManager


def provider_fixture(path):
    if path != "datasets.json":
        return []
    return {
        "multi-direct": {
            "name": "Multi Direct",
            "distribution": [
                {
                    "provider": "example",
                    "content_url": "https://downloads.example.org/one.zip",
                    "filename": "one.zip",
                    "content_size": 5,
                },
                {
                    "provider": "example",
                    "content_url": "https://downloads.example.org/two.zip",
                    "filename": "two.zip",
                    "content_size": 7,
                },
            ],
        },
        "plugin-data": {
            "name": "Plugin Data",
            "license": "CC0-1.0",
            "programmatic_access": [
                {
                    "provider": "example_plugin",
                    "method": "example_archive",
                    "resource_id": "archive-1",
                    "requires_auth": False,
                }
            ],
        },
        "broken-github": {
            "name": "Broken GitHub",
            "programmatic_access": [
                {
                    "provider": "github",
                    "method": "github_clone",
                    "repo_id": "not-a-repository-id",
                    "requires_auth": False,
                }
            ],
        },
        "zenodo-large": {
            "name": "Large Zenodo Record",
            "distribution": [
                {
                    "provider": "zenodo",
                    "content_url": "https://zenodo.org/api/records/12345/files-archive",
                    "filename": "record.zip",
                }
            ],
        },
    }


class ExampleArchiveProvider(DownloadProvider):
    method = "example_archive"
    provider_ids = ("example_plugin",)
    capabilities = ProviderCapabilities(
        access_mode="programmatic",
        executable=True,
        supports_resume=True,
        supports_versions=True,
    )

    def validate(self, metadata, distributions):
        if metadata.get("resource_id") != "archive-1":
            raise ValueError("example_archive requires resource_id=archive-1")

    def execute(self, context: DownloadContext):
        context.target.mkdir(parents=True, exist_ok=context.job.resumed)
        artifact = context.target / "plugin.txt"
        artifact.write_text("provider plugin executed", encoding="utf-8")
        context.job.files.append(
            {
                "path": "plugin.txt",
                "size": artifact.stat().st_size,
                "kind": "example_archive",
                "source": "archive-1",
            }
        )
        context.runtime.persist_provider_job(context.job, force=True)


class ProviderRegistryTest(unittest.TestCase):
    def setUp(self):
        self.catalog = CatalogClient(fetch_json=provider_fixture)

    def test_default_registry_exposes_executable_and_assisted_capabilities(self):
        registry = build_default_provider_registry(load_plugins=False)
        capabilities = registry.public_capabilities()

        self.assertTrue(capabilities["github_clone"]["executable"])
        self.assertTrue(capabilities["http_files"]["supports_multiple_files"])
        self.assertFalse(capabilities["designsafe_globus"]["executable"])
        self.assertEqual(capabilities["designsafe_globus"]["access_mode"], "assisted")
        self.assertTrue(capabilities["zenodo_files"]["supports_multiple_files"])

    def test_multi_file_distributions_resolve_through_direct_adapter(self):
        registry = build_default_provider_registry(load_plugins=False)
        plan = resolve_dataset_download_plan(
            self.catalog,
            "multi-direct",
            provider_registry=registry,
        )

        self.assertEqual(plan.kind, "direct")
        self.assertEqual(plan.method, "direct_download")
        self.assertEqual(plan.estimated_size, 12)
        self.assertEqual(len(plan.distributions), 2)
        self.assertTrue(plan.capabilities["supports_multiple_files"])

    def test_provider_owns_catalog_validation(self):
        registry = build_default_provider_registry(load_plugins=False)
        plan = resolve_dataset_download_plan(
            self.catalog,
            "broken-github",
            provider_registry=registry,
        )

        self.assertFalse(plan.executable_locally)
        self.assertIn("provider configuration is invalid", " ".join(plan.warnings))
        self.assertIn("owner/repository", " ".join(plan.warnings))

    def test_zenodo_archive_route_uses_multi_file_provider(self):
        registry = build_default_provider_registry(load_plugins=False)
        plan = resolve_dataset_download_plan(
            self.catalog,
            "zenodo-large",
            provider_registry=registry,
        )

        self.assertEqual(plan.kind, "direct")
        self.assertEqual(plan.method, "zenodo_files")
        self.assertEqual(
            plan.source_identity,
            {"provider": "zenodo", "record_id": "12345", "revision": "12345"},
        )
        self.assertTrue(plan.executable_locally)
        self.assertIs(registry.for_plan(plan.to_dict()), registry.get("zenodo_files"))

    def test_zenodo_provider_enumerates_files_instead_of_requesting_archive(self):
        registry = build_default_provider_registry(load_plugins=False)
        plan = resolve_dataset_download_plan(
            self.catalog,
            "zenodo-large",
            provider_registry=registry,
        )

        class Runtime:
            def __init__(self):
                self.fetched = []
                self.downloaded = []

            def fetch_public_json(inner_self, url):
                inner_self.fetched.append(url)
                return {
                    "files": [
                        {
                            "key": "province/one.gpkg",
                            "size": 3,
                            "links": {"self": "https://zenodo.org/api/records/12345/files/one/content"},
                        },
                        {
                            "key": "two.gpkg",
                            "size": 4,
                            "links": {"self": "https://zenodo.org/api/records/12345/files/two/content"},
                        },
                    ]
                }

            def download_url(inner_self, job, url, target, **kwargs):
                target.write_bytes(b"x" * kwargs["expected_size"])
                inner_self.downloaded.append((url, target, kwargs["expected_size"]))
                return {"path": target.name, "size": kwargs["expected_size"], "source_url": url}

            def persist_provider_job(inner_self, job, *, force=False):
                return None

            def probe_url_size(inner_self, url):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime()
            job = SimpleNamespace(
                plan=plan.to_dict(),
                related_paper={"download_status": "disabled"},
                files=[],
                bytes_total=None,
                resumed=False,
            )
            registry.get("zenodo_files").execute(
                DownloadContext(job=job, target=Path(tmp) / "zenodo-large", runtime=runtime)
            )

            self.assertEqual(runtime.fetched, ["https://zenodo.org/api/records/12345"])
            self.assertEqual(job.bytes_total, 7)
            self.assertTrue((Path(tmp) / "zenodo-large" / "province" / "one.gpkg").is_file())
            self.assertTrue((Path(tmp) / "zenodo-large" / "two.gpkg").is_file())
            self.assertFalse(any(url.endswith("files-archive") for url, _, _ in runtime.downloaded))

    def test_new_provider_runs_without_download_manager_changes(self):
        registry = build_default_provider_registry(load_plugins=False)
        registry.register(ExampleArchiveProvider())
        plan = resolve_dataset_download_plan(
            self.catalog,
            "plugin-data",
            provider_registry=registry,
        )
        self.assertTrue(plan.executable_locally)
        self.assertEqual(plan.method, "example_archive")

        with tempfile.TemporaryDirectory() as tmp:
            manager = DownloadManager(
                root=tmp,
                provider_registry=registry,
                url_validator=lambda url: None,
            )
            started = manager.start(plan, "plugin-data", True, include_papers=False)
            deadline = time.time() + 3
            result = started
            while time.time() < deadline:
                result = manager.get(started["download_id"])
                if result["status"] in {"completed", "failed", "cancelled", "auth_required"}:
                    break
                time.sleep(0.01)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(
                (Path(tmp) / "plugin-data" / "plugin.txt").read_text(encoding="utf-8"),
                "provider plugin executed",
            )
            self.assertTrue((Path(tmp) / "plugin-data" / ".openconstruction-manifest.json").is_file())

    def test_registry_rejects_accidental_method_override(self):
        registry = ProviderRegistry([ExampleArchiveProvider()])
        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register(ExampleArchiveProvider())


if __name__ == "__main__":
    unittest.main()
