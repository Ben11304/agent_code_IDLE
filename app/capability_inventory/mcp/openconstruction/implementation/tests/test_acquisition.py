import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from openconstruction_mcp.acquisition import AcquisitionPlan, resolve_dataset_download_plan
from openconstruction_mcp.catalog import CatalogClient
from openconstruction_mcp.downloads import DownloadJob, DownloadManager, validate_public_download_url
from openconstruction_mcp.provider_auth import provider_auth_status
from openconstruction_mcp.research import find_research_bundle, find_research_bundle_for_dataset


def fixture(path):
    if path != "datasets.json":
        return []
    return {
        "direct-data": {
            "name": "Direct Data",
            "license": "CC BY 4.0",
            "distribution": [
                {
                    "provider": "example",
                    "content_url": "https://downloads.example.org/direct.zip",
                    "filename": "direct.zip",
                    "content_size": 12,
                }
            ],
        },
        "github-data": {
            "name": "GitHub Data",
            "license": "MIT",
            "programmatic_access": [
                {
                    "provider": "github",
                    "method": "github_clone",
                    "repo_id": "open-construction/example",
                    "scope": "full",
                    "requires_auth": False,
                }
            ],
        },
        "globus-data": {
            "name": "Globus Data",
            "license": "custom",
            "programmatic_access": [
                {
                    "provider": "designsafe",
                    "method": "designsafe_globus",
                    "scope": "full",
                    "requires_auth": True,
                }
            ],
        },
        "manual-data": {
            "name": "Manual Data",
            "license": "custom",
            "access": "https://example.org/manual",
        },
    }


class AcquisitionPlanTest(unittest.TestCase):
    def setUp(self):
        self.catalog = CatalogClient(fetch_json=fixture)

    def test_resolves_direct_download(self):
        plan = resolve_dataset_download_plan(self.catalog, "DIRECT-DATA")
        self.assertEqual(plan.dataset_id, "direct-data")
        self.assertEqual(plan.kind, "direct")
        self.assertTrue(plan.executable_locally)
        self.assertEqual(plan.filename, "direct.zip")
        self.assertEqual(plan.estimated_size, 12)

    def test_resolves_automated_and_assisted_programmatic_routes(self):
        github = resolve_dataset_download_plan(self.catalog, "github-data")
        self.assertEqual(github.method, "github_clone")
        self.assertTrue(github.executable_locally)
        self.assertIn("git clone", github.instructions["command"])

        globus = resolve_dataset_download_plan(self.catalog, "globus-data")
        self.assertFalse(globus.executable_locally)
        self.assertTrue(globus.requires_auth)
        self.assertIn("Globus", globus.instructions["summary"])
        self.assertEqual(globus.auth["status"], "auth_required")
        self.assertEqual(globus.auth["auth_mode"], "local_provider")
        self.assertIsNone(globus.auth["credential_detected"])

    def test_resolves_manual_route_and_rejects_unknown_id(self):
        manual = resolve_dataset_download_plan(self.catalog, "manual-data")
        self.assertEqual(manual.kind, "site")
        self.assertFalse(manual.executable_locally)
        with self.assertRaisesRegex(ValueError, "Unknown dataset id"):
            resolve_dataset_download_plan(self.catalog, "missing")


class FakeResponse:
    def __init__(self, payload):
        self.payload = io.BytesIO(payload)
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size=-1):
        return self.payload.read(size)


class FakeOpener:
    def __init__(self, payload):
        self.payload = payload

    def open(self, request, timeout=30):
        return FakeResponse(self.payload)


class RefusingOpener:
    def open(self, request, timeout=30):
        raise AssertionError("A verified completed file should not be downloaded again")


class InterruptingResponse:
    status = 200

    def __init__(self):
        self.headers = {"Content-Length": "12", "ETag": '"version-1"'}
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, size=-1):
        self.calls += 1
        if self.calls == 1:
            return b"first-"
        raise ConnectionResetError("simulated interrupted connection")


class InterruptingOpener:
    def open(self, request, timeout=30):
        return InterruptingResponse()


class ResumeResponse(FakeResponse):
    status = 206

    def __init__(self):
        super().__init__(b"second")
        self.headers.update(
            {
                "Content-Range": "bytes 6-11/12",
                "ETag": '"version-1"',
            }
        )


