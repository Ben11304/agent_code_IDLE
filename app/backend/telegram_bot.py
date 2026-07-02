"""Telegram channel — drive a single orchestrator agent (default BOSS) from chat.

This is a *second subscriber* to the same machinery the web UI uses. An incoming
authorized Telegram message becomes a normal agent turn via ``main._start_run``
(the shared entry POST /chat and the scheduler both use). We then attach an
``asyncio.Queue`` to the ``_Run.subscribers`` set and drain events exactly like
``main._run_subscriber_sse`` does — minus the SSE formatting — collecting the
root agent's text + dispatch notifications and streaming them back to the chat
via live message edits.

Nothing here touches the PTY contract, the dispatch ledger, the scheduler, or the
web UI. It is pure additive: absent ``TELEGRAM_BOT_TOKEN`` the whole module is a
no-op (``is_enabled()`` is False, ``start_telegram``/``stop_telegram`` return
immediately).

Config (env vars, loaded from ~/.env by run.sh — same pattern as DEEPSEEK_API_KEY):

  TELEGRAM_BOT_TOKEN   required to enable; the BotFather token.
  TELEGRAM_CHAT_IDS    comma-separated numeric chat/user ids on the allow-list.
                       Messages from anyone else are silently dropped. The bot
                       spends your Claude subscription and can dispatch real work,
                       so this MUST be locked to your account.
  TELEGRAM_AGENT_SLUG  project slug to drive (default "energy").
  TELEGRAM_AGENT_ID    agent id to drive (default "BOSS").
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("telegram_channel")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
_CHAT_IDS = {c.strip() for c in os.getenv("TELEGRAM_CHAT_IDS", "").split(",") if c.strip()}
_SLUG = os.getenv("TELEGRAM_AGENT_SLUG", "energy").strip() or "energy"
_AGENT_ID = os.getenv("TELEGRAM_AGENT_ID", "BOSS").strip() or "BOSS"

# Live-edit throttle: Telegram rate-limits message edits; ~1/s per message is
# the safe practical bound. Also caps the preview we resend each tick.
_EDIT_INTERVAL_S = 1.2
_PREVIEW_TAIL = 3500          # keep the last ~3500 chars in the edited message
_TG_MAX = 4096                # Telegram hard message limit

# Held so the Application isn't GC'd (its background tasks would die).
_app: Optional[object] = None
# Bot @username captured at startup (for the UI "connected to bot" badge).
_bot_username: Optional[str] = None

# Per-agent message queue. Incoming turns go here and a single worker drains it
# sequentially, so a message sent while BOSS is busy is queued + auto-processed
# when the current turn finishes (instead of being dropped). Honors the
# one-active-run-per-agent invariant: the worker waits for any in-flight run
# (web UI or its own) before starting the next.
_QUEUE: Optional["asyncio.Queue"] = None


def is_enabled() -> bool:
    return bool(_TOKEN)


def connection() -> Optional[dict]:
    """What the Telegram channel is currently driving, for the UI.

    Returns ``None`` when disabled; else ``{slug, agent_id, bot}`` where ``bot``
    is the bot's @username (or the token's numeric id if the username isn't
    known yet). The UI shows a "🤖 <bot>" badge on the connected agent's node."""
    if not is_enabled():
        return None
    return {
        "slug": _SLUG,
        "agent_id": _AGENT_ID,
        "bot": _bot_username or "bot",
    }


def _authorized(chat_id) -> bool:
    # No allow-list configured ⇒ refuse everyone (fail closed) rather than expose
    # a subscription-burning bot to the public.
    if not _CHAT_IDS:
        return False
    return str(chat_id) in _CHAT_IDS


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #

