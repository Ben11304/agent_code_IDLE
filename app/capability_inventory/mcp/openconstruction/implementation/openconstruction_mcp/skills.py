from __future__ import annotations

import json
import os
import sysconfig
from functools import lru_cache
from pathlib import Path
from typing import Any


REPO_SKILL_INDEX_PATH = Path(__file__).resolve().parent.parent / "skills" / "index.json"
INSTALLED_SKILL_INDEX_PATH = Path(sysconfig.get_path("data")) / "share" / "openconstruction" / "skills" / "index.json"


def skill_index_path() -> Path:
    configured = os.environ.get("OPENCONSTRUCTION_SKILL_INDEX", "").strip()
    candidates = [Path(configured).expanduser()] if configured else []
    candidates.extend([REPO_SKILL_INDEX_PATH, INSTALLED_SKILL_INDEX_PATH])
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("OpenConstruction skill index is not installed; reinstall the package or set OPENCONSTRUCTION_SKILL_INDEX")


@lru_cache(maxsize=1)
def load_skill_index() -> dict[str, Any]:
    with skill_index_path().open("r", encoding="utf-8") as handle:
        return json.load(handle)


def list_skills(lifecycle_stage: str | None = None) -> dict[str, Any]:
    index = load_skill_index()
    skills = index.get("skills", [])
    if lifecycle_stage:
        skills = [skill for skill in skills if skill.get("lifecycle_stage") == lifecycle_stage]
    return {
        "schema_version": index.get("schema_version"),
        "updated": index.get("updated"),
        "source_repo": index.get("source_repo"),
        "skills": skills,
    }


def get_skill(skill_id: str) -> dict[str, Any] | None:
    skill_id = skill_id.strip()
    for skill in load_skill_index().get("skills", []):
        if skill.get("id") == skill_id:
            return skill
    return None
