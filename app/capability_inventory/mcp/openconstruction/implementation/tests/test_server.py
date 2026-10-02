import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openconstruction_mcp.acquisition import AcquisitionPlan
from openconstruction_mcp.server import (
    TOOLS,
    call_tool,
    download_manager,
    handle_request,
    paper_catalog,
    research_preparation_view,
)


class ServerTest(unittest.TestCase):
    def research_plan(self):
        return AcquisitionPlan(
            dataset_id="CEQuest",
            dataset_name="CEQuest",
            kind="direct",
            license="CC BY 4.0",
            url="https://example.org/cequest.zip",
            executable_locally=True,
        )

    def test_research_preparation_reviews_license_before_starting(self):
        paper_plan = {
            "available": True,
            "paper_title": "CEQuest Paper",
            "paper_license": "CC BY 4.0",
            "redistribution_status": "verified_redistributable",
        }
        with patch("openconstruction_mcp.server.resolve_dataset_download_plan", return_value=self.research_plan()), patch.object(
            paper_catalog, "resolve", return_value=paper_plan
        ), patch.object(download_manager, "start") as start:
            result = call_tool(
                "prepare_dataset_for_research",
                {"dataset_id": "CEQuest", "library_dir": "/tmp/oc-research-library"},
            )

        self.assertEqual(result["status"], "license_acceptance_required")
        self.assertTrue(result["package_contents"]["related_paper"])
        self.assertFalse(result["package_contents"]["pdf_to_markdown"])
        self.assertEqual(result["next_step"]["arguments"]["accept_license"], True)
        self.assertEqual(result["next_step"]["arguments"]["library_dir"], "/tmp/oc-research-library")
        start.assert_not_called()

    def test_research_preparation_starts_one_step_bundle_workflow(self):
        paper_plan = {"available": True, "paper_title": "CEQuest Paper"}
        started = {
            "download_id": "rp_0123456789abcdef",
            "dataset_id": "CEQuest",
            "dataset_name": "CEQuest",
            "status": "queued",
            "workflow": "research_preparation",
            "related_paper": {"download_status": "queued", "paper_title": "CEQuest Paper"},
            "warnings": [],
        }
        with patch("openconstruction_mcp.server.resolve_dataset_download_plan", return_value=self.research_plan()), patch.object(
            paper_catalog, "resolve", return_value=paper_plan
        ), patch("openconstruction_mcp.server.find_research_bundle_for_dataset", return_value=None), patch.object(
            download_manager, "has_accepted_resume", return_value=False
        ), patch.object(
            download_manager, "start", return_value=started
        ) as start:
            result = call_tool(
                "prepare_dataset_for_research",
                {"dataset_id": "CEQuest", "accept_license": True},
            )

        self.assertEqual(result["preparation_id"], "rp_0123456789abcdef")
        self.assertFalse(result["package_contents"]["pdf_to_markdown"])
        self.assertTrue(start.call_args.kwargs["include_papers"])
        self.assertTrue(start.call_args.kwargs["create_bundle"])

    def test_research_preparation_auto_resumes_saved_license_checkpoint(self):
        paper_plan = {"available": True, "paper_title": "CEQuest Paper"}
        resumed_job = {
            "download_id": "rp_0123456789abcdef",
            "dataset_id": "CEQuest",
            "dataset_name": "CEQuest",
            "status": "queued",
            "workflow": "research_preparation",
            "resumed": True,
            "related_paper": {"download_status": "queued", "paper_title": "CEQuest Paper"},
            "warnings": [],
        }
        with patch("openconstruction_mcp.server.resolve_dataset_download_plan", return_value=self.research_plan()), patch.object(
            paper_catalog, "resolve", return_value=paper_plan
        ), patch("openconstruction_mcp.server.find_research_bundle_for_dataset", return_value=None), patch.object(
            download_manager, "has_accepted_resume", return_value=True
        ), patch.object(download_manager, "start", return_value=resumed_job) as start:
            result = call_tool("prepare_dataset_for_research", {"dataset_id": "CEQuest"})

        self.assertTrue(result["resumed"])
        self.assertFalse(start.call_args.args[2])
        self.assertTrue(start.call_args.kwargs["create_bundle"])

    def test_research_view_explains_cross_process_reuse(self):
        result = research_preparation_view(
            {
                "download_id": "rp_0123456789abcdef",
                "dataset_id": "CEQuest",
                "dataset_name": "CEQuest",
                "status": "downloading",
                "already_running": True,
                "related_paper": {"download_status": "queued"},
            }
        )

        self.assertTrue(result["already_running"])
        self.assertIn("Another OpenConstruction process", result["user_message"])

    def test_research_preparation_status_rediscovers_completed_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "CEQuest"
            (target / "research").mkdir(parents=True)
            (target / "papers").mkdir()
            (target / "papers" / "paper.pdf").write_bytes(b"%PDF-test")
            (target / "research" / "bundle.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "bundle_id": "rb_0123456789abcdef",
                        "preparation_id": "rp_0123456789abcdef",
                        "status": "ready",
                        "dataset": {"id": "CEQuest", "name": "CEQuest", "license": "CC BY 4.0"},
                        "papers": [
                            {
                                "title": "CEQuest Paper",
                                "license": "CC BY 4.0",
                                "redistribution_status": "verified_redistributable",
                                "path": "../papers/paper.pdf",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(download_manager, "root", Path(tmp)), patch.object(
                download_manager, "get", side_effect=ValueError("restart")
            ):
                result = call_tool(
                    "get_research_preparation_status",
                    {"preparation_id": "rp_0123456789abcdef"},
                )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["progress_percent"], 100.0)
        self.assertEqual(result["research_bundle"]["bundle_id"], "rb_0123456789abcdef")
        self.assertFalse(result["package_contents"]["pdf_to_markdown"])

    def test_download_plan_includes_paper_by_default_and_allows_opt_out(self):
        plan = AcquisitionPlan(
            dataset_id="CEQuest",
            dataset_name="CEQuest",
            kind="direct",
            executable_locally=True,
        )
        paper_plan = {"dataset_id": "CEQuest", "available": True}
        with patch("openconstruction_mcp.server.resolve_dataset_download_plan", return_value=plan), patch.object(
            paper_catalog, "resolve", return_value=paper_plan
        ) as resolve_paper:
            included = call_tool("get_dataset_download_plan", {"dataset_id": "CEQuest"})
            excluded = call_tool(
                "get_dataset_download_plan",
                {"dataset_id": "CEQuest", "include_papers": False},
            )

        self.assertTrue(included["include_papers"])
        self.assertEqual(included["paper_plan"], paper_plan)
        self.assertFalse(excluded["include_papers"])
        self.assertIsNone(excluded["paper_plan"])
        resolve_paper.assert_called_once_with("CEQuest")

    def test_checks_installation_in_custom_library(self):
        plan = self.research_plan()
        inspection = {
            "dataset_id": "CEQuest",
            "status": "installed",
            "installed": True,
            "library_dir": "/tmp/oc-library",
        }
        with patch("openconstruction_mcp.server.resolve_dataset_download_plan", return_value=plan), patch.object(
            paper_catalog, "resolve", return_value={"available": False}
        ), patch.object(download_manager, "inspect_installation", return_value=inspection) as inspect:
            result = call_tool(
                "check_dataset_installation",
                {
                    "dataset_id": "CEQuest",
                    "library_dir": "/tmp/oc-library",
                    "verify_checksums": True,
                },
            )

        self.assertEqual(result["status"], "installed")
        self.assertEqual(inspect.call_args.kwargs["library_dir"], "/tmp/oc-library")
        self.assertTrue(inspect.call_args.kwargs["verify_checksums"])

    def test_lists_tools(self):
        result = handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = [tool["name"] for tool in result["result"]["tools"]]
        self.assertIn("search_resources", names)
        self.assertIn("find_datasets", names)
        self.assertIn("run_dataset_discovery", names)
        self.assertIn("validate_metadata_record", names)
        self.assertIn("list_skills", names)
        self.assertIn("get_skill", names)
        self.assertIn("get_dataset_download_plan", names)
        self.assertIn("get_dataset_paper_plan", names)
        self.assertIn("check_dataset_installation", names)
        self.assertIn("prepare_dataset_for_research", names)
        self.assertIn("get_research_preparation_status", names)
        self.assertIn("download_dataset", names)
        self.assertIn("get_download_status", names)
        self.assertIn("cancel_download", names)
        self.assertEqual(len(names), len({tool["name"] for tool in TOOLS}))

    def test_remote_tool_list_excludes_local_filesystem_tools(self):
        result = handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            allow_download_execution=False,
        )
        names = [tool["name"] for tool in result["result"]["tools"]]
        self.assertIn("get_dataset_download_plan", names)
        self.assertNotIn("check_dataset_installation", names)
        self.assertNotIn("prepare_dataset_for_research", names)
        self.assertNotIn("get_research_preparation_status", names)
        self.assertNotIn("download_dataset", names)
        self.assertNotIn("get_download_status", names)
        self.assertNotIn("cancel_download", names)

    def test_initialize(self):
        result = handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(result["result"]["serverInfo"]["name"], "openconstruction")
        self.assertIn("auth_required", result["result"]["instructions"])
        self.assertIn("never ask for credentials", result["result"]["instructions"])
        self.assertIn("progress_text", result["result"]["instructions"])
        self.assertIn("every two seconds", result["result"]["instructions"])
        self.assertIn("included by default", result["result"]["instructions"])
        self.assertIn("never disable it solely", result["result"]["instructions"])
        self.assertIn("prefer prepare_dataset_for_research", result["result"]["instructions"])
        self.assertIn("resumes the matching checkpoint", result["result"]["instructions"])
        self.assertIn("do not claim that OC converts", result["result"]["instructions"])
        self.assertIn("tools", result["result"]["capabilities"])


if __name__ == "__main__":
    unittest.main()
