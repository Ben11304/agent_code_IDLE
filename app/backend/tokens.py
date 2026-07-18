"""Exact token accounting, read from the CLI's own transcript.

The `claude` CLI writes every turn to ~/.claude/projects/<cwd-slug>/<session_id>.jsonl,
and each assistant line carries the `usage` blob the SERVER returned for that single API
request: input_tokens + cache_creation_input_tokens + cache_read_input_tokens +
output_tokens.

Why this and not `client.messages.count_tokens`:
  - count_tokens only counts what YOU hand it (messages + system + tools). It is blind to
    everything the CLI injects on its own — CLAUDE.md, skill preambles, system-reminders,
    tool schemas, and the tool RESULTS that dominate an agentic turn. It would under-count
    heavily and silently.
  - It needs an ANTHROPIC_API_KEY, which the Claude nodes here deliberately do not have
    (subscription-only by design), and it costs an extra API round-trip per estimate.
The transcript numbers are the server's own accounting: free, offline, exact, and they
cover the glm/deepseek nodes too (they drive the same CLI).

Two DIFFERENT quantities come out of one scan; conflating them is the bug this module
exists to prevent:
  - BILLING  = sum over requests. Grows without bound across an agentic turn (cache_read
               alone reached 7.6M in one observed session).
  - OCCUPANCY = max over requests of (input + cache_creation + cache_read). A single
               request can never exceed the context window, so this is what the context
               gauge and auto-compact must use.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def transcript_path(claude_session_id: str) -> Optional[Path]:
    """Locate a session's JSONL. The CLI derives the per-project folder name by slugifying
    the cwd, so rather than reimplement that mapping (and drift when it changes), glob for
    the session id — it is a uuid and unique across projects."""
    if not claude_session_id:
        return None
    hits = sorted(
        _PROJECTS_DIR.glob(f"*/{claude_session_id}.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return hits[0] if hits else None


_cache: dict[str, tuple[float, int, dict]] = {}   # session -> (mtime, size, result)
_last_scan: dict[str, float] = {}                 # session -> monotonic time of last real parse
_SCAN_TTL_S = 20.0


def scan(claude_session_id: str, force: bool = False) -> Optional[dict]:
    """Aggregate every API request of a session. Returns None when no transcript exists
    (grok nodes, or a session that has not completed a turn).

    Deduped on message.id: the CLI re-emits the same assistant line as the stream is
    assembled, so raw lines over-count (observed 130 lines for 67 real requests).

    Memoized on (mtime, size) because /stats calls this per agent on every graph refresh
    and a busy session's transcript runs to several MB — an unconditional re-parse would
    make the graph poll heavier than the agent turns it is reporting on.
    """
    path = transcript_path(claude_session_id)
    if not path:
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    hit = _cache.get(claude_session_id)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    # TTL floor on top of the mtime check: while the CLI is mid-turn it appends to the
    # file continuously, so mtime changes on every poll and the mtime cache never hits —
    # each /stats refresh would re-parse a transcript that can run to tens of MB over
    # NFS (observed 25 MB; on a loaded login node that starved the event loop, wedged
    # the PTY reader, and froze a live run). A ≤20s-stale result is fine for a gauge.
    # force=True bypasses the TTL (never the exact mtime/size cache above). BOOKING
    # must use it: the delta booked at turn-end/rotation is final — at rotation nothing
    # ever re-scans the old session id, so a ≤20s-stale scan there would silently drop
    # the last requests of the turn forever. The TTL exists only for the polling /stats
    # gauge, where a slightly stale number is harmless.
    now = time.monotonic()
    if not force and hit and now - _last_scan.get(claude_session_id, 0.0) < _SCAN_TTL_S:
        return hit[2]
    _last_scan[claude_session_id] = now

    by_id: dict[str, dict] = {}
    model = None
    try:
        with path.open(errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") != "assistant":
                    continue
                # Sidechain lines are subagent (Task tool) requests written into the same
                # transcript. Their context is the SUBAGENT's, not this session's — if one
                # is the file's last line, "current occupancy" would collapse to the
                # subagent's tiny context. The Agent tool is disabled in adapters.py so
                # none exist today (verified across 7 live transcripts), but this guard
                # keeps the numbers right if that ever changes. Excluded from billing too:
                # keep this session's ledger about this session.
                if d.get("isSidechain"):
                    continue
                m = d.get("message") or {}
                u = m.get("usage")
                mid = m.get("id")
                if not u or not mid:
                    continue
                by_id[mid] = u
                model = m.get("model") or model
    except OSError:
        return None

    if not by_id:
        return None

    def _i(u: dict, k: str) -> int:
        v = u.get(k)
        return v if isinstance(v, int) else 0

    inp = cc = cr = out = 0
    peak = 0
    last = 0
    for u in by_id.values():   # dict preserves insertion order → last = most recent request
        i, c, r, o = (_i(u, "input_tokens"), _i(u, "cache_creation_input_tokens"),
                      _i(u, "cache_read_input_tokens"), _i(u, "output_tokens"))
        inp += i
        cc += c
        cr += r
        out += o
        last = i + c + r
        peak = max(peak, last)

    result = {
        "requests": len(by_id),
        "input_tokens": inp,
        "cache_creation": cc,
        "cache_read": cr,
        "output_tokens": out,
        # Billing-side total. NOT context occupancy — see module docstring.
        "billed_total": inp + cc + cr + out,
        # CURRENT occupancy = the MOST RECENT request's input side. Deliberately not
        # max(): context does NOT grow monotonically within a session — the CLI drops
        # or compacts tool results on its own (observed: aecbench/RESEARCHER peaked at
        # 113,364 then dropped to 94,233). max() would report a peak the session has
        # already moved past, inflating the gauge and firing auto-compact early.
        "occupancy": last,
        # Highest point ever reached — informational only; never the gauge numerator.
        "peak": peak,
        "model": model,
        "path": str(path),
    }
    _cache[claude_session_id] = (st.st_mtime, st.st_size, result)
    return result


def scan_by_day(claude_session_id: str) -> list[dict]:
    """Per-HOUR aggregate of a session's requests, for historical backfill that must land
    in the right time bucket. Grouped by the local HOUR of each request's own timestamp,
    so the dashboard's hour/day/6h views are all correct — a day-granular backfill
    stamped everything at noon and left every sub-day view empty.

    (Name kept for its one caller; it is hourly now, not daily.) 15-min view still
    snaps historical rows to the hour — sub-hour precision only exists for live turns.

    Each row: {date (YYYY-MM-DD HH), epoch (start of that hour, local), requests,
    input_tokens, cache_creation, cache_read, output_tokens, occupancy, model}.
    """
    path = transcript_path(claude_session_id)
    if not path:
        return []
    # PASS 1 — dedupe GLOBALLY on message.id, exactly like scan(). The CLI re-emits the
    # same assistant message while the stream assembles (partial → final usage, output
    # growing each emit). Deduping per-bucket instead of globally double-counted any
    # message whose re-emissions crossed an hour boundary: measured, 8 of the 20 largest
    # sessions were inflated, the worst by +2.37M billed. Each mid gets exactly ONE
    # bucket — the hour of its LAST emission — carrying its FINAL usage, so the sum over
    # buckets equals scan()'s session totals by construction.
    latest: dict[str, tuple] = {}   # mid -> (usage, dt, model)
    try:
        with path.open(errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") != "assistant" or d.get("isSidechain"):
                    continue
                m = d.get("message") or {}
                u = m.get("usage")
                mid = m.get("id")
                ts = d.get("timestamp")
                if not u or not mid or not ts:
                    continue
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
                except ValueError:
                    continue
                prev = latest.get(mid)
                if prev is None or dt.timestamp() >= prev[1].timestamp():
                    latest[mid] = (u, dt, m.get("model"))
    except OSError:
        return []

    # PASS 2 — group the deduped requests into hourly buckets.
    days: dict[str, dict] = {}
    for mid, (u, dt, model) in latest.items():
        key = dt.strftime("%Y-%m-%d %H")
        b = days.setdefault(key, {"reqs": [], "model": None, "last": (0.0, None)})
        b["reqs"].append(u)
        if model:
            b["model"] = model
        if dt.timestamp() >= b["last"][0]:
            b["last"] = (dt.timestamp(), u)

    def _i(u, k):
        v = u.get(k)
        return v if isinstance(v, int) else 0

    out = []
    for key, b in sorted(days.items()):
        inp = cc = cr = op = 0
        for u in b["reqs"]:
            inp += _i(u, "input_tokens")
            cc += _i(u, "cache_creation_input_tokens")
            cr += _i(u, "cache_read_input_tokens")
            op += _i(u, "output_tokens")
        last_u = b["last"][1] or {}
        occ = _i(last_u, "input_tokens") + _i(last_u, "cache_creation_input_tokens") \
            + _i(last_u, "cache_read_input_tokens")
        # start of that hour, local — the created_at that lands the row in the right
        # hour/day/6h bucket. key is "YYYY-MM-DD HH".
        epoch = datetime.fromisoformat(key.replace(" ", "T") + ":00:00").timestamp()
        out.append({
            "date": key, "epoch": epoch, "requests": len(b["reqs"]),
            "input_tokens": inp, "cache_creation": cc, "cache_read": cr,
            "output_tokens": op, "occupancy": occ, "model": b["model"],
        })
    return out