async def start_telegram():
    """Initialise + start polling inside uvicorn's event loop. No-op if disabled."""
    global _app
    if not is_enabled():
        return
    try:
        from telegram.ext import (ApplicationBuilder, CommandHandler,
                                  MessageHandler, filters)
    except Exception as e:  # pragma: no cover - import guard
        logger.error("python-telegram-bot not available: %s", e)
        return

    application = (ApplicationBuilder().token(_TOKEN).build())

    application.add_handler(CommandHandler("start", _cmd_help))
    application.add_handler(CommandHandler("help", _cmd_help))
    application.add_handler(CommandHandler("status", _cmd_status))
    application.add_handler(CommandHandler("stop", _cmd_stop))
    application.add_handler(CommandHandler("clear", _cmd_clear))
    application.add_handler(CommandHandler("model", _cmd_model))
    application.add_handler(CommandHandler("effort", _cmd_effort))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_message))

    try:
        await application.initialize()
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)
        _app = application
        # Capture the bot @username for the UI "connected to bot" badge.
        global _bot_username
        try:
            me = await application.bot.get_me()
            _bot_username = ("@" + me.username) if me and me.username else None
        except Exception:
            _bot_username = None
        # Start the per-agent turn worker (sequential queue drain).
        global _QUEUE
        _QUEUE = asyncio.Queue()
        asyncio.create_task(_worker())
        who = f"{_SLUG}/{_AGENT_ID}"
        logger.info("Telegram channel enabled → driving %s", who)
        print(f">> Telegram channel enabled → driving {who}", flush=True)
    except Exception as e:  # pragma: no cover - runtime guard
        logger.error("Telegram start failed: %s", e)
        try:
            await application.shutdown()
        except Exception:
            pass


async def stop_telegram():
    """Graceful shutdown. No-op if never started / disabled."""
    global _app
    application = _app
    if application is None:
        return
    _app = None
    try:
        if application.updater and application.updater.running:
            await application.updater.stop()
        if application.running:
            await application.stop()
        await application.shutdown()
    except Exception as e:  # pragma: no cover
        logger.warning("Telegram stop error: %s", e)


# --------------------------------------------------------------------------- #
# Helpers — thin accessors into main/db, imported lazily to avoid an import
# cycle (main imports this module at top level).
# --------------------------------------------------------------------------- #

def _main():
    from backend import main
    return main


async def _reply(update, context, text: str):
    """Send a plain-text reply, chunked to Telegram's 4096-char ceiling."""
    if not text:
        return
    chat_id = update.effective_chat.id
    for i in range(0, len(text), _TG_MAX):
        await context.bot.send_message(chat_id=chat_id, text=text[i:i + _TG_MAX])


# --------------------------------------------------------------------------- #
# Core: a chat turn → run → subscribe (no SSE) → stream back
# --------------------------------------------------------------------------- #

async def _on_message(update, context):
    try:
        if not _authorized(update.effective_chat.id):
            return  # fail closed; no acknowledgement to strangers
        text = (update.message.text or "").strip()
        if not text:
            return
        if _QUEUE is None:
            await _reply(update, context, "⚠️ Kênh chưa sẵn sàng, thử lại sau.")
            return

        # If BOSS is busy (a run is live) or there are turns already waiting,
        # this message is queued — not dropped. Tell the user their position.
        main = _main()
        active = main._active_run(_SLUG, _AGENT_ID)
        ahead = _QUEUE.qsize() + (1 if active else 0)
        await _QUEUE.put((update, context, text))
        if ahead > 0:
            await _reply(update, context,
                         f"📥 {_AGENT_ID} đang bận — đã xếp hàng "
                         f"(còn {ahead} turn phía trước). Tự xử lý khi đến lượt.")
    except Exception as e:  # never let the polling loop die
        logger.exception("Telegram message handler error: %s", e)
        try:
            await _reply(update, context, f"⚠️ Lỗi: {e}")
        except Exception:
            pass


async def _worker():
    """Drain the turn queue sequentially. One BOSS run at a time, ever.

    Before each turn we wait for any in-flight run (ours or the web UI's) to
    finish so the one-active-run-per-agent invariant holds even when the browser
    and Telegram are both driving BOSS."""
    assert _QUEUE is not None
    while True:
        update, context, text = await _QUEUE.get()
        try:
            await _process_turn(update, context, text)
        except Exception as e:
            logger.exception("Telegram worker turn error: %s", e)
            try:
                await _reply(update, context, f"⚠️ Lỗi xử lý turn: {e}")
            except Exception:
                pass


