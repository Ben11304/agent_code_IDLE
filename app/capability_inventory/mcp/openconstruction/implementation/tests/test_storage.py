import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openconstruction_mcp.storage import DatasetStore


class DatasetStoreTest(unittest.TestCase):
    def test_default_library_uses_oc_home(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"OC_HOME": tmp, "OC_DATASETS_DIR": "", "OC_DOWNLOAD_ROOT": ""},
        ):
            self.assertEqual(DatasetStore().root, Path(tmp) / "datasets")

    def test_resolves_default_and_custom_library_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DatasetStore(Path(tmp) / "default")
            self.assertEqual(store.resolve_target("My Dataset", None), Path(tmp) / "default" / "My-Dataset")

            custom = Path(tmp) / "custom"
            self.assertEqual(
                store.resolve_target("My Dataset", "renamed", library_dir=custom),
                custom / "renamed",
            )

    def test_rejects_unsafe_destination_and_filesystem_root_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DatasetStore(tmp)
            with self.assertRaisesRegex(ValueError, "inside OC_DOWNLOAD_ROOT"):
                store.resolve_target("dataset", "../escape")
            with self.assertRaisesRegex(ValueError, "filesystem root"):
                store.resolve_library_dir("/")

    def test_detects_unmanaged_and_source_conflict_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DatasetStore(tmp)
            target = Path(tmp) / "dataset"
            target.mkdir()
            unmanaged = store.inspect("dataset", target, source_fingerprint="a" * 64)
            self.assertEqual(unmanaged.status, "unmanaged_directory")

            (target / ".openconstruction-manifest.json").write_text(
                json.dumps(
                    {
                        "dataset_id": "dataset",
                        "source_fingerprint": "b" * 64,
                        "files": [],
                    }
                ),
                encoding="utf-8",
            )
            conflict = store.inspect("dataset", target, source_fingerprint="a" * 64)
            self.assertEqual(conflict.status, "source_conflict")
            self.assertFalse(conflict.source_matches)


if __name__ == "__main__":
    unittest.main()
