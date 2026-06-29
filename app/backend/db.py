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
                deepseek_model TEXT,
                effort TEXT,
                updated_at REAL NOT NULL,
                PRIMARY KEY (project_slug, agent_id)
            );
            CREATE TABLE IF NOT EXISTS dispatch_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_slug TEXT NOT NULL,
                source_agent TEXT NOT NULL,
                target_agent TEXT NOT NULL,
                task TEXT NOT NULL,
                result_text TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('ok','error','cancelled')),
                completed_at REAL NOT NULL,
                consumed_at REAL,
                consumed_by_session TEXT,
                meta TEXT
            );
            CREATE TABLE IF NOT EXISTS node_positions (
                project_slug TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                x REAL NOT NULL,
                y REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (project_slug, agent_id)
            );
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_slug TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                prompt TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('interval','once','until')),
                interval_seconds INTEGER,
                until_goal TEXT,
                next_run_at REAL NOT NULL,
                last_run_at REAL,
                last_status TEXT,
                runs_done INTEGER NOT NULL DEFAULT 0,
                max_runs INTEGER,
                active INTEGER NOT NULL DEFAULT 1,
                origin TEXT NOT NULL DEFAULT 'user',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sched_due
                ON scheduled_tasks(active, next_run_at);
            CREATE INDEX IF NOT EXISTS idx_dr_source
                ON dispatch_results(project_slug, source_agent, consumed_at, completed_at);
            CREATE INDEX IF NOT EXISTS idx_sessions_proj_agent
                ON sessions(project_slug, agent_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(session_id, id);
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS context_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_slug TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                status TEXT,
                ctx_chars INTEGER NOT NULL,
                est_tokens INTEGER NOT NULL,
                input_tokens INTEGER,
                cache_read INTEGER,
                cache_creation INTEGER,
                output_tokens INTEGER,
                resumed INTEGER,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ctxlog_proj_agent
                ON context_log(project_slug, agent_id, created_at);
            CREATE TABLE IF NOT EXISTS dissent_flags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_slug TEXT NOT NULL,
                source_agent TEXT NOT NULL,
                against TEXT NOT NULL,
                reason TEXT,
                evidence TEXT,
                severity TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                verdict TEXT,
                resolved_by TEXT,
                resolution_reason TEXT,
                created_at REAL NOT NULL,
                resolved_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_dissent_open
                ON dissent_flags(project_slug, status);
            CREATE TABLE IF NOT EXISTS halt_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_slug TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                reason TEXT,
                evidence TEXT,
                recommendation TEXT,
                confidence TEXT,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS verify_watermark (
                project_slug TEXT NOT NULL,
                auditor TEXT NOT NULL,
                producer TEXT NOT NULL,
                verified_version TEXT,
                verified_at REAL NOT NULL,
                PRIMARY KEY (project_slug, auditor, producer)
            );
            """
        )
        # backward-compat: add grok_model column if older db
        _ensure_column(c, "agent_overrides", "grok_model", "TEXT")
        _ensure_column(c, "agent_overrides", "deepseek_model", "TEXT")
        # latest real token usage from the CLI (JSON: input/output/cache buckets)
        _ensure_column(c, "sessions", "usage", "TEXT")
        # one-time context seed (compact recap) prepended to this session's next turn
        _ensure_column(c, "sessions", "seed", "TEXT")


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


def set_session_usage(session_id: str, usage: dict) -> None:
    """Persist the latest real token usage reported by the CLI for this session."""
    with _conn() as c:
        c.execute(
            "UPDATE sessions SET usage=? WHERE id=?",
            (json.dumps(usage), session_id),
        )


def set_session_seed(session_id: str, text: str) -> None:
    """Store a one-time context recap (from /compact) to prepend to this session's
    next turn, then it is cleared. Lets a fresh session continue with small context."""
    with _conn() as c:
        c.execute("UPDATE sessions SET seed=? WHERE id=?", (text, session_id))


def clear_session_seed(session_id: str) -> None:
    with _conn() as c:
        c.execute("UPDATE sessions SET seed=NULL WHERE id=?", (session_id,))


def get_agent_override(project_slug: str, agent_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT claude_model, grok_model, deepseek_model, effort FROM agent_overrides "
            "WHERE project_slug=? AND agent_id=?",
            (project_slug, agent_id),
        ).fetchone()
    if not row:
        return None
    return {
        "claude_model": row["claude_model"],
        "grok_model": row["grok_model"],
        "deepseek_model": row["deepseek_model"],
        "effort": row["effort"],
    }


def set_agent_override(project_slug: str, agent_id: str,
                       claude_model: str | None,
                       grok_model: str | None,
                       deepseek_model: str | None,
                       effort: str | None) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO agent_overrides(project_slug, agent_id, claude_model, grok_model, deepseek_model, effort, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(project_slug, agent_id) DO UPDATE SET "
            "claude_model=excluded.claude_model, grok_model=excluded.grok_model, "
            "deepseek_model=excluded.deepseek_model, "
            "effort=excluded.effort, updated_at=excluded.updated_at",
            (project_slug, agent_id, claude_model, grok_model, deepseek_model, effort, time.time()),
        )


def list_agent_overrides(project_slug: str) -> dict[str, dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT agent_id, claude_model, grok_model, deepseek_model, effort FROM agent_overrides WHERE project_slug=?",
            (project_slug,),
        ).fetchall()
    return {r["agent_id"]: {
        "claude_model": r["claude_model"],
        "grok_model": r["grok_model"],
        "deepseek_model": r["deepseek_model"],
        "effort": r["effort"],
    } for r in rows}


def get_node_positions(project_slug: str) -> dict[str, dict]:
    """Saved graph positions for this project. Layout source of truth: a node
    present here was placed by hand (or by an explicit re-layout) and must
    never be silently moved by auto-layout."""
    with _conn() as c:
        rows = c.execute(
            "SELECT agent_id, x, y FROM node_positions WHERE project_slug=?",
            (project_slug,),
        ).fetchall()
    return {r["agent_id"]: {"x": r["x"], "y": r["y"]} for r in rows}


def set_node_positions(project_slug: str, positions: dict[str, dict]) -> None:
    now = time.time()
    with _conn() as c:
        for agent_id, p in positions.items():
            c.execute(
                "INSERT INTO node_positions(project_slug, agent_id, x, y, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(project_slug, agent_id) DO UPDATE SET "
                "x=excluded.x, y=excluded.y, updated_at=excluded.updated_at",
                (project_slug, agent_id, float(p["x"]), float(p["y"]), now),
            )


def clear_node_positions(project_slug: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM node_positions WHERE project_slug=?", (project_slug,))


def record_dispatch_result(
    project_slug: str,
    source_agent: str,
    target_agent: str,
    task: str,
    result_text: str,
    status: str,
    meta: dict | None = None,
) -> int:
    """Append a row to the dispatch_results ledger so the SOURCE agent can see
    this worker's output in its next prompt (via get_unconsumed_results +
    enrichment in _run_agent).
    """
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO dispatch_results(project_slug, source_agent, target_agent, "
            "task, result_text, status, completed_at, meta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (project_slug, source_agent, target_agent, task, result_text or "",
             status, time.time(),
             json.dumps(meta) if meta else None),
        )
        return cur.lastrowid


def get_unconsumed_results(project_slug: str, for_source_agent: str) -> list[dict]:
    """Return dispatch results for which `for_source_agent` is the source and
    nobody has consumed them yet. These are the worker outputs the source agent
    has NOT yet seen in any of its own prompts.
    """
    with _conn() as c:
        rows = c.execute(
            "SELECT id, project_slug, source_agent, target_agent, task, "
            "result_text, status, completed_at, meta "
            "FROM dispatch_results "
            "WHERE project_slug=? AND source_agent=? AND consumed_at IS NULL "
            "ORDER BY completed_at ASC",
            (project_slug, for_source_agent),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("meta"):
            try:
                d["meta"] = json.loads(d["meta"])
            except Exception:
                pass
        out.append(d)
    return out


def consume_results(result_ids: list[int], session_id: str) -> None:
    if not result_ids:
        return
    placeholders = ",".join("?" * len(result_ids))
    with _conn() as c:
        c.execute(
            f"UPDATE dispatch_results SET consumed_at=?, consumed_by_session=? "
            f"WHERE id IN ({placeholders})",
            (time.time(), session_id, *result_ids),
        )


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


# ---------------------------------------------------------------------------
# Scheduler — recurring / deferred / goal-driven re-invocation of an agent turn.
# A fire goes through the same _Run/_run_agent path as a normal /chat. See
# docs/scheduler-spec.md.
# ---------------------------------------------------------------------------

def create_scheduled_task(
    project_slug: str,
    agent_id: str,
    prompt: str,
    kind: str,
    interval_seconds: int | None,
    next_run_at: float,
    *,
    until_goal: str | None = None,
    max_runs: int | None = None,
    origin: str = "user",
) -> dict:
    now = time.time()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO scheduled_tasks(project_slug, agent_id, prompt, kind, "
            "interval_seconds, until_goal, next_run_at, runs_done, max_runs, "
            "active, origin, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 1, ?, ?, ?)",
            (project_slug, agent_id, prompt, kind, interval_seconds, until_goal,
             next_run_at, max_runs, origin, now, now),
        )
        row = c.execute("SELECT * FROM scheduled_tasks WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)


def list_scheduled_tasks(project_slug: str, include_inactive: bool = True) -> list[dict]:
    q = "SELECT * FROM scheduled_tasks WHERE project_slug=?"
    if not include_inactive:
        q += " AND active=1"
    q += " ORDER BY active DESC, next_run_at ASC"
    with _conn() as c:
        rows = c.execute(q, (project_slug,)).fetchall()
    return [dict(r) for r in rows]


def get_scheduled_task(task_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM scheduled_tasks WHERE id=?", (task_id,)).fetchone()
    return dict(row) if row else None


def get_due_scheduled_tasks(now: float | None = None) -> list[dict]:
    """Active schedules whose next fire time has arrived."""
    now = time.time() if now is None else now
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM scheduled_tasks WHERE active=1 AND next_run_at<=? "
            "ORDER BY next_run_at ASC",
            (now,),
        ).fetchall()
    return [dict(r) for r in rows]


def record_scheduled_fire(task_id: int, next_run_at: float | None, status: str) -> dict | None:
    """After a fire completes: bump runs_done, stamp last_run_at/last_status, set
    the next fire time (None = no further fire, deactivate)."""
    now = time.time()
    with _conn() as c:
        if next_run_at is None:
            c.execute(
                "UPDATE scheduled_tasks SET runs_done=runs_done+1, last_run_at=?, "
                "last_status=?, active=0, next_run_at=?, updated_at=? WHERE id=?",
                (now, status, now, now, task_id),
            )
        else:
            c.execute(
                "UPDATE scheduled_tasks SET runs_done=runs_done+1, last_run_at=?, "
                "last_status=?, next_run_at=?, updated_at=? WHERE id=?",
                (now, status, next_run_at, now, task_id),
            )
        row = c.execute("SELECT * FROM scheduled_tasks WHERE id=?", (task_id,)).fetchone()
    return dict(row) if row else None


def defer_scheduled_task(task_id: int, next_run_at: float) -> None:
    """Push the next fire time forward without counting a run (skip-on-busy)."""
    with _conn() as c:
        c.execute(
            "UPDATE scheduled_tasks SET next_run_at=?, updated_at=? WHERE id=?",
            (next_run_at, time.time(), task_id),
        )


def set_scheduled_active(task_id: int, active: bool) -> dict | None:
    with _conn() as c:
        c.execute(
            "UPDATE scheduled_tasks SET active=?, updated_at=? WHERE id=?",
            (1 if active else 0, time.time(), task_id),
        )
        row = c.execute("SELECT * FROM scheduled_tasks WHERE id=?", (task_id,)).fetchone()
    return dict(row) if row else None


def deactivate_agent_schedules(project_slug: str, agent_id: str, kind: str | None = None,
                               created_before: float | None = None) -> list[int]:
    """Deactivate this agent's active schedules (optionally only one kind). Used by
    <schedule_stop> so a goal-loop can self-terminate. Returns deactivated ids.

    `created_before` guards the same-turn-echo pitfall: an agent that EXPLAINS or
    quotes `<schedule_stop>` (e.g. echoing the example from its instructions) in the
    very message that also CREATES a schedule must not instantly kill it. Passing the
    turn's start time means only schedules that pre-date this turn are stopped."""
    q = "SELECT id FROM scheduled_tasks WHERE project_slug=? AND agent_id=? AND active=1"
    params: list = [project_slug, agent_id]
    if kind:
        q += " AND kind=?"
        params.append(kind)
    if created_before is not None:
        q += " AND created_at < ?"
        params.append(created_before)
    with _conn() as c:
        ids = [r["id"] for r in c.execute(q, params).fetchall()]
        if ids:
            placeholders = ",".join("?" * len(ids))
            c.execute(
                f"UPDATE scheduled_tasks SET active=0, updated_at=? WHERE id IN ({placeholders})",
                (time.time(), *ids),
            )
    return ids


