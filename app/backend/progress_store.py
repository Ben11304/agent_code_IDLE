"""Structured progress log with automatic archival.

The old `state/progress.md` was an append-only markdown log the agents kept writing
to. It only ever grew, so the cold-start preamble either loaded stale head sections
(ordering was inconsistent — some agents newest-first, some oldest-first) or a huge
excerpt. Measured: agents read only the 5-15 newest lines 92% of the time, so the tail
was dead weight on disk and a correctness hazard when the head held the OLDEST day.

New shape — JSON keyed by date, so ordering is explicit (sort the keys, never guess):

    {
      "2026-07-17": [ {"time": "16:25", "what": "..."}, {"time": "11:05", "what": "..."} ],
      "2026-07-16": [ {"time": "22:37", "what": "..."} ]
    }

Two files:
  - HOT  `state/progress.json`                — the KEEP_DAYS most recent days; read every flight.
  - COLD `state/archive/<AGENT>_progress.json` — every older day, merged into ONE file; rarely read.

The rotator (in main.py, mirroring _scheduler_loop) moves days past the window from hot
to cold. This module is the pure read/rotate logic — no scheduling, no I/O policy.

Backward compatible: if `progress.json` is absent, the reader falls back to parsing the
legacy `progress.md`, so nothing breaks before an agent has migrated.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# "## 2026-07-17 16:25 — headline"  or  "## 2026-07-17 — headline" (legacy markdown)
_MD_SECTION = re.compile(
    r"^##\s+(\d{4}-\d{2}-\d{2})(?:[ T]+(\d{1,2}:\d{2}))?\s*(?:[—:-]\s*)?(.*)$"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _hot_path(agent_dir: Path) -> Path:
    return agent_dir / "state" / "progress.json"


def _archive_path(agent_dir: Path, agent_id: str) -> Path:
    return agent_dir / "state" / "archive" / f"{agent_id}_progress.json"


def _legacy_md(agent_dir: Path) -> Path:
    return agent_dir / "state" / "progress.md"


def _coerce(obj) -> dict[str, list]:
    """Normalise a parsed JSON blob into {date: [ {time, what}, ... ]}. Tolerant of
    hand-edits: drops malformed days/entries rather than raising, since this feeds a
    live agent's preamble and must never hard-fail on a stray comma."""
    out: dict[str, list] = {}
    if not isinstance(obj, dict):
        return out
    for date, entries in obj.items():
        if not _DATE_RE.match(str(date)) or not isinstance(entries, list):
            continue
        clean = []
        for e in entries:
            if isinstance(e, dict) and (e.get("what") or e.get("time")):
                clean.append({"time": str(e.get("time") or ""), "what": str(e.get("what") or "")})
            elif isinstance(e, str) and e.strip():
                clean.append({"time": "", "what": e.strip()})
        if clean:
            out[str(date)] = clean
    return out


