from __future__ import annotations

import json
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / "skills"
PACKAGE_DIR = ROOT / "packages" / "skills"
INDEX_PATH = SKILLS_DIR / "index.json"


def load_index() -> dict:
    with INDEX_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def package_skill(skill_id: str) -> Path:
    skill_dir = SKILLS_DIR / skill_id
    if not skill_dir.exists():
        raise FileNotFoundError(f"Missing skill directory: {skill_dir}")
    if not (skill_dir / "SKILL.md").exists():
        raise FileNotFoundError(f"Missing SKILL.md in {skill_dir}")

    PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
    package_path = PACKAGE_DIR / f"{skill_id}.zip"
    with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(skill_dir.rglob("*")):
            if path.is_file():
                archive.write(path, Path(skill_id) / path.relative_to(skill_dir))
    return package_path


def main() -> int:
    index = load_index()
    skills = index.get("skills") or []
    for skill in skills:
        skill_id = skill.get("id")
        if not skill_id:
            continue
        package_path = package_skill(skill_id)
        print(f"Packaged {skill_id}: {package_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