def delete_scheduled_task(task_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM scheduled_tasks WHERE id=?", (task_id,))


# ---------------------------------------------------------------------------
# Global settings (key-value). Used for scheduler on/off etc.
# ---------------------------------------------------------------------------

def get_setting(key: str, default: str = "1") -> str:
    with _conn() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

def set_setting(key: str, value: str) -> None:
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# ---------------------------------------------------------------------------
# Per-turn context log (feature C) — passive measurement, ~free. One row per
# model turn: the constructed-context size (system_prompt + enriched message)
# plus the real token usage. Lets you A/B overview-mode vs manifest-mode on the
# constructed context, free of the prompt-cache confound. Gated by
# settings.context_log_enabled (default "1").
# ---------------------------------------------------------------------------

def add_context_log(project_slug: str, agent_id: str, status: str | None,
                    ctx_chars: int, est_tokens: int,
                    input_tokens=0, cache_read=0, cache_creation=0,
                    output_tokens=0, resumed: bool = False) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO context_log(project_slug, agent_id, status, ctx_chars, est_tokens, "
            "input_tokens, cache_read, cache_creation, output_tokens, resumed, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (project_slug, agent_id, status, int(ctx_chars), int(est_tokens),
             int(input_tokens or 0), int(cache_read or 0), int(cache_creation or 0),
             int(output_tokens or 0), 1 if resumed else 0, time.time()),
        )


