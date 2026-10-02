import io
import unittest

from openconstruction_mcp.catalog import CatalogClient
from openconstruction_mcp.cli import run


def catalog_fixture(path):
    if path != "datasets.json":
        return []
    return {
        "sample-data": {
            "name": "Sample Data",
            "license": "CC BY 4.0",
            "distribution": [
                {
                    "provider": "example",
                    "content_url": "https://downloads.example.org/sample.zip",
                    "filename": "sample.zip",
                    "content_size": 12,
                }
            ],
        }
    }


class RecordingPaperCatalog:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {
            "dataset_id": "sample-data",
            "available": True,
            "availability_status": "available",
            "content_url": "https://downloads.example.org/paper.pdf",
            "content_size": 7,
            "sha256": None,
        }

    def resolve(self, dataset_id):
        self.calls.append(dataset_id)
        return dict(self.result)


class RecordingDownloadManager:
    def __init__(self, final=None):
        self.calls = []
        self.final = final

    def start(self, plan, destination, accept_license, **kwargs):
        self.calls.append(
            {
                "plan": plan,
                "destination": destination,
                "accept_license": accept_license,
                **kwargs,
            }
        )
        if self.final is not None:
            return dict(self.final)
        return {
            "status": "completed",
            "destination": "/tmp/library/sample-data",
            "related_paper": {"download_status": "disabled"},
            "progress_text": "Completed [████████████████████] 100.0% — 12 B / 12 B",
        }

    def get(self, download_id):
        return dict(self.final or {})

    def cancel(self, download_id):
        return {"status": "cancelled", "download_id": download_id}


class LicenseDownloadManager(RecordingDownloadManager):
    def start(self, plan, destination, accept_license, **kwargs):
        raise ValueError(
            "Set accept_license=true only after the user reviews and accepts the dataset license and source terms"
        )


class CliTest(unittest.TestCase):
    def setUp(self):
        self.catalog = CatalogClient(fetch_json=catalog_fixture)

    def run_cli(self, argv, *, paper_catalog=None, manager=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = run(
            argv,
            catalog=self.catalog,
            paper_catalog=paper_catalog or RecordingPaperCatalog(),
            download_manager=manager or RecordingDownloadManager(),
            stdout=stdout,
            stderr=stderr,
            sleep=lambda _: None,
        )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_download_does_not_resolve_or_include_paper_without_flag(self):
        papers = RecordingPaperCatalog()
        manager = RecordingDownloadManager()

        code, output, _ = self.run_cli(
            ["download", "sample-data", "--accept-license"],
            paper_catalog=papers,
            manager=manager,
        )

        self.assertEqual(code, 0)
        self.assertEqual(papers.calls, [])
        self.assertFalse(manager.calls[0]["include_papers"])
        self.assertIsNone(manager.calls[0]["paper_plan"])
        self.assertIn("Related paper: not requested", output)

    def test_paper_flag_resolves_and_forwards_paper_plan(self):
        papers = RecordingPaperCatalog()
        manager = RecordingDownloadManager(
            {
                "status": "completed",
                "destination": "/tmp/research-library/custom-name",
                "related_paper": {
                    "download_status": "completed",
                    "local_path": "papers/paper.pdf",
                },
                "progress_text": "Completed [████████████████████] 100.0% — 19 B / 19 B",
            }
        )

        code, output, _ = self.run_cli(
            [
                "download",
                "sample-data",
                "--paper",
                "--accept-license",
                "--library-dir",
                "/tmp/research-library",
                "--destination",
                "custom-name",
            ],
            paper_catalog=papers,
            manager=manager,
        )

        self.assertEqual(code, 0)
        self.assertEqual(papers.calls, ["sample-data"])
        self.assertTrue(manager.calls[0]["include_papers"])
        self.assertTrue(manager.calls[0]["paper_plan"]["available"])
        self.assertEqual(manager.calls[0]["library_dir"], "/tmp/research-library")
        self.assertEqual(manager.calls[0]["destination"], "custom-name")
        self.assertIn("Paper: /tmp/research-library/custom-name/papers/paper.pdf", output)

    def test_missing_license_acceptance_returns_actionable_error(self):
        code, output, error = self.run_cli(
            ["download", "sample-data", "--paper"],
            manager=LicenseDownloadManager(),
        )

        self.assertEqual(code, 2)
        self.assertIn("License: CC BY 4.0", output)
        self.assertIn("--accept-license", error)

    def test_explicit_paper_transfer_failure_returns_nonzero(self):
        manager = RecordingDownloadManager(
            {
                "status": "completed",
                "destination": "/tmp/library/sample-data",
                "related_paper": {
                    "download_status": "failed",
                    "error": "paper mirror unavailable",
                },
                "progress_text": "Completed [████████████████████] 100.0% — 12 B / 12 B",
            }
        )

        code, output, _ = self.run_cli(
            ["download", "sample-data", "--paper", "--accept-license"],
            manager=manager,
        )

        self.assertEqual(code, 1)
        self.assertIn("Paper: download failed", output)


if __name__ == "__main__":
    unittest.main()