async def _process_turn(update, context, text: str):
    main = _main()
    # Wait for any in-flight BOSS run to finish (web UI concurrency safety).
    while main._active_run(_SLUG, _AGENT_ID):
        await asyncio.sleep(1.0)
    run = main._start_run(_SLUG, _AGENT_ID, text, origin="user")
    await _drive(update, context, run)


async def _drive(update, context, run):
    """Subscribe to the run's event buffer and stream the answer back to chat.

    Mirrors ``main._run_subscriber_sse``: replay buffered events from seq 0,
    then follow the live queue until the run publishes its None sentinel."""
    chat_id = update.effective_chat.id
    bot = context.bot

    # Placeholder that we'll edit as text streams in.
    placeholder = await bot.send_message(
        chat_id=chat_id, text=f"🧠 {_AGENT_ID} đang nghĩ…")
    msg_id = placeholder.message_id

    answer_parts: list[str] = []     # root agent's text only
    progress: list[str] = []          # one line per dispatch event
    had_error: Optional[str] = None

    q: asyncio.Queue = asyncio.Queue()
    run.subscribers.add(q)

    last_edit = 0.0
    dirty = False  # have we accumulated text since the last edit?

    def _preview() -> str:
        head = "\n".join(progress)
        body = "".join(answer_parts)
        merged = (head + "\n\n" + body) if head else body
        # Keep the tail so a long answer doesn't blow the 4096 cap on edit.
        return merged[-_PREVIEW_TAIL:] if len(merged) > _PREVIEW_TAIL else merged

    async def _maybe_edit(force: bool):
        nonlocal last_edit, dirty
        if not dirty and not force:
            return
        now = time.monotonic()
        if not force and (now - last_edit) < _EDIT_INTERVAL_S:
            return
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id, text=_preview() or "…")
            last_edit = now
            dirty = False
        except Exception:
            # "message is not modified" and transient rate-limit errors: ignore.
            pass

    def _process(evt: dict):
        nonlocal dirty, had_error
        et = evt.get("type")
        if et == "delta" and evt.get("agent") == _AGENT_ID:
            answer_parts.append(evt.get("text", ""))
            dirty = True
        elif et == "agent_done" and evt.get("agent") == _AGENT_ID:
            # agent_done carries the full final text; prefer it over the
            # concatenated deltas if the deltas were empty (some adapters emit
            # only the final text).
            t = evt.get("text") or ""
            if t and not answer_parts:
                answer_parts.append(t)
                dirty = True
        elif et == "dispatch_started":
            tgt = evt.get("target", "?")
            task = (evt.get("task") or "").splitlines()[0][:120]
            progress.append(f"→ {tgt}: {task}")
            dirty = True
        elif et == "dispatch_complete":
            tgt = evt.get("target", "?")
            ok = evt.get("status") == "ok"
            progress.append(f"  {'✓' if ok else '✗'} {tgt}")
            dirty = True
        elif et == "error":
            had_error = evt.get("message", "unknown error")
            dirty = True

    try:
        # Replay buffered events, then follow live until the sentinel.
        nxt = 0
        while nxt < len(run.events):
            _process(run.events[nxt])
            nxt += 1
        await _maybe_edit(force=True)
        while True:
            evt = await q.get()
            if evt is None:
                break
            if evt.get("seq", nxt) < nxt:
                continue
            nxt = evt.get("seq", nxt) + 1
            _process(evt)
            await _maybe_edit(force=False)
    finally:
        run.subscribers.discard(q)

    # Final delivery: a clean copy of the answer, chunked if long.
    answer = "".join(answer_parts).strip()
    await _maybe_edit(force=True)

    if had_error:
        await _reply(update, context, f"⚠️ {_AGENT_ID} lỗi: {had_error}")
        return

    if not answer:
        # Nothing streamed (e.g. pure dispatch / narration). Leave the preview as
        # the result rather than posting an empty message.
        return

    # If the final answer is short enough, the edited placeholder already holds
    # it. Otherwise post the full text as fresh message(s) and trim the
    # placeholder to a pointer so the chat stays readable.
    if len(answer) > _TG_MAX:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id,
            text=f"✅ {_AGENT_ID} xong — đáp án đầy đủ ở dưới:")
        await _reply(update, context, answer)