# ---------------------------------------------------------------------------
# Dissent flags (spec §15.2) + halt log (spec §15.1) — agency enforcement state.
# Dissent is the HARD half: a blocking-flag the orchestrator cannot silently
# bypass (gated in main._run_agent). Halt-log is an audit trail (halt-rate).
# ---------------------------------------------------------------------------

def add_dissent_flag(project_slug: str, source_agent: str, against: str,
                     reason: str = "", evidence: str = "", severity: str = "blocking") -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO dissent_flags(project_slug, source_agent, against, reason, evidence, "
            "severity, status, created_at) VALUES (?,?,?,?,?,?,'open',?)",
            (project_slug, source_agent, against, reason, evidence, severity, time.time()),
        )


def get_open_dissents(project_slug: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM dissent_flags WHERE project_slug=? AND status='open' ORDER BY created_at",
            (project_slug,),
        ).fetchall()
    return [dict(r) for r in rows]


def resolve_dissent(project_slug: str, against: str, verdict: str,
                    resolved_by: str, resolution_reason: str = "") -> int:
    """Mark open dissents matching `against` resolved. Returns rows affected."""
    with _conn() as c:
        cur = c.execute(
            "UPDATE dissent_flags SET status='resolved', verdict=?, resolved_by=?, "
            "resolution_reason=?, resolved_at=? WHERE project_slug=? AND against=? AND status='open'",
            (verdict, resolved_by, resolution_reason, time.time(), project_slug, against),
        )
        return cur.rowcount