class ResumeOpener:
    def __init__(self):
        self.requests = []

    def open(self, request, timeout=30):
        self.requests.append(request)
        return ResumeResponse()


class ResearchInterruptOpener:
    def open(self, request, timeout=30):
        if request.full_url.endswith("/paper.pdf"):
            return InterruptingResponse()
        return FakeResponse(b"test-payload")


class ResearchResumeOpener(ResumeOpener):
    def open(self, request, timeout=30):
        if not request.full_url.endswith("/paper.pdf"):
            raise AssertionError("Completed dataset file should not be downloaded again")
        return super().open(request, timeout)


class BlockingResponse(FakeResponse):
    status = 200

    def __init__(self, started, release):
        super().__init__(b"test-payload")
        self.started = started
        self.release = release
        self.first_read = True

    def read(self, size=-1):
        if self.first_read:
            self.first_read = False
            self.started.set()
            self.release.wait(timeout=5)
        return super().read(size)


class BlockingOpener:
    def __init__(self, started, release):
        self.started = started
        self.release = release

    def open(self, request, timeout=30):
        return BlockingResponse(self.started, self.release)


class ScriptedDownloadManager(DownloadManager):
    def __init__(self, *args, opener, **kwargs):
        super().__init__(*args, **kwargs)
        self.test_opener = opener

    def _opener(self):
        return self.test_opener


class FakeDownloadManager(DownloadManager):
    def _opener(self):
        return FakeOpener(b"test-payload")


class PaperFailureOpener:
    def open(self, request, timeout=30):
        if request.full_url.endswith("/paper.pdf"):
            raise urllib.error.URLError("paper source unavailable")
        return FakeResponse(b"test-payload")


class PaperFailureDownloadManager(DownloadManager):
    def _opener(self):
        return PaperFailureOpener()


class AuthFailureOpener:
    def open(self, request, timeout=30):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)


class AuthFailureDownloadManager(DownloadManager):
    def _opener(self):
        return AuthFailureOpener()


class NoopDownloadManager(DownloadManager):
    def _run(self, job):
        job.status = "cancelled"
        job.completed_at = time.time()
        self._persist_job(job, force=True)
        self._release_job_lock(job)


