from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "agentui.db"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _ensure_column(c, table: str, column: str, decl: str) -> None:
    cols = [r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                project_slug TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                claude_session_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_status TEXT
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                meta TEXT,
                FOREIGN KEY(session_id) REFERENCES sessions(id)
            );
            CREATE TABLE IF NOT EXISTS agent_overrides (
                project_slug TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                claude_model TEXT,
                grok_model TEXT,
                effort TEXT,
                updated_at REAL NOT NULL,
                PRIMARY KEY (project_slug, agent_id)
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_proj_agent
                ON sessions(project_slug, agent_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(session_id, id);
            """
        )
        # backward-compat: add grok_model column if older db
        _ensure_column(c, "agent_overrides", "grok_model", "TEXT")


def get_or_create_active_session(project_slug: str, agent_id: str) -> dict:
    now = time.time()
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM sessions WHERE project_slug=? AND agent_id=? "
            "ORDER BY updated_at DESC LIMIT 1",
            (project_slug, agent_id),
        ).fetchone()
        if row:
            return dict(row)
        sid = str(uuid.uuid4())
        c.execute(
            "INSERT INTO sessions(id, project_slug, agent_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, project_slug, agent_id, now, now),
        )
        return {
            "id": sid,
            "project_slug": project_slug,
            "agent_id": agent_id,
            "claude_session_id": None,
            "created_at": now,
            "updated_at": now,
            "last_status": None,
        }


def new_session(project_slug: str, agent_id: str) -> dict:
    now = time.time()
    sid = str(uuid.uuid4())
    with _conn() as c:
        c.execute(
            "INSERT INTO sessions(id, project_slug, agent_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, project_slug, agent_id, now, now),
        )
    return {
        "id": sid, "project_slug": project_slug, "agent_id": agent_id,
        "claude_session_id": None, "created_at": now, "updated_at": now,
        "last_status": None,
    }


def list_sessions(project_slug: str, agent_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM sessions WHERE project_slug=? AND agent_id=? "
            "ORDER BY updated_at DESC",
            (project_slug, agent_id),
        ).fetchall()
    return [dict(r) for r in rows]


def get_messages(session_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, role, content, created_at, meta FROM messages "
            "WHERE session_id=? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "role": r["role"],
            "content": r["content"],
            "created_at": r["created_at"],
            "meta": json.loads(r["meta"]) if r["meta"] else None,
        }
        for r in rows
    ]


def add_message(session_id: str, role: str, content: str, meta: dict | None = None) -> int:
    now = time.time()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO messages(session_id, role, content, created_at, meta) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, role, content, now, json.dumps(meta) if meta else None),
        )
        c.execute(
            "UPDATE sessions SET updated_at=? WHERE id=?",
            (now, session_id),
        )
        return cur.lastrowid


def update_session_status(session_id: str, status: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE sessions SET last_status=?, updated_at=? WHERE id=?",
            (status, time.time(), session_id),
        )


def set_claude_session_id(session_id: str, claude_session_id: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE sessions SET claude_session_id=? WHERE id=?",
            (claude_session_id, session_id),
        )


def get_agent_override(project_slug: str, agent_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT claude_model, grok_model, effort FROM agent_overrides "
            "WHERE project_slug=? AND agent_id=?",
            (project_slug, agent_id),
        ).fetchone()
    if not row:
        return None
    return {
        "claude_model": row["claude_model"],
        "grok_model": row["grok_model"],
        "effort": row["effort"],
    }


def set_agent_override(project_slug: str, agent_id: str,
                       claude_model: str | None,
                       grok_model: str | None,
                       effort: str | None) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO agent_overrides(project_slug, agent_id, claude_model, grok_model, effort, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(project_slug, agent_id) DO UPDATE SET "
            "claude_model=excluded.claude_model, grok_model=excluded.grok_model, "
            "effort=excluded.effort, updated_at=excluded.updated_at",
            (project_slug, agent_id, claude_model, grok_model, effort, time.time()),
        )


def list_agent_overrides(project_slug: str) -> dict[str, dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT agent_id, claude_model, grok_model, effort FROM agent_overrides WHERE project_slug=?",
            (project_slug,),
        ).fetchall()
    return {r["agent_id"]: {
        "claude_model": r["claude_model"],
        "grok_model": r["grok_model"],
        "effort": r["effort"],
    } for r in rows}


def cleanup_stale_running() -> int:
    """Mark any sessions that the previous process left in 'running' as 'cancelled'.

    Called at startup. If the previous process died mid-stream (uvicorn reload,
    browser disconnect that killed the task, OS kill), `running` sessions are
    orphaned with no way to ever finish.
    """
    with _conn() as c:
        cur = c.execute(
            "UPDATE sessions SET last_status='cancelled', updated_at=? "
            "WHERE last_status='running'",
            (time.time(),),
        )
        return cur.rowcount


def get_last_status(project_slug: str, agent_id: str) -> str | None:
    with _conn() as c:
        row = c.execute(
            "SELECT last_status FROM sessions WHERE project_slug=? AND agent_id=? "
            "ORDER BY updated_at DESC LIMIT 1",
            (project_slug, agent_id),
        ).fetchone()
    return row["last_status"] if row else None