# --------------------------------------------------------------------------- #
# Commands — mirror the in-UI slash commands, via db/main helpers (not REST)
# --------------------------------------------------------------------------- #

async def _cmd_help(update, context):
    if not _authorized(update.effective_chat.id):
        return
    await _reply(update, context, (
        f"🤖 Điều khiển {_AGENT_ID} ({_SLUG})\n\n"
        "• <tin nhắn thường> — gửi một turn cho agent\n"
        "/status — model/effort hiện tại + run đang chạy\n"
        "/stop — huỷ turn đang chạy\n"
        "/clear — tạo session mới (xoá context)\n"
        "/model <tên> — vd /model opus-4-8, sonnet, haiku, fable-5\n"
        "/effort <mức> — default|low|medium|high|max\n"
        "/help — trợ giúp này"
    ))


async def _cmd_status(update, context):
    if not _authorized(update.effective_chat.id):
        return
    from backend import db
    ov = db.get_agent_override(_SLUG, _AGENT_ID) or {}
    adapter = ov.get("model") or "(claude)"
    model = (ov.get("claude_model") or ov.get("grok_model")
             or ov.get("deepseek_model") or ov.get("glm_model") or "(mặc định)")
    effort = ov.get("effort") or "default"
    main = _main()
    active = main._active_run(_SLUG, _AGENT_ID)
    run_line = (f"• run: {active.id} (đang chạy)" if active else "• không có run nào đang chạy")
    await _reply(update, context, (
        f"📊 {_AGENT_ID} ({_SLUG})\n"
        f"• adapter: {adapter}\n"
        f"• model: {model}\n"
        f"• effort: {effort}\n"
        f"{run_line}"
    ))


async def _cmd_stop(update, context):
    if not _authorized(update.effective_chat.id):
        return
    main = _main()
    run = main._active_run(_SLUG, _AGENT_ID)
    if not run or not run.task or run.task.done():
        await _reply(update, context, "Không có run nào đang chạy.")
        return
    run.task.cancel()
    await _reply(update, context, f"⏹️ Đã huỷ run {run.id}.")


async def _cmd_clear(update, context):
    if not _authorized(update.effective_chat.id):
        return
    from backend import db
    # An active run can't be safely cleared mid-flight.
    main = _main()
    if main._active_run(_SLUG, _AGENT_ID):
        await _reply(update, context, "Agent đang chạy — /stop trước khi /clear.")
        return
    sess = db.new_session(_SLUG, _AGENT_ID)
    await _reply(update, context, f"🧹 Session mới: {sess['id'][:8]}…")


async def _cmd_model(update, context):
    if not _authorized(update.effective_chat.id):
        return
    from backend import db
    arg = (context.args[0] if context.args else "").strip()
    if not arg:
        await _reply(update, context, "Cách dùng: /model <tên>  (vd: opus-4-8, sonnet, haiku, fable-5)")
        return
    # set_agent_override needs every column; pass-through current values for the
    # ones we aren't changing so we don't clobber them.
    cur = db.get_agent_override(_SLUG, _AGENT_ID) or {}
    db.set_agent_override(
        _SLUG, _AGENT_ID,
        claude_model=arg,
        grok_model=cur.get("grok_model"),
        deepseek_model=cur.get("deepseek_model"),
        glm_model=cur.get("glm_model"),
        effort=cur.get("effort"),
    )
    await _reply(update, context, f"✅ model → {arg}")


async def _cmd_effort(update, context):
    if not _authorized(update.effective_chat.id):
        return
    from backend import db
    arg = (context.args[0] if context.args else "").strip().lower()
    valid = {"default", "low", "medium", "high", "max"}
    if arg not in valid:
        await _reply(update, context, f"Cách dùng: /effort <{'|'.join(sorted(valid))}>")
        return
    cur = db.get_agent_override(_SLUG, _AGENT_ID) or {}
    db.set_agent_override(
        _SLUG, _AGENT_ID,
        claude_model=cur.get("claude_model"),
        grok_model=cur.get("grok_model"),
        deepseek_model=cur.get("deepseek_model"),
        glm_model=cur.get("glm_model"),
        effort=(None if arg == "default" else arg),
    )
    await _reply(update, context, f"✅ effort → {arg}")
