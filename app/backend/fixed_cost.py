"""Fixed context cost per agent — the task-independent floor.

Every turn, before doing any work, an agent pays for a fixed set of bytes: its system
prompt (AGENT.md) plus whatever files its pre-flight block tells it to read every
session (shared contracts, glossary, schema, …). This is the "how heavy is this agent
to wake up" number the user wanted beside each agent — independent of the task.

Measured from files, so it is knowable without running the agent. chars/4 is the usual
rough token rule; it under-counts markdown and non-ASCII, so treat these as a floor.

Generality: any AGENT.md that lists file paths in backticks inside a PRE-FLIGHT / Required
reads block is parsed. A project whose prompt lists nothing just gets the system-prompt
size — still correct, just smaller.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# The heading that opens the "files to read every session" block. Agents phrase it
# differently (PRE-FLIGHT / Required reads / Boot / read in this order …), so match the
# common variants rather than one fixed string.
_PREFLIGHT_HDR = re.compile(
    r"^#{1,4}\s*\d*\.?\s*(PRE-?FLIGHT|Required reads|Boot\b|read in this order|Reading)",
    re.I | re.M)
_PATH_RE = re.compile(r"`([A-Za-z0-9_./\-]+\.(?:md|json|jsonl|yaml|yml|txt))`")

# (agent_dir, mtime-signature) -> result, so /api/dashboard polling doesn't re-read the
# files every tick. Invalidated when any input file's mtime changes.
_cache: dict[str, tuple[float, dict]] = {}


def _preflight_rel_paths(agent_md_text: str) -> list[str]:
    m = _PREFLIGHT_HDR.search(agent_md_text)
    if not m:
        return []
    rest = agent_md_text[m.end():]
    nxt = re.search(r"^#{1,4}\s", rest, re.M)
    block = rest[: nxt.start()] if nxt else rest
    out, seen = [], set()
    for p in _PATH_RE.findall(block):
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _resolve(agent_dir: Path, project_root: Path, rel: str) -> Optional[Path]:
    for cand in (agent_dir / rel, project_root / rel, project_root / agent_dir.name / rel):
        try:
            c = cand.resolve()
        except OSError:
            continue
        if c.is_file():
            return c
    return None


def estimate(project_root: str, agent_dir: Path, system_prompt_file: str) -> dict:
    """Fixed-cost breakdown for one agent:
      { "tokens": int, "chars": int, "system_prompt": int (tok), "preflight": int (tok),
        "files": [ {name, tokens} ] }
    Everything task-independent that the agent (re)loads every session.
    """
    root = Path(project_root)
    sp_path = (root / system_prompt_file) if system_prompt_file else None

    # mtime signature of every input, to key the cache.
    sig_parts = []
    sp_text = ""
    if sp_path and sp_path.is_file():
        try:
            sig_parts.append(str(sp_path.stat().st_mtime))
            sp_text = sp_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    rels = _preflight_rel_paths(sp_text)
    resolved: list[tuple[str, Path]] = []
    for rel in rels:
        p = _resolve(agent_dir, root, rel)
        if p:
            try:
                sig_parts.append(f"{rel}:{p.stat().st_mtime}")
            except OSError:
                continue
            resolved.append((rel, p))

    cache_key = str(agent_dir)
    sig = float(hash("|".join(sig_parts)) & 0xFFFFFFFF)
    hit = _cache.get(cache_key)
    if hit and hit[0] == sig:
        return hit[1]

    sp_chars = len(sp_text)
    files = []
    pf_chars = 0
    seen_paths = set()
    for rel, p in resolved:
        rp = str(p)
        if rp in seen_paths:
            continue
        seen_paths.add(rp)
        try:
            chars = len(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        pf_chars += chars
        files.append({"name": rel, "tokens": chars // 4})

    total_chars = sp_chars + pf_chars
    result = {
        "tokens": total_chars // 4,
        "chars": total_chars,
        "system_prompt": sp_chars // 4,
        "preflight": pf_chars // 4,
        "files": sorted(files, key=lambda f: -f["tokens"])[:12],
    }
    _cache[cache_key] = (sig, result)
    return result