def _parse_md(md_path: Path) -> dict[str, list]:
    """Fallback: turn the legacy markdown log into the same date-keyed structure so a
    not-yet-migrated agent still gets a clean, correctly-ordered preamble."""
    try:
        return _parse_md_text(md_path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return {}


def read(agent_dir: Path) -> dict[str, list]:
    """The hot log as {date: [entries]}. JSON first; legacy markdown if JSON absent."""
    hp = _hot_path(agent_dir)
    if hp.is_file():
        try:
            return _coerce(json.loads(hp.read_text(encoding="utf-8", errors="replace")))
        except (ValueError, OSError):
            return {}
    md = _legacy_md(agent_dir)
    return _parse_md(md) if md.is_file() else {}


def recent_text(agent_dir: Path, keep_days: int = 2, max_chars: int = 3000) -> str:
    """Render the newest `keep_days` days as a compact block for the cold-start preamble.
    Newest day first, newest entry first within a day — deterministic regardless of how
    the underlying file happens to be ordered."""
    data = read(agent_dir)
    if not data:
        return ""
    days = sorted(data.keys(), reverse=True)[:keep_days]
    out: list[str] = []
    for d in days:
        out.append(f"**{d}**")
        # entries newest-first by time string (HH:MM sorts lexicographically)
        for e in sorted(data[d], key=lambda x: x.get("time", ""), reverse=True):
            t = e.get("time")
            out.append(f"- {t + ' — ' if t else ''}{e.get('what', '')}")
    txt = "\n".join(out).strip()
    if len(txt) > max_chars:
        txt = txt[:max_chars].rstrip() + "\n[… older entries in this window truncated]"
    return txt


def _today() -> str:
    # main.py owns the injectable clock; this module is only called from there and from
    # the migration script, both of which run on the host — a bare date is fine here.
    return datetime.now().strftime("%Y-%m-%d")


def _split_md(md_text: str) -> tuple[str, list[tuple[str, str]]]:
    """Split a legacy progress.md into (preface, [(date, raw_block)]). The preface is
    everything before the first dated `## ` section (title line, notes); each block is
    the section's raw text VERBATIM, so a rewrite preserves the agent's own formatting
    byte-for-byte for the days it keeps."""
    lines = md_text.splitlines(keepends=True)
    preface: list[str] = []
    blocks: list[tuple[str, list[str]]] = []
    cur_date = None
    for ln in lines:
        m = _MD_SECTION.match(ln.rstrip("\n"))
        if m:
            cur_date = m.group(1)
            blocks.append((cur_date, [ln]))
        elif cur_date is None:
            preface.append(ln)
        else:
            blocks[-1][1].append(ln)
    return "".join(preface), [(d, "".join(b)) for d, b in blocks]


def _keep_cutoff(dates, keep_days: int) -> Optional[str]:
    """The keep window is the newest `keep_days` dates PRESENT in the log — not a
    calendar window. A calendar cutoff empties the whole file for an agent that has
    been idle a few days (observed: BOSS, newest day 07-12, 48h cutoff → kept nothing,
    total amnesia on next cold start). Anchoring on the dates that exist means every
    agent always retains its most recent activity, however old."""
    uniq = sorted(set(dates), reverse=True)
    if len(uniq) <= keep_days:
        return None            # nothing older than the keep window exists
    return uniq[keep_days - 1]  # keep dates >= this


def needs_rotation(agent_dir: Path, keep_days: int = 2) -> bool:
    if _hot_path(agent_dir).is_file():
        data = read(agent_dir)
        return _keep_cutoff(data.keys(), keep_days) is not None
    md = _legacy_md(agent_dir)
    if md.is_file():
        try:
            _, blocks = _split_md(md.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return False
        return _keep_cutoff([d for d, _ in blocks], keep_days) is not None
    return False


def rotate(agent_dir: Path, agent_id: str, keep_days: int = 2) -> Optional[dict]:
    """Move days older than the keep window from the hot file into the single archive
    file. Returns a small summary dict when it moved anything, else None.

    Merge-then-write, never delete content: archived days are appended to whatever the
    archive already holds. The hot file is rewritten atomically (temp + replace) so a
    crash mid-rotate cannot leave a truncated progress.json in front of a live agent.
    """
    hp = _hot_path(agent_dir)
    if not hp.is_file():
        # No JSON: the agent still writes the legacy markdown log — rotate THAT.
        # Agents keep appending markdown (an LLM appends one `## date time — what`
        # line reliably; making it rewrite a whole JSON file invites one bad comma
        # silently dropping a day). Structure is the UI's job: old days get parsed
        # into the archive JSON, the md is rewritten with only the keep-window days,
        # each kept section byte-identical to what the agent wrote.
        return _rotate_md(agent_dir, agent_id, keep_days)
    data = read(agent_dir)
    if not data:
        return None
    cutoff = _keep_cutoff(data.keys(), keep_days)
    if cutoff is None:
        return None
    old = {d: data[d] for d in data if d < cutoff}
    if not old:
        return None
    keep = {d: data[d] for d in data if d >= cutoff}

    ap = _archive_path(agent_dir, agent_id)
    ap.parent.mkdir(parents=True, exist_ok=True)
    archive: dict[str, list] = {}
    if ap.is_file():
        try:
            archive = _coerce(json.loads(ap.read_text(encoding="utf-8", errors="replace")))
        except (ValueError, OSError):
            archive = {}
    for d, entries in old.items():
        archive.setdefault(d, [])
        # de-dupe on (time, what) so a re-run cannot double-append the same day
        seen = {(e.get("time"), e.get("what")) for e in archive[d]}
        for e in entries:
            if (e.get("time"), e.get("what")) not in seen:
                archive[d].append(e)

    _atomic_write_json(ap, dict(sorted(archive.items(), reverse=True)))
    _atomic_write_json(hp, dict(sorted(keep.items(), reverse=True)))
    return {"archived_days": sorted(old.keys()), "kept_days": sorted(keep.keys(), reverse=True)}


def _rotate_md(agent_dir: Path, agent_id: str, keep_days: int) -> Optional[dict]:
    """Markdown leg of rotate(): trim progress.md to the keep window; archive the
    parsed entries of older days into the single per-agent archive JSON.

    Safety order matters: (1) one-time full backup of the md, (2) archive write,
    (3) md rewrite — each atomic. A crash between (2) and (3) leaves duplicate data
    (old days in BOTH md and archive), which the archive-side de-dupe absorbs on the
    next pass; no ordering can lose data."""
    md = _legacy_md(agent_dir)
    if not md.is_file():
        return None
    try:
        text = md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    preface, blocks = _split_md(text)
    cutoff = _keep_cutoff([d for d, _ in blocks], keep_days)
    if cutoff is None:
        return None
    old_blocks = [(d, b) for d, b in blocks if d < cutoff]
    if not old_blocks:
        return None
    keep_blocks = [(d, b) for d, b in blocks if d >= cutoff]

    # (1) one-time full backup, never overwritten — the escape hatch if parsing ever
    # mangles something. Disk-cheap relative to what rotation saves.
    bak = _archive_path(agent_dir, agent_id).parent / f"{agent_id}_progress_backup.md"
    bak.parent.mkdir(parents=True, exist_ok=True)
    if not bak.is_file():
        bak.write_text(text, encoding="utf-8")

    # (2) merge parsed old days into the archive JSON (same de-dupe as the JSON leg).
    parsed_old: dict[str, list] = {}
    for d, block in old_blocks:
        for date, entries in _parse_md_text(block).items():
            parsed_old.setdefault(date, []).extend(entries)
    ap = _archive_path(agent_dir, agent_id)
    archive: dict[str, list] = {}
    if ap.is_file():
        try:
            archive = _coerce(json.loads(ap.read_text(encoding="utf-8", errors="replace")))
        except (ValueError, OSError):
            archive = {}
    for d, entries in parsed_old.items():
        archive.setdefault(d, [])
        seen = {(e.get("time"), e.get("what")) for e in archive[d]}
        for e in entries:
            if (e.get("time"), e.get("what")) not in seen:
                archive[d].append(e)
    _atomic_write_json(ap, dict(sorted(archive.items(), reverse=True)))

    # (3) rewrite the md with only the keep-window sections, original order and bytes.
    new_md = preface + "".join(b for _, b in keep_blocks)
    tmp = md.with_suffix(".md.tmp")
    tmp.write_text(new_md, encoding="utf-8")
    tmp.replace(md)
    return {
        "archived_days": sorted({d for d, _ in old_blocks}),
        "kept_days": sorted({d for d, _ in keep_blocks}, reverse=True),
        "format": "md",
    }


def _parse_md_text(text: str) -> dict[str, list]:
    """_parse_md over a string instead of a file path."""
    out: dict[str, list] = {}
    cur_date = cur_time = None
    buf: list[str] = []

    def flush():
        if cur_date:
            what = " ".join(x.strip() for x in buf if x.strip())[:600]
            out.setdefault(cur_date, []).append({"time": cur_time or "", "what": what})

    for ln in text.splitlines():
        m = _MD_SECTION.match(ln)
        if m:
            flush()
            cur_date, cur_time, head = m.group(1), m.group(2), (m.group(3) or "").strip()
            buf = [head] if head else []
        elif cur_date:
            buf.append(ln)
    flush()
    return out


# ---------------- findings.md (VERIFIER-style audit log) ----------------
# Same treatment as progress, different anatomy: blocks head with `## F-### — title`
# (no date in the heading); the date lives inside as a `- **date**: YYYY-MM-DD` line.

_ANY_SECTION = re.compile(r"^##\s+")
_INLINE_DATE = re.compile(r"^\s*-\s*\*\*date\*\*:\s*(\d{4}-\d{2}-\d{2})", re.M)


def _findings_path(agent_dir: Path) -> Path:
    return agent_dir / "state" / "findings.md"


def _split_findings(text: str) -> tuple[str, list[tuple[Optional[str], str]]]:
    """(preface, [(date_or_None, raw_block)]). Date comes from the block's own
    `- **date**:` line; a block without one gets None and is always KEPT — never
    archive what can't be dated."""
    lines = text.splitlines(keepends=True)
    preface: list[str] = []
    blocks: list[list[str]] = []
    started = False
    for ln in lines:
        if _ANY_SECTION.match(ln):
            started = True
            blocks.append([ln])
        elif not started:
            preface.append(ln)
        else:
            blocks[-1].append(ln)
    out = []
    for b in blocks:
        raw = "".join(b)
        m = _INLINE_DATE.search(raw)
        out.append((m.group(1) if m else None, raw))
    return "".join(preface), out


def rotate_findings(agent_dir: Path, agent_id: str, keep_days: int = 2) -> Optional[dict]:
    """Trim findings.md to the newest `keep_days` dated days present (same policy as
    progress); archive older blocks — full raw text, nothing summarized away — into
    state/archive/<AGENT>_findings.json. Undated blocks are always kept."""
    fp = _findings_path(agent_dir)
    if not fp.is_file():
        return None
    try:
        text = fp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    preface, blocks = _split_findings(text)
    dated = [d for d, _ in blocks if d]
    cutoff = _keep_cutoff(dated, keep_days)
    if cutoff is None:
        return None
    old = [(d, b) for d, b in blocks if d and d < cutoff]
    if not old:
        return None
    keep = [(d, b) for d, b in blocks if d is None or d >= cutoff]

    bak = _archive_path(agent_dir, agent_id).parent / f"{agent_id}_findings_backup.md"
    bak.parent.mkdir(parents=True, exist_ok=True)
    if not bak.is_file():
        bak.write_text(text, encoding="utf-8")

    ap = _archive_path(agent_dir, agent_id).parent / f"{agent_id}_findings.json"
    archive: dict[str, list] = {}
    if ap.is_file():
        try:
            archive = json.loads(ap.read_text(encoding="utf-8", errors="replace"))
            if not isinstance(archive, dict):
                archive = {}
        except (ValueError, OSError):
            archive = {}
    for d, raw in old:
        day = archive.setdefault(d, [])
        heading = raw.splitlines()[0].strip() if raw else ""
        if not any(isinstance(e, dict) and e.get("heading") == heading for e in day):
            day.append({"heading": heading, "body": raw})
    _atomic_write_json(ap, dict(sorted(archive.items(), reverse=True)))

    tmp = fp.with_suffix(".md.tmp")
    tmp.write_text(preface + "".join(b for _, b in keep), encoding="utf-8")
    tmp.replace(fp)
    return {
        "archived_days": sorted({d for d, _ in old}),
        "kept_days": sorted({d for d, _ in keep if d}, reverse=True),
        "format": "findings",
    }


def _atomic_write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def migrate_md_to_json(agent_dir: Path) -> Optional[int]:
    """One-time: build progress.json from the legacy progress.md, leaving the .md in
    place as a backup. Returns the number of days migrated, or None if there is nothing
    to do (no md, or json already exists)."""
    if _hot_path(agent_dir).is_file():
        return None
    md = _legacy_md(agent_dir)
    if not md.is_file():
        return None
    data = _parse_md(md)
    if not data:
        return None
    _hot_path(agent_dir).parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(_hot_path(agent_dir), dict(sorted(data.items(), reverse=True)))
    return len(data)
