from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from plugins.package.schemas import PluginManifestModel, SkillAgentModel
from plugins.package.validation import validate_plugin_dir

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MARKETPLACE_ROOT = PLUGIN_ROOT.parents[1]
SKILL_ROOT = PLUGIN_ROOT / "skills" / "deep-research"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_work_manifest_associates_the_first_party_ecosystem_app() -> None:
    raw_manifest = _read_json(PLUGIN_ROOT / ".codex-plugin" / "plugin.json")
    manifest = PluginManifestModel.model_validate(raw_manifest)
    app_manifest = _read_json(PLUGIN_ROOT / ".app.json")

    assert manifest.name == "deep-research-work"
    assert manifest.skills == "./skills/"
    assert manifest.apps == "./.app.json"
    assert manifest.interface.display_name == "Deep Research"
    assert manifest.interface.category == "Education & Research"
    assert "mcpServers" not in raw_manifest
    assert app_manifest == {
        "apps": {
            "deep_research_work": {
                "id": "connector_openai_deep_research_work",
            }
        }
    }
    assert (PLUGIN_ROOT / "assets" / "deep-research.svg").is_file()
    validation = validate_plugin_dir(PLUGIN_ROOT)
    assert not validation.has_errors(), validation.notices
    assert validation.package is not None
    assert [skill.name for skill in validation.package.skills] == ["deep-research"]
    assert {skill.name for skill in (PLUGIN_ROOT / "skills").iterdir() if skill.is_dir()} == {
        "deep-research"
    }


def test_maintained_marketplace_registers_one_available_ecosystem_app_plugin() -> None:
    marketplace = _read_json(MARKETPLACE_ROOT / "marketplace.json")

    assert [entry for entry in marketplace["plugins"] if entry["name"] == "deep-research-work"] == [
        {
            "name": "deep-research-work",
            "source": {
                "source": "local",
                "path": "./plugins/deep-research-work",
            },
            "policy": {
                "installation": "AVAILABLE",
                "authentication": "ON_INSTALL",
                "statsigGate": "deep-research-work-marketplace-eligibility",
            },
            "category": "Education & Research",
        }
    ]


def test_work_skill_supports_unrestricted_executor_discovery() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---(?:\n|\Z)", skill, re.DOTALL)
    assert match is not None
    frontmatter = yaml.safe_load(match.group(1))
    assert isinstance(frontmatter, dict)
    assert frontmatter["name"] == "deep-research"
    manifest = PluginManifestModel.model_validate(
        _read_json(PLUGIN_ROOT / ".codex-plugin" / "plugin.json")
    )
    assert f"{manifest.name}:{frontmatter['name']}" == "deep-research-work:deep-research"

    agent = SkillAgentModel.model_validate(
        yaml.safe_load((SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8"))
    )
    assert agent.interface is not None
    assert agent.interface.default_prompt is not None
    assert "$deep-research" in agent.interface.default_prompt
    assert agent.policy is not None
    assert agent.policy.products is None
    assert agent.policy.allow_implicit_invocation is True