def add_halt_log(project_slug: str, agent_id: str, reason: str = "", evidence: str = "",
                 recommendation: str = "", confidence: str = "") -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO halt_log(project_slug, agent_id, reason, evidence, recommendation, "
            "confidence, created_at) VALUES (?,?,?,?,?,?,?)",
            (project_slug, agent_id, reason, evidence, recommendation, confidence, time.time()),
        )


# ---------------------------------------------------------------------------
# Verify watermark (spec §16.1) — incremental verification. An auditor declares
# `verified: PROD@ver` in its [RESULT]; control-plane stores it, then on the next
# audit pass computes the delta (producer version unchanged → SKIP re-verify).
# Turns O(N per change) re-audit into O(Δ). The version-compare is deterministic
# (hard); the judgment "is it real" stays the auditor's (soft).
# ---------------------------------------------------------------------------

def set_verify_watermark(project_slug: str, auditor: str, producer: str, verified_version: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO verify_watermark(project_slug, auditor, producer, verified_version, verified_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(project_slug, auditor, producer) "
            "DO UPDATE SET verified_version=excluded.verified_version, verified_at=excluded.verified_at",
            (project_slug, auditor, producer, verified_version, time.time()),
        )


def get_verify_watermarks(project_slug: str, auditor: str) -> dict:
    with _conn() as c:
        rows = c.execute(
            "SELECT producer, verified_version FROM verify_watermark WHERE project_slug=? AND auditor=?",
            (project_slug, auditor),
        ).fetchall()
    return {r["producer"]: r["verified_version"] for r in rows}


def context_log_report(project_slug: str | None = None) -> list[dict]:
    """Per-(project, agent) aggregate over the context log: turn count, average
    constructed-context tokens/chars, average cache_creation + output, and how
    many turns were cold-start (resumed=0 → the ones that get the overview
    injection). Ordered by heaviest constructed context first."""
    where = "WHERE project_slug=?" if project_slug else ""
    args = (project_slug,) if project_slug else ()
    with _conn() as c:
        rows = c.execute(
            f"""SELECT project_slug, agent_id,
                       COUNT(*)              AS turns,
                       ROUND(AVG(est_tokens)) AS avg_ctx_tok,
                       ROUND(AVG(ctx_chars))  AS avg_ctx_chars,
                       ROUND(AVG(cache_creation)) AS avg_cache_cr,
                       ROUND(AVG(output_tokens))  AS avg_out,
                       SUM(CASE WHEN resumed=0 THEN 1 ELSE 0 END) AS coldstart_turns
                FROM context_log {where}
                GROUP BY project_slug, agent_id
                ORDER BY avg_ctx_tok DESC""",
            args,
        ).fetchall()
    return [dict(r) for r in rows]
