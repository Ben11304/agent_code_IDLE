"""Live tool-activity feed for one agent, tailed from the CLI transcript.

The transcript JSONL the CLI appends during a turn already records every tool_use
(name + input), every tool_result (ok/error), and the per-request usage. This module
tails that file from a byte offset — like `tail -f` — so the dashboard can poll for the
new events since its last read without re-parsing a multi-MB file each tick.

No streaming/PTY involvement: this only READS the transcript the CLI writes anyway.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from . import tokens

# Per tool, the one input field worth showing in a one-line log row.
_TARGET_FIELDS = ("file_path", "command", "pattern", "path", "url", "query",
                  "notebook_path", "prompt")


def _target(name: str, inp: dict) -> str:
    if not isinstance(inp, dict):
        return ""
    for f in _TARGET_FIELDS:
        if inp.get(f):
            return str(inp[f])
    # Task/Agent and friends: fall back to a short dump of the input.
    return json.dumps(inp, ensure_ascii=False)[:120] if inp else ""


# First poll (since<=0) seeds from at most this many trailing bytes, so opening the log
# on a 70 MB transcript ships the recent tail, not the whole history.
_SEED_BYTES = 256 * 1024


def tail(claude_session_id: str, since: int = 0) -> dict:
    """Return tool events appended after byte `since`, plus the new offset.

    Shape:
      { "events": [ {ts, kind, tool, target, tokens} ], "offset": <int>,
        "session": <csid>, "reset": <bool>, "seeded": <bool> }

    reset=True means the file shrank (a fresh session / rotation) — the caller should
    clear its table and start from the new offset. seeded=True means this was a
    tail-only first read (some earlier history was skipped).
    """
    path = tokens.transcript_path(claude_session_id)
    if not path:
        return {"events": [], "offset": since, "session": claude_session_id, "reset": False}
    try:
        size = path.stat().st_size
    except OSError:
        return {"events": [], "offset": since, "session": claude_session_id, "reset": False}

    reset = since > size            # file got smaller than our cursor → new content
    seeded = False
    if since <= 0 and size > _SEED_BYTES:
        start = size - _SEED_BYTES  # seed: only the trailing window on first open
        seeded = True
    else:
        start = 0 if reset else since
    events: list[dict] = []
    # Pair a tool_use to its result: the tool_result arrives in the NEXT user message,
    # keyed by tool_use_id. Buffer unresolved uses across lines within this read.
    pending: dict[str, dict] = {}
    try:
        with path.open("rb") as f:
            f.seek(start)
            raw = f.read()
            offset = start + len(raw)
    except OSError:
        return {"events": [], "offset": since, "session": claude_session_id, "reset": False}

    lines = raw.split(b"\n")
    if seeded and lines:
        lines = lines[1:]   # seek landed mid-line; drop the truncated first fragment

    for line in lines:
        if not line.strip():
            continue
        try:
            d = json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        # Keep the FULL ISO-8601 (incl. the trailing Z) so the frontend can parse it as
        # UTC and render in the viewer's own timezone — slicing it dropped the Z and made
        # the browser read UTC as local, so every row showed the wrong hour.
        ts = d.get("timestamp") or ""
        m = d.get("message") or {}
        typ = d.get("type")

        if typ == "assistant":
            usage = m.get("usage") or {}
            out = usage.get("output_tokens")
            cr = usage.get("cache_read_input_tokens")
            round_tok = None
            if out is not None or cr is not None:
                round_tok = (usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0) \
                    + (cr or 0) + (out or 0)
            for c in (m.get("content") or []):
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use":
                    ev = {
                        "ts": ts, "kind": "tool", "tool": c.get("name") or "?",
                        "target": _target(c.get("name") or "", c.get("input") or {}),
                        "status": "…", "id": c.get("id"),
                        "out_tokens": out, "round_tokens": round_tok,
                    }
                    events.append(ev)
                    if c.get("id"):
                        pending[c["id"]] = ev
                elif c.get("type") == "text" and c.get("text", "").strip():
                    # a short assistant note between tool calls — useful context
                    events.append({"ts": ts, "kind": "say", "tool": "",
                                   "target": c["text"].strip()[:160], "status": "",
                                   "out_tokens": out, "round_tokens": round_tok})
        elif typ == "user":
            for c in (m.get("content") or []):
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    tid = c.get("tool_use_id")
                    st = "err" if c.get("is_error") else "ok"
                    ev = pending.pop(tid, None)
                    if ev is not None:
                        ev["status"] = st          # same read window: resolve in place
                    elif tid:
                        # tool_use was emitted in an EARLIER poll (long-running tool):
                        # send a standalone result event so the frontend can update that
                        # row's status by id.
                        events.append({"ts": ts, "kind": "result", "id": tid, "status": st})

    return {"events": events, "offset": offset, "session": claude_session_id,
            "reset": reset, "seeded": seeded}
