from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import capabilities


class CapabilityPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = {
            "plugins": [
                {
                    "id": "bundle@market",
                    "name": "bundle",
                    "baseline_enabled": False,
                }
            ],
            "skills": [
                {
                    "id": "/skills/alpha/SKILL.md",
                    "path": "/skills/alpha/SKILL.md",
                    "name": "alpha",
                    "baseline_enabled": True,
                },
                {
                    "id": "/skills/beta/SKILL.md",
                    "path": "/skills/beta/SKILL.md",
                    "name": "beta",
                    "baseline_enabled": False,
                },
                {
                    "id": "/plugins/bundle/skills/gamma/SKILL.md",
                    "path": "/plugins/bundle/skills/gamma/SKILL.md",
                    "name": "bundle:gamma",
                    "plugin_id": "bundle@market",
                    "configured_baseline_enabled": True,
                    "baseline_enabled": False,
                },
            ],
            "mcp_servers": [
                {
                    "id": "docs.server",
                    "name": "docs.server",
                    "baseline_enabled": True,
                    "controllable": True,
                    "tools": [
                        {"id": "search", "name": "search", "baseline_enabled": True},
                        {"id": "fetch", "name": "fetch", "baseline_enabled": True},
                    ],
                }
            ],
        }

    def test_compiles_multiple_skills_as_one_toml_array(self) -> None:
        inventory_paths = capabilities.idle_inventory_skill_paths()
        first, second = inventory_paths[:2]
        overrides = capabilities.codex_config_overrides({
            "skills": {
                first: False,
                second: True,
            },
            "mcp_servers": {
                "openaiDeveloperDocs": {
                    "enabled": True,
                    "tools": {"search": False, "fetch": True},
                }
            },
        })

        skill_overrides = [item for item in overrides if item.startswith("skills.config=")]
        self.assertEqual(len(skill_overrides), 1)
        self.assertIn(first, skill_overrides[0])
        self.assertIn(second, skill_overrides[0])
        self.assertIn(
            'mcp_servers.openaiDeveloperDocs.url="https://developers.openai.com/mcp"',
            overrides,
        )
        self.assertIn('mcp_servers.openaiDeveloperDocs.enabled=true', overrides)
        self.assertIn(
            'mcp_servers.openaiDeveloperDocs.disabled_tools=["search"]', overrides
        )

    def test_agent_overrides_preserve_inherited_denials(self) -> None:
        managed_skill = capabilities.idle_inventory_skill_paths()[0]
        overrides = capabilities.codex_config_overrides(
            {
                "skills": {managed_skill: True},
                "mcp_servers": {
                    "openaiDeveloperDocs": {"tools": {"search": False}}
                },
            },
            {
                "skills": {"/skills/admin-disabled/SKILL.md": False},
                "mcp_disabled_tools": {
                    "openaiDeveloperDocs": ["admin_tool"]
                },
            },
        )
        skill_config = next(item for item in overrides if item.startswith("skills.config="))
        self.assertIn("admin-disabled/SKILL.md", skill_config)
        self.assertIn(managed_skill, skill_config)
        self.assertIn(
            'mcp_servers.openaiDeveloperDocs.disabled_tools=["admin_tool", "search"]',
            overrides,
        )

    def test_inventory_launches_local_mcp_from_central_copy(self) -> None:
        overrides = capabilities.codex_config_overrides({
            "mcp_servers": {"openconstruction": {"enabled": True}},
        })
        args = next(
            item for item in overrides
            if item.startswith("mcp_servers.openconstruction.args=")
        )
        self.assertIn("app/capability_inventory/mcp/openconstruction/implementation", args)
        self.assertNotIn("OpenConstruction/OC-mcp", args)

    def test_non_inventory_global_mcp_is_disabled_for_idle_agent(self) -> None:
        overrides = capabilities.codex_config_overrides(
            {}, {"mcp_server_names": ["private-global-server"]}
        )
        self.assertIn(
            "mcp_servers.private-global-server.enabled=false", overrides
        )

    def test_agentui_managed_mcp_is_omitted_without_agent_identity(self) -> None:
        overrides = capabilities.codex_config_overrides({}, env={})
        self.assertFalse(any(
            item.startswith("mcp_servers.agentui_notion_report.")
            for item in overrides
        ))

    def test_toggle_to_baseline_removes_override(self) -> None:
        disabled = capabilities.update_policy(
            {}, self.catalog, kind="skill", target="/skills/alpha/SKILL.md", enabled=False
        )
        self.assertEqual(disabled["skills"], {"/skills/alpha/SKILL.md": False})

        restored = capabilities.update_policy(
            disabled,
            self.catalog,
            kind="skill",
            target="/skills/alpha/SKILL.md",
            enabled=True,
        )
        self.assertEqual(restored["skills"], {})

    def test_server_disable_makes_tools_effectively_unavailable(self) -> None:
        effective = capabilities.effective_catalog(
            self.catalog,
            {"mcp_servers": {"docs.server": {"enabled": False}}},
        )
        server = effective["mcp_servers"][0]
        self.assertFalse(server["enabled"])
        self.assertTrue(all(not tool["enabled"] for tool in server["tools"]))
        self.assertTrue(all(tool["configured_enabled"] for tool in server["tools"]))

    def test_downloaded_plugin_controls_child_availability(self) -> None:
        disabled = capabilities.effective_catalog(self.catalog, {})
        child = disabled["skills"][2]
        self.assertFalse(disabled["plugins"][0]["enabled"])
        self.assertFalse(child["available"])
        self.assertFalse(child["enabled"])
        self.assertTrue(child["configured_enabled"])

        policy = capabilities.update_policy(
            {}, disabled, kind="plugin", target="bundle@market", enabled=True
        )
        enabled = capabilities.effective_catalog(self.catalog, policy)
        self.assertTrue(enabled["plugins"][0]["enabled"])
        self.assertTrue(enabled["skills"][2]["enabled"])

    def test_child_cannot_be_changed_before_parent_plugin_is_enabled(self) -> None:
        effective = capabilities.effective_catalog(self.catalog, {})
        with self.assertRaisesRegex(ValueError, "parent plugin"):
            capabilities.update_policy(
                {}, effective, kind="skill",
                target="/plugins/bundle/skills/gamma/SKILL.md", enabled=False,
            )

    def test_idle_inventory_roots_never_escape_the_canonical_store(self) -> None:
        managed_skill = next(
            path for path in capabilities.idle_inventory_skill_paths()
            if Path(path).parent.parent == capabilities._IDLE_INVENTORY_SKILLS.resolve()
        )
        roots = capabilities.plugin_extra_roots({
            "skills": {managed_skill: True},
        })
        self.assertIn(str(capabilities._IDLE_INVENTORY_SKILLS.resolve()), roots)
        self.assertTrue(all(
            Path(root).resolve().is_relative_to(
                capabilities._IDLE_INVENTORY_ROOT.resolve()
            )
            for root in roots
        ))

    def test_explicit_skill_mentions_resolve_only_enabled_idle_skills(self) -> None:
        openai_docs = next(
            skill for skill in capabilities.enabled_inventory_skills({})
            if skill["name"] == "openai-docs"
        )
        resolved = capabilities.explicit_skill_inputs(
            "Use $openai-docs, ignore $does-not-exist, then $openai-docs again.",
            {},
        )
        self.assertEqual(resolved, [openai_docs])
        self.assertEqual(
            capabilities.explicit_skill_inputs(
                "Use $openai-docs", {"skills": {openai_docs["path"]: False}}
            ),
            [],
        )

    def test_dashboard_catalog_reads_only_idle_inventory(self) -> None:
        catalog = asyncio.run(capabilities.discover_catalog(
            config=None,
            cwd="/tmp/must-not-be-scanned",
            cache_key="unit-central-inventory",
            force=True,
        ))
        inventory_root = capabilities._IDLE_INVENTORY_ROOT.resolve()
        snapshot = capabilities.load_inventory_snapshot()
        self.assertEqual(catalog["source"], "idle-inventory")
        self.assertEqual(len(catalog["skills"]), len(snapshot["skills"]))
        self.assertEqual(len(catalog["mcp_servers"]), len(snapshot["mcp_servers"]))
        self.assertTrue(all(
            Path(skill["path"]).resolve().is_relative_to(inventory_root)
            for skill in catalog["skills"]
        ))
        self.assertTrue(all(
            Path(server["inventory_manifest"]).resolve().is_relative_to(inventory_root)
            for server in catalog["mcp_servers"]
        ))

    def test_unknown_targets_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "skill not found"):
            capabilities.update_policy(
                {}, self.catalog, kind="skill", target="/tmp/injected", enabled=False
            )

    def test_runtime_managed_mcp_cannot_produce_invalid_override(self) -> None:
        catalog = {
            "skills": [],
            "mcp_servers": [{
                "id": "codex_apps",
                "name": "codex_apps",
                "baseline_enabled": True,
                "controllable": False,
                "tools": [{"id": "search", "name": "search", "baseline_enabled": True}],
            }],
        }
        with self.assertRaisesRegex(ValueError, "managed by the Codex runtime"):
            capabilities.update_policy(
                {}, catalog, kind="mcp_server", target="codex_apps", enabled=False
            )

    def test_delete_skill_removes_package_and_keeps_tombstone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            inventory = repo / "app" / "capability_inventory"
            skill_file = inventory / "skills" / "sample" / "SKILL.md"
            skill_file.parent.mkdir(parents=True)
            skill_file.write_text("---\nname: sample\n---\n", encoding="utf-8")
            index = inventory / "inventory.json"
            index.write_text(json.dumps({
                "skills": [{
                    "id": "sample", "name": "sample", "scope": "system",
                    "plugin_id": None,
                    "path": "app/capability_inventory/skills/sample/SKILL.md",
                    "source_path": "/external/sample/SKILL.md",
                }],
                "plugins": [], "mcp_servers": [],
            }), encoding="utf-8")
            with (
                patch.object(capabilities, "_IDLE_REPO_ROOT", repo),
                patch.object(capabilities, "_IDLE_INVENTORY_ROOT", inventory),
                patch.object(capabilities, "_IDLE_INVENTORY_SKILLS", inventory / "skills"),
                patch.object(capabilities, "_IDLE_INVENTORY_INDEX", index),
            ):
                removed = capabilities.delete_inventory_capability(
                    kind="skill", target=str(skill_file)
                )
                saved = json.loads(index.read_text(encoding="utf-8"))
                overrides = capabilities.codex_config_overrides({})

            self.assertEqual(removed["name"], "sample")
            self.assertFalse(skill_file.parent.exists())
            self.assertEqual(saved["skills"], [])
            self.assertEqual(saved["deleted_skills"][0]["source_path"], "/external/sample/SKILL.md")
            self.assertIn("/external/sample/SKILL.md", next(
                item for item in overrides if item.startswith("skills.config=")
            ))


if __name__ == "__main__":
    unittest.main()
