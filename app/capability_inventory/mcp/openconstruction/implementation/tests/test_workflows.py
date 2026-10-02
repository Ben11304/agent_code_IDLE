import unittest

from openconstruction_mcp.catalog import CatalogClient
from openconstruction_mcp.workflows import dataset_discovery


def fixture(path):
    if path == "datasets.json":
        return {
            "ppe-images": {
                "name": "PPE Image Dataset",
                "summary": "Construction site images with hardhat and vest bounding box annotations.",
                "tasks": ["safety monitoring", "helmet detection"],
                "modalities": ["image"],
                "classes": ["hardhat", "worker", "vest"],
                "license": "CC BY 4.0",
                "url": "https://example.org/ppe",
                "year": 2024,
            },
            "bridge-cracks": {
                "name": "Bridge Crack Dataset",
                "summary": "Bridge inspection photos with crack segmentation masks.",
                "tasks": ["defect detection"],
                "modalities": ["image"],
                "classes": ["crack"],
                "license": "MIT",
                "url": "https://example.org/bridge",
                "year": 2023,
            },
        }
    return []


class DatasetDiscoveryWorkflowTest(unittest.TestCase):
    def test_ranks_dataset_candidates_by_constraints(self):
        client = CatalogClient(fetch_json=fixture)
        result = dataset_discovery(
            {
                "task": "helmet detection",
                "modality": "image",
                "object_class": "hardhat",
                "annotation": "bounding box",
                "license": "CC BY",
                "limit": 2,
            },
            client,
        )

        self.assertEqual(result["skill"]["id"], "dataset-discovery")
        self.assertEqual(result["candidate_datasets"][0]["id"], "ppe-images")
        self.assertGreater(result["candidate_datasets"][0]["fit_score"], result["candidate_datasets"][1]["fit_score"])
        self.assertTrue(result["candidate_datasets"][0]["fit_reasons"])
        self.assertTrue(result["next_actions"])

    def test_handles_no_strong_matches(self):
        client = CatalogClient(fetch_json=fixture)
        result = dataset_discovery({"task": "thermal robotics", "limit": 1}, client)

        self.assertEqual(len(result["candidate_datasets"]), 1)
        self.assertIn("selection_notes", result)


if __name__ == "__main__":
    unittest.main()
