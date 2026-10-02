from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / "skills"
INDEX_PATH = SKILLS_DIR / "index.json"

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
TIERS = {"official", "verified_community", "community"}
STATUSES = {"draft", "review", "verified", "deprecated"}
STAGES = {"discover", "select", "access", "understand", "analyze", "execute"}
PERMISSIONS = {"read_openconstruction", "github_read", "huggingface_read", "network_access", "sandbox_execution"}
RISK_LEVELS = {"low", "medium", "high"}


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def fail(errors: list[str], message: str) -> None:
    errors.append(message)


def validate_skill(skill: dict[str, Any], errors: list[str]) -> None:
    skill_id = str(skill.get("id") or "<missing-id>")
    missing = sorted(REQUIRED_FIELDS - set(skill))
    if missing:
        fail(errors, f"{skill_id}: missing required fields: {', '.join(missing)}")

    if skill.get("tier") not in TIERS:
        fail(errors, f"{skill_id}: invalid tier {skill.get('tier')!r}")
    if skill.get("status") not in STATUSES:
        fail(errors, f"{skill_id}: invalid status {skill.get('status')!r}")
    if skill.get("lifecycle_stage") not in STAGES:
        fail(errors, f"{skill_id}: invalid lifecycle_stage {skill.get('lifecycle_stage')!r}")
    if skill.get("risk_level") not in RISK_LEVELS:
        fail(errors, f"{skill_id}: invalid risk_level {skill.get('risk_level')!r}")

    undeclared_permissions = set(skill.get("permissions") or []) - PERMISSIONS
    if undeclared_permissions:
        fail(errors, f"{skill_id}: unknown permissions: {', '.join(sorted(undeclared_permissions))}")

    if skill.get("risk_level") != "high" and "sandbox_execution" in set(skill.get("permissions") or []):
        fail(errors, f"{skill_id}: sandbox_execution requires risk_level high")

    for field in ("inputs", "outputs", "tools", "permissions"):
        if not isinstance(skill.get(field), list):
            fail(errors, f"{skill_id}: {field} must be a list")

    review = skill.get("review")
    if not isinstance(review, dict):
        fail(errors, f"{skill_id}: review must be an object")
    else:
        for key in ("reviewer", "reviewed_date", "test_status", "security_notes"):
            if key not in review:
                fail(errors, f"{skill_id}: review.{key} is required")

    runtime = skill.get("runtime")
    if runtime:
        runtime_tool = runtime.get("tool") if isinstance(runtime, dict) else None
        if not runtime_tool:
            fail(errors, f"{skill_id}: runtime.tool is required when runtime is set")
        elif runtime_tool not in set(skill.get("tools") or []):
            fail(errors, f"{skill_id}: runtime.tool must be declared in tools")

    instruction_path = skill.get("instruction_path")
    if instruction_path and not (ROOT / instruction_path).exists():
        fail(errors, f"{skill_id}: instruction_path does not exist: {instruction_path}")

    metadata_path = skill.get("metadata_path")
    if metadata_path and not (ROOT / metadata_path).exists():
        fail(errors, f"{skill_id}: metadata_path does not exist: {metadata_path}")


def main() -> int:
    errors: list[str] = []
    index = load_json(INDEX_PATH)
    skills = index.get("skills")
    if not isinstance(skills, list):
        fail(errors, "skills/index.json: skills must be a list")
        skills = []

    seen = set()
    for skill in skills:
        if not isinstance(skill, dict):
            fail(errors, "skills/index.json: each skill must be an object")
            continue
        skill_id = skill.get("id")
        if skill_id in seen:
            fail(errors, f"{skill_id}: duplicate skill id")
        seen.add(skill_id)
        validate_skill(skill, errors)

        metadata_path = skill.get("metadata_path")
        if metadata_path:
            metadata = load_json(ROOT / metadata_path)
            if metadata.get("id") != skill_id:
                fail(errors, f"{skill_id}: metadata_path id does not match index entry")

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    print(f"Validated {len(skills)} skill(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
