from __future__ import annotations

import unittest

from . import adapters


class CodexNotionEnvironmentTests(unittest.TestCase):
    def test_destination_token_is_forwarded_by_name_only(self) -> None:
        secret = "secret-must-never-enter-codex-config"
        runtime_env = {
            "AGENTUI_PROJECT_SLUG": "gelsight",
            "AGENTUI_AGENT_ID": "BOSS",
            "NOTION_REPORT_CONFIG": "/tmp/destinations.json",
            "AGENTUI_NOTION_TOKEN_ENV": "NOTION_REPORT_TOKEN",
            "NOTION_REPORT_TOKEN": secret,
        }

        names = adapters._codex_notion_env_vars(runtime_env)

        self.assertEqual(
            names,
            [
                "AGENTUI_PROJECT_SLUG",
                "AGENTUI_AGENT_ID",
                "AGENTUI_PROJECT_NAME",
                "AGENTUI_PROJECT_ROOT_AGENT_ID",
                "NOTION_REPORT_CONFIG",
                "NOTION_REPORT_TOKEN",
            ],
        )
        self.assertNotIn(secret, repr(names))

    def test_invalid_token_variable_name_is_not_forwarded(self) -> None:
        names = adapters._codex_notion_env_vars({
            "AGENTUI_NOTION_TOKEN_ENV": "NOTION_REPORT_TOKEN;bad",
        })
        self.assertNotIn("NOTION_REPORT_TOKEN;bad", names)

    def test_mcp_argv_carries_scope_but_never_secret(self) -> None:
        secret = "secret-must-stay-out-of-argv"
        args = adapters._notion_mcp_args({
            "AGENTUI_PROJECT_SLUG": "gelsight",
            "AGENTUI_AGENT_ID": "BOSS",
            "NOTION_REPORT_CONFIG": "/tmp/destinations.json",
            "NOTION_REPORT_TOKEN": secret,
        })
        self.assertEqual(args[1:], [
            "--project", "gelsight",
            "--agent", "BOSS",
            "--config", "/tmp/destinations.json",
        ])
        self.assertNotIn(secret, repr(args))

    def test_mcp_argv_carries_auto_provision_metadata(self) -> None:
        args = adapters._notion_mcp_args({
            "AGENTUI_PROJECT_SLUG": "gelsight",
            "AGENTUI_AGENT_ID": "MODEL",
            "AGENTUI_PROJECT_NAME": "GelSight Research",
            "AGENTUI_PROJECT_ROOT_AGENT_ID": "BOSS",
            "NOTION_REPORT_CONFIG": "/tmp/destinations.json",
        })
        self.assertEqual(args[-4:], [
            "--project-name", "GelSight Research",
            "--project-root-agent", "BOSS",
        ])

    def test_mcp_argv_carries_project_report_schema_scope(self) -> None:
        args = adapters._notion_mcp_args({
            "AGENTUI_PROJECT_SLUG": "gelsight",
            "AGENTUI_AGENT_ID": "BOSS",
            "NOTION_REPORT_CONFIG": "/tmp/destinations.json",
            "AGENTUI_PROJECT_ROOT": "/workspace/gelsight",
            "AGENTUI_REPORT_SCHEMA_FILE": "/workspace/gelsight/.agentui/report_schema.yaml",
        })
        self.assertEqual(args[-4:], [
            "--project-directory", "/workspace/gelsight",
            "--report-schema-file", "/workspace/gelsight/.agentui/report_schema.yaml",
        ])

    def test_sdk_config_keeps_secret_out_of_config_overrides(self) -> None:
        secret = "secret-only-for-child-environment"
        config, _sandbox, _approval, _mode = adapters._codex_sdk_settings({
            "AGENTUI_PROJECT_SLUG": "gelsight",
            "AGENTUI_AGENT_ID": "BOSS",
            "NOTION_REPORT_CONFIG": "/tmp/destinations.json",
            "AGENTUI_NOTION_TOKEN_ENV": "NOTION_REPORT_TOKEN",
            "NOTION_REPORT_TOKEN": secret,
        })

        self.assertNotIn(secret, repr(config.config_overrides))
        self.assertEqual(config.env["NOTION_REPORT_TOKEN"], secret)
        self.assertIn(
            'mcp_servers.agentui_notion_report.env_vars=["AGENTUI_PROJECT_SLUG", '
            '"AGENTUI_AGENT_ID", "AGENTUI_PROJECT_NAME", '
            '"AGENTUI_PROJECT_ROOT_AGENT_ID", "NOTION_REPORT_CONFIG", '
            '"NOTION_REPORT_TOKEN"]',
            config.config_overrides,
        )


if __name__ == "__main__":
    unittest.main()