class DownloadManagerTest(unittest.TestCase):
    def test_reports_agent_ready_progress_fields(self):
        job = DownloadJob(
            id="dl_progress",
            dataset_id="progress-data",
            destination="/tmp/progress-data",
            plan={},
            status="downloading",
            bytes_downloaded=50,
            bytes_total=100,
            created_at=99.0,
            started_at=100.0,
        )
        with patch("openconstruction_mcp.downloads.time.time", return_value=102.0):
            result = job.public_dict()

        self.assertEqual(result["progress_percent"], 50.0)
        self.assertEqual(result["progress_bar"], "[██████████░░░░░░░░░░]")
        self.assertFalse(result["progress_indeterminate"])
        self.assertEqual(result["speed_bytes_per_second"], 25)
        self.assertEqual(result["eta_seconds"], 2)
        self.assertIn("50.0%", result["progress_text"])

    def test_reports_indeterminate_progress_without_declared_size(self):
        job = DownloadJob(
            id="dl_unknown",
            dataset_id="unknown-data",
            destination="/tmp/unknown-data",
            plan={},
            status="downloading",
            bytes_downloaded=2048,
            bytes_total=None,
            started_at=100.0,
        )
        with patch("openconstruction_mcp.downloads.time.time", return_value=102.0):
            result = job.public_dict()

        self.assertIsNone(result["progress_percent"])
        self.assertTrue(result["progress_indeterminate"])
        self.assertEqual(result["progress_bar"], "[????????????????????]")
        self.assertIn("total size unknown", result["progress_text"])

    def direct_plan(self):
        return AcquisitionPlan(
            dataset_id="direct-data",
            dataset_name="Direct Data",
            kind="direct",
            provider="example",
            method="navigate",
            url="https://downloads.example.org/direct.zip",
            filename="direct.zip",
            license="CC BY 4.0",
            executable_locally=True,
            distributions=[
                {
                    "content_url": "https://downloads.example.org/direct.zip",
                    "filename": "direct.zip",
                }
            ],
        )

    def resumable_plan(self, url="https://downloads.example.org/resumable.bin"):
        return AcquisitionPlan(
            dataset_id="resumable-data",
            dataset_name="Resumable Data",
            kind="direct",
            provider="example",
            method="navigate",
            url=url,
            filename="resumable.bin",
            license="CC BY 4.0",
            executable_locally=True,
            estimated_size=12,
            distributions=[
                {
                    "content_url": url,
                    "filename": "resumable.bin",
                    "content_size": 12,
                }
            ],
        )

    def zenodo_plan(self, method="zenodo_files", source_identity=True):
        record_id = "18688062"
        url = f"https://zenodo.org/api/records/{record_id}/files-archive"
        return AcquisitionPlan(
            dataset_id="GeoLink-UV",
            dataset_name="GeoLink UV",
            kind="direct",
            provider="zenodo",
            method=method,
            url=url,
            filename="GeoLink-UV.zip",
            license="CC BY 4.0",
            executable_locally=True,
            distributions=[{"provider": "zenodo", "content_url": url, "filename": "GeoLink-UV.zip"}],
            source_identity=(
                {"provider": "zenodo", "record_id": record_id, "revision": record_id}
                if source_identity
                else None
            ),
        )

    def write_legacy_failed_checkpoint(self, manager, target, *, with_payload=False):
        old_plan = self.zenodo_plan(method="navigate", source_identity=False)
        target.mkdir(parents=True)
        current_file = {
            "path": "GeoLink-UV.zip",
            "partial_path": "GeoLink-UV.zip.part",
            "source_url": old_plan.url,
            "bytes_downloaded": 0,
        }
        bytes_downloaded = 0
        if with_payload:
            partial = target / "GeoLink-UV.zip.part"
            partial.write_bytes(b"x")
            current_file["bytes_downloaded"] = 1
            bytes_downloaded = 1
        job = DownloadJob(
            id="dl_0000000000000000",
            dataset_id=old_plan.dataset_id,
            destination=str(target),
            plan=old_plan.to_dict(),
            library_dir=str(target.parent),
            status="failed",
            bytes_downloaded=bytes_downloaded,
            include_papers=False,
            related_paper={"dataset_id": old_plan.dataset_id, "download_status": "disabled"},
            source_fingerprint=manager.execution_fingerprint(
                old_plan,
                include_papers=False,
                paper_plan=None,
            ),
            current_file=current_file,
            license_accepted_at=100,
        )
        manager._persist_job(job, force=True)
        state = manager._read_json(manager._state_path(target))
        state.pop("execution_fingerprint", None)
        state.pop("plan", None)
        state["job"].pop("execution_fingerprint", None)
        manager._atomic_json(manager._state_path(target), state)

    def wait(self, manager, download_id):
        for _ in range(100):
            result = manager.get(download_id)
            if result["status"] in {"completed", "failed", "cancelled", "auth_required"}:
                return result
            time.sleep(0.01)
        self.fail("download job did not finish")

    def test_downloads_direct_file_and_writes_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = FakeDownloadManager(root=tmp, url_validator=lambda url: None)
            started = manager.start(self.direct_plan(), "direct-data", accept_license=True)
            result = self.wait(manager, started["download_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["bytes_total"], 12)
            self.assertEqual(result["progress_percent"], 100.0)
            self.assertEqual(result["eta_seconds"], 0)
            self.assertIn("100.0%", result["progress_text"])
            target = Path(tmp) / "direct-data"
            self.assertEqual((target / "direct.zip").read_bytes(), b"test-payload")
            manifest = json.loads((target / ".openconstruction-manifest.json").read_text())
            self.assertEqual(manifest["dataset_id"], "direct-data")
            self.assertEqual(manifest["files"][0]["size"], 12)

    def test_reuses_verified_completed_file_after_manager_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = FakeDownloadManager(root=tmp, url_validator=lambda url: None)
            started = first.start(self.direct_plan(), "direct-data", accept_license=True)
            original = self.wait(first, started["download_id"])

            second = ScriptedDownloadManager(
                root=tmp,
                url_validator=lambda url: None,
                opener=RefusingOpener(),
            )
            reused = second.start(self.direct_plan(), "direct-data", accept_license=False)

            self.assertEqual(reused["download_id"], original["download_id"])
            self.assertEqual(reused["status"], "already_installed")
            self.assertTrue(reused["installed"])
            self.assertEqual(reused["progress_percent"], 100.0)
            self.assertEqual((Path(tmp) / "direct-data" / "direct.zip").read_bytes(), b"test-payload")

    def test_installs_into_custom_library_and_reports_existing_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            default_root = Path(tmp) / "default-library"
            custom_root = Path(tmp) / "research-datasets"
            manager = FakeDownloadManager(root=default_root, url_validator=lambda url: None)
            started = manager.start(
                self.direct_plan(),
                None,
                accept_license=True,
                library_dir=str(custom_root),
            )
            completed = self.wait(manager, started["download_id"])

            self.assertEqual(completed["status"], "completed")
            self.assertEqual(Path(completed["destination"]), custom_root / "direct-data")
            self.assertEqual(Path(completed["library_dir"]), custom_root)
            self.assertTrue((custom_root / "direct-data" / "direct.zip").is_file())
            self.assertTrue(any((default_root / ".openconstruction-state" / "jobs").glob("direct-data-*.json")))

            inspection = manager.inspect_installation(
                self.direct_plan(),
                library_dir=str(custom_root),
            )
            self.assertEqual(inspection["status"], "installed")
            self.assertTrue(inspection["installed"])
            self.assertEqual(inspection["bytes_present"], 12)

    def test_installation_inspection_finds_resumable_partial(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = ScriptedDownloadManager(
                root=tmp,
                url_validator=lambda url: None,
                opener=InterruptingOpener(),
            )
            started = first.start(self.resumable_plan(), "partial-data", accept_license=True)
            failed = self.wait(first, started["download_id"])
            self.assertEqual(failed["status"], "failed")

            second = ScriptedDownloadManager(
                root=tmp,
                url_validator=lambda url: None,
                opener=ResumeOpener(),
            )
            inspection = second.inspect_installation(
                self.resumable_plan(),
                "partial-data",
            )
            self.assertEqual(inspection["status"], "partial")
            self.assertTrue(inspection["resume_available"])
            self.assertEqual(inspection["download_id"], started["download_id"])
            self.assertEqual(inspection["bytes_present"], 6)

    def test_resumes_http_partial_after_manager_restart_and_rejects_changed_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = ScriptedDownloadManager(
                root=tmp,
                url_validator=lambda url: None,
                opener=InterruptingOpener(),
            )
            started = first.start(self.resumable_plan(), "resumable-data", accept_license=True)
            interrupted = self.wait(first, started["download_id"])

            target = Path(tmp) / "resumable-data"
            partial = target / "resumable.bin.part"
            self.assertEqual(interrupted["status"], "failed")
            self.assertEqual(partial.read_bytes(), b"first-")
            self.assertTrue(interrupted["resume_available"])

            changed = ScriptedDownloadManager(
                root=tmp,
                url_validator=lambda url: None,
                opener=ResumeOpener(),
            ).start(
                self.resumable_plan("https://downloads.example.org/replaced.bin"),
                "resumable-data",
                accept_license=True,
            )
            self.assertEqual(changed["status"], "source_changed")
            self.assertEqual(partial.read_bytes(), b"first-")

            resume_opener = ResumeOpener()
            second = ScriptedDownloadManager(
                root=tmp,
                url_validator=lambda url: None,
                opener=resume_opener,
            )
            resumed = second.start(
                self.resumable_plan(),
                "resumable-data",
                accept_license=False,
            )
            completed = self.wait(second, resumed["download_id"])

            self.assertEqual(resumed["download_id"], started["download_id"])
            self.assertTrue(resumed["resumed"])
            self.assertEqual(completed["status"], "completed")
            self.assertEqual((target / "resumable.bin").read_bytes(), b"first-second")
            self.assertFalse(partial.exists())
            self.assertEqual(resume_opener.requests[0].get_header("Range"), "bytes=6-")
            self.assertEqual(resume_opener.requests[0].get_header("If-range"), '"version-1"')
            manifest = json.loads((target / ".openconstruction-manifest.json").read_text())
            self.assertEqual(manifest["source_fingerprint"], completed["source_fingerprint"])

    def test_stable_provider_identity_ignores_download_strategy_changes(self):
        manager = DownloadManager(root="/tmp/oc-fingerprint-test", url_validator=lambda url: None)
        first = self.zenodo_plan(method="navigate")
        second = self.zenodo_plan(method="zenodo_files")

        first_source = manager.source_fingerprint(first, include_papers=False, paper_plan=None)
        second_source = manager.source_fingerprint(second, include_papers=False, paper_plan=None)
        first_execution = manager.execution_fingerprint(first, include_papers=False, paper_plan=None)
        second_execution = manager.execution_fingerprint(second, include_papers=False, paper_plan=None)

        self.assertEqual(first_source, second_source)
        self.assertNotEqual(first_execution, second_execution)

    def test_resets_empty_failed_checkpoint_after_strategy_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = NoopDownloadManager(root=tmp, url_validator=lambda url: None)
            target = Path(tmp) / "GeoLink-UV"
            self.write_legacy_failed_checkpoint(manager, target)

            started = manager.start(
                self.zenodo_plan(),
                None,
                accept_license=False,
                include_papers=False,
            )

            self.assertNotEqual(started["status"], "source_changed")
            self.assertFalse(started["resumed"])
            self.assertIn("reset an empty failed checkpoint", " ".join(started["warnings"]))
            self.assertEqual(started["license_accepted_at"], 100)

    def test_strategy_upgrade_never_discards_nonempty_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = NoopDownloadManager(root=tmp, url_validator=lambda url: None)
            target = Path(tmp) / "GeoLink-UV"
            self.write_legacy_failed_checkpoint(manager, target, with_payload=True)

            result = manager.start(
                self.zenodo_plan(),
                None,
                accept_license=True,
                include_papers=False,
            )

            self.assertEqual(result["status"], "source_changed")
            self.assertEqual((target / "GeoLink-UV.zip.part").read_bytes(), b"x")

    def test_returns_same_checkpoint_when_another_manager_is_already_downloading(self):
        with tempfile.TemporaryDirectory() as tmp:
            started_event = threading.Event()
            release_event = threading.Event()
            first = ScriptedDownloadManager(
                root=tmp,
                url_validator=lambda url: None,
                opener=BlockingOpener(started_event, release_event),
            )
            started = first.start(self.direct_plan(), "shared-job", accept_license=True)
            self.assertTrue(started_event.wait(timeout=2))

            second = ScriptedDownloadManager(
                root=tmp,
                url_validator=lambda url: None,
                opener=FakeOpener(b"test-payload"),
            )
            duplicate = second.start(self.direct_plan(), "shared-job", accept_license=False)

            self.assertTrue(duplicate["already_running"])
            self.assertEqual(duplicate["download_id"], started["download_id"])
            release_event.set()
            completed = self.wait(first, started["download_id"])
            self.assertEqual(completed["status"], "completed")

    def test_includes_related_paper_by_default(self):
        paper_plan = {
            "dataset_id": "direct-data",
            "available": True,
            "availability_status": "available",
            "access_mode": "oc_mirror",
            "paper_title": "Direct Data Paper",
            "content_url": "https://oc.example/papers/direct-data/paper.pdf",
            "content_size": 12,
            "sha256": None,
            "redistribution_status": "unreviewed",
        }
        with tempfile.TemporaryDirectory() as tmp:
            manager = FakeDownloadManager(root=tmp, url_validator=lambda url: None)
            started = manager.start(
                self.direct_plan(),
                "with-paper",
                accept_license=True,
                paper_plan=paper_plan,
            )
            result = self.wait(manager, started["download_id"])

            self.assertEqual(result["status"], "completed")
            self.assertTrue(result["include_papers"])
            self.assertEqual(result["related_paper"]["download_status"], "completed")
            self.assertEqual(result["related_paper"]["local_path"], "papers/paper.pdf")
            target = Path(tmp) / "with-paper"
            self.assertEqual((target / "papers" / "paper.pdf").read_bytes(), b"test-payload")
            manifest = json.loads((target / ".openconstruction-manifest.json").read_text())
            self.assertTrue(manifest["include_papers"])
            self.assertEqual(manifest["related_papers"][0]["download_status"], "completed")
            self.assertIn("papers/paper.pdf", [item["path"] for item in manifest["files"]])

    def test_prepares_research_bundle_without_extracting_pdf(self):
        paper_plan = {
            "dataset_id": "direct-data",
            "available": True,
            "availability_status": "available",
            "access_mode": "oc_mirror",
            "paper_title": "Direct Data Paper",
            "paper_authors": ["A. Researcher"],
            "doi": "10.0000/direct-data",
            "content_url": "https://oc.example/papers/direct-data/paper.pdf",
            "content_size": 12,
            "sha256": None,
            "paper_license": "CC BY 4.0",
            "paper_license_url": "https://creativecommons.org/licenses/by/4.0/",
            "redistribution_status": "verified_redistributable",
        }
        with tempfile.TemporaryDirectory() as tmp:
            manager = FakeDownloadManager(root=tmp, url_validator=lambda url: None)
            started = manager.start(
                self.direct_plan(),
                "research-data",
                accept_license=True,
                paper_plan=paper_plan,
                create_bundle=True,
            )
            result = self.wait(manager, started["download_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["workflow"], "research_preparation")
            self.assertTrue(result["download_id"].startswith("rp_"))
            self.assertEqual(result["research_bundle"]["status"], "ready")
            target = Path(tmp) / "research-data"
            bundle = json.loads((target / "research" / "bundle.json").read_text())
            self.assertTrue(bundle["document_handling"]["original_pdfs_preserved"])
            self.assertFalse(bundle["document_handling"]["pdf_to_markdown"])
            self.assertFalse(bundle["document_handling"]["content_extraction"])
            self.assertEqual(bundle["papers"][0]["path"], "../papers/paper.pdf")
            self.assertEqual((target / "papers" / "paper.pdf").read_bytes(), b"test-payload")
            self.assertFalse(any(path.suffix == ".md" for path in (target / "papers").rglob("*")))

            persisted = find_research_bundle(Path(tmp), result["download_id"])
            self.assertEqual(persisted["bundle_id"], result["research_bundle"]["bundle_id"])
            self.assertEqual(persisted["paper_paths"], [str(target / "papers" / "paper.pdf")])
            self.assertEqual(persisted["source_fingerprint"], result["source_fingerprint"])
            self.assertIsNotNone(
                find_research_bundle_for_dataset(Path(tmp), "direct-data", result["source_fingerprint"])
            )
            self.assertIsNone(find_research_bundle_for_dataset(Path(tmp), "direct-data", "0" * 64))

    def test_resumes_related_paper_before_completing_research_bundle(self):
        paper_plan = {
            "dataset_id": "direct-data",
            "available": True,
            "paper_title": "Direct Data Paper",
            "content_url": "https://oc.example/papers/direct-data/paper.pdf",
            "content_size": 12,
            "sha256": None,
            "paper_license": "CC BY 4.0",
            "redistribution_status": "verified_redistributable",
        }
        with tempfile.TemporaryDirectory() as tmp:
            first = ScriptedDownloadManager(
                root=tmp,
                url_validator=lambda url: None,
                opener=ResearchInterruptOpener(),
            )
            started = first.start(
                self.direct_plan(),
                "paper-resume",
                accept_license=True,
                paper_plan=paper_plan,
                create_bundle=True,
            )
            interrupted = self.wait(first, started["download_id"])
            target = Path(tmp) / "paper-resume"

            self.assertEqual(interrupted["status"], "failed")
            self.assertTrue((target / "direct.zip").is_file())
            self.assertEqual((target / "papers" / "paper.pdf.part").read_bytes(), b"first-")
            self.assertFalse((target / "research" / "bundle.json").exists())

            resume_opener = ResearchResumeOpener()
            second = ScriptedDownloadManager(
                root=tmp,
                url_validator=lambda url: None,
                opener=resume_opener,
            )
            resumed = second.start(
                self.direct_plan(),
                "paper-resume",
                accept_license=False,
                paper_plan=paper_plan,
                create_bundle=True,
            )
            completed = self.wait(second, resumed["download_id"])

            self.assertEqual(completed["status"], "completed")
            self.assertTrue(completed["resumed"])
            self.assertEqual((target / "papers" / "paper.pdf").read_bytes(), b"first-second")
            self.assertTrue((target / "research" / "bundle.json").is_file())
            self.assertEqual(resume_opener.requests[0].get_header("Range"), "bytes=6-")

    def test_paper_failure_does_not_fail_dataset(self):
        paper_plan = {
            "dataset_id": "direct-data",
            "available": True,
            "content_url": "https://oc.example/papers/direct-data/paper.pdf",
            "content_size": 12,
            "redistribution_status": "unreviewed",
        }
        with tempfile.TemporaryDirectory() as tmp:
            manager = PaperFailureDownloadManager(root=tmp, url_validator=lambda url: None)
            started = manager.start(
                self.direct_plan(),
                "paper-failure",
                accept_license=True,
                paper_plan=paper_plan,
            )
            result = self.wait(manager, started["download_id"])

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["related_paper"]["download_status"], "failed")
            self.assertIn("paper source unavailable", result["related_paper"]["error"])
            self.assertTrue(result["warnings"])
            self.assertTrue((Path(tmp) / "paper-failure" / "direct.zip").is_file())

    def test_requires_license_acceptance_and_sandboxed_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = FakeDownloadManager(root=tmp, url_validator=lambda url: None)
            with self.assertRaisesRegex(ValueError, "accept_license=true"):
                manager.start(self.direct_plan(), None, accept_license=False)
            with self.assertRaisesRegex(ValueError, "inside OC_DOWNLOAD_ROOT"):
                manager.start(self.direct_plan(), "../escape", accept_license=True)
            with self.assertRaisesRegex(ValueError, "reserved state directory"):
                manager.start(self.direct_plan(), ".openconstruction-state", accept_license=True)

    def test_assisted_plan_returns_instructions_without_starting_job(self):
        plan = AcquisitionPlan(
            dataset_id="globus-data",
            dataset_name="Globus Data",
            kind="programmatic",
            method="designsafe_globus",
            executable_locally=False,
            instructions={"summary": "Authenticate with Globus."},
        )
        with tempfile.TemporaryDirectory() as tmp:
            manager = FakeDownloadManager(root=tmp, url_validator=lambda url: None)
            result = manager.start(plan, None, accept_license=False)
            self.assertEqual(result["status"], "instructions_required")

    def test_rejects_private_network_urls_and_guides_missing_provider_auth(self):
        with self.assertRaisesRegex(ValueError, "non-public"):
            validate_public_download_url("http://127.0.0.1/private")

        plan = AcquisitionPlan(
            dataset_id="private-hf",
            dataset_name="Private HF",
            kind="programmatic",
            method="huggingface_snapshot",
            requires_auth=True,
            executable_locally=True,
        )
        with tempfile.TemporaryDirectory() as tmp, patch(
            "openconstruction_mcp.provider_auth._credential_detected", return_value=False
        ):
            manager = FakeDownloadManager(root=tmp, url_validator=lambda url: None)
            result = manager.start(plan, None, accept_license=True)

        self.assertEqual(result["status"], "auth_required")
        self.assertEqual(result["auth"]["auth_mode"], "local_provider")
        self.assertEqual(result["auth"]["provider"], "Hugging Face")
        self.assertIn("hf auth login", [step["command"] for step in result["auth"]["instructions"]])
        self.assertEqual(result["retry"]["tool"], "download_dataset")
        self.assertNotIn("hf_private-value", json.dumps(result))

    def test_detects_huggingface_cli_credential_without_exposing_it(self):
        with patch("huggingface_hub.get_token", return_value="hf_private-value"):
            result = provider_auth_status(
                "huggingface",
                "huggingface_snapshot",
                required=True,
            )

        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["credential_detected"])
        self.assertNotIn("hf_private-value", json.dumps(result))

    def test_runtime_http_auth_failure_becomes_safe_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = AuthFailureDownloadManager(root=tmp, url_validator=lambda url: None)
            started = manager.start(self.direct_plan(), "protected-data", accept_license=True)
            result = self.wait(manager, started["download_id"])

        self.assertEqual(result["status"], "auth_required")
        self.assertIn("local terminal", result["auth"]["security_notice"])
        self.assertEqual(result["retry"]["tool"], "download_dataset")
        self.assertNotIn("Unauthorized", result["error"])


if __name__ == "__main__":
    unittest.main()
