import unittest

from openconstruction_mcp.papers import DEFAULT_PAPER_CONTENT_BASE_URL, PaperCatalogClient


PAPER_HASH = "a" * 64


def fixture(_url):
    return {
        "schema_version": "1.0.0",
        "papers": {
            "CEQuest": {
                "paper_title": "CEQuest Paper",
                "doi": "10.0000/cequest",
                "source_url": "file:///private/source.pdf",
                "redistribution_status": "verified_redistributable",
                "license": "CC BY 4.0",
                "license_url": "https://creativecommons.org/licenses/by/4.0/",
                "rights_evidence": "The published PDF identifies this article as CC BY 4.0.",
                "rights_verified_at": "2026-08-23",
                "authors": ["A. Researcher", "B. Builder"],
                "status": "available_local",
                "path": "papers/CEQuest/paper.pdf",
                "sha256": PAPER_HASH,
                "bytes": 1234,
                "pages": 12,
            },
            "missing-paper": {
                "paper_title": "Missing Paper",
                "status": "unresolved",
                "path": None,
                "redistribution_status": "unreviewed",
            },
        },
    }


class PaperCatalogTest(unittest.TestCase):
    def setUp(self):
        self.client = PaperCatalogClient(
            manifest_url="https://oc.example/papers/manifest.json",
            content_base_url="https://oc.example/catalog",
            fetch_json=fixture,
        )

    def test_resolves_oc_mirror_plan_and_hides_local_source_path(self):
        plan = self.client.resolve("cequest")

        self.assertTrue(plan["available"])
        self.assertEqual(plan["access_mode"], "oc_mirror")
        self.assertEqual(plan["content_url"], "https://oc.example/catalog/papers/CEQuest/paper.pdf")
        self.assertEqual(plan["content_size"], 1234)
        self.assertEqual(plan["sha256"], PAPER_HASH)
        self.assertEqual(plan["redistribution_status"], "verified_redistributable")
        self.assertEqual(plan["paper_license"], "CC BY 4.0")
        self.assertEqual(plan["paper_authors"], ["A. Researcher", "B. Builder"])
        self.assertEqual(plan["paper_license_url"], "https://creativecommons.org/licenses/by/4.0/")
        self.assertEqual(plan["rights_verified_at"], "2026-08-23")
        self.assertIsNone(plan["warning"])
        self.assertIsNone(plan["original_source_url"])

    def test_default_content_source_resolves_github_lfs_objects(self):
        self.assertEqual(
            DEFAULT_PAPER_CONTENT_BASE_URL,
            "https://media.githubusercontent.com/media/Ben11304/OC-clone/main/open-construction-data",
        )

    def test_unresolved_and_manifest_failures_are_non_blocking(self):
        unresolved = self.client.resolve("missing-paper")
        self.assertFalse(unresolved["available"])
        self.assertEqual(unresolved["access_mode"], "metadata_only")

        failed = PaperCatalogClient(
            manifest_url="https://oc.example/papers/manifest.json",
            fetch_json=lambda _url: (_ for _ in ()).throw(OSError("offline")),
        ).resolve("CEQuest")
        self.assertFalse(failed["available"])
        self.assertEqual(failed["availability_status"], "manifest_unavailable")
        self.assertIn("offline", failed["error"])

    def test_rejects_manifest_path_for_another_dataset(self):
        client = PaperCatalogClient(
            fetch_json=lambda _url: {
                "papers": {
                    "CEQuest": {
                        "status": "available_local",
                        "path": "papers/another-dataset/paper.pdf",
                    }
                }
            }
        )
        plan = client.resolve("CEQuest")
        self.assertFalse(plan["available"])
        self.assertIsNone(plan["content_url"])


if __name__ == "__main__":
    unittest.main()
