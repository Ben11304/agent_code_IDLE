import unittest

from openconstruction_mcp.skills import get_skill, list_skills


REQUIRED_FIELDS = {
    "id",
    "name",
    "author",
    "version",
    "tier",
    "status",
    "lifecycle_stage",
    "description",
    "inputs",
    "outputs",
    "tools",
    "permissions",
    "risk_level",
    "license",
    "review",
}


class SkillsTest(unittest.TestCase):
    def test_lists_repo_defined_skills(self):
        result = list_skills()
        skills = result["skills"]
        self.assertGreaterEqual(len(skills), 1)
        self.assertEqual(skills[0]["id"], "dataset-discovery")

    def test_get_skill_by_id(self):
        skill = get_skill("dataset-discovery")
        self.assertIsNotNone(skill)
        self.assertEqual(skill["lifecycle_stage"], "discover")
        self.assertEqual(skill["risk_level"], "low")
        self.assertEqual(skill["runtime"]["tool"], "run_dataset_discovery")
        self.assertIn("run_dataset_discovery", skill["tools"])

    def test_required_metadata_fields(self):
        for skill in list_skills()["skills"]:
            self.assertFalse(REQUIRED_FIELDS - set(skill))
            self.assertIn(skill["tier"], {"official", "verified_community", "community"})
            self.assertIn(skill["status"], {"draft", "review", "verified", "deprecated"})
            self.assertIn(skill["risk_level"], {"low", "medium", "high"})


if __name__ == "__main__":
    unittest.main()
