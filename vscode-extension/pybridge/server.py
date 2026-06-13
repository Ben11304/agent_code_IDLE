"""Cross-platform launcher for the AgentUI backend, used by the VSCode extension.

Responsibilities (all additive — the main `app/` is never modified):
  * Put the main app's `app/` dir on sys.path so `backend.main:app` imports.
  * On Windows: install the Proactor event loop policy (required for asyncio
    subprocesses) and monkeypatch the ConPTY adapters over the Unix-only ones.
  * Run uvicorn on the host/port handed in by the extension.

Usage:
    python server.py --app-root <path to app dir> --host 127.0.0.1 --port 5174
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-root", required=True,
                    help="Absolute path to the main app dir (parent of `backend/`).")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5174)
    args = ap.parse_args()

    app_root = os.path.abspath(args.app_root)
    backend_dir = os.path.join(app_root, "backend")
    if not os.path.isdir(backend_dir):
        print(f"[server] backend not found at {backend_dir}", file=sys.stderr, flush=True)
        return 2

    # Import `backend.*` as a package: app_root on the path, cwd = app_root so
    # registry.yaml resolves exactly as it does under run.sh.
    if app_root not in sys.path:
        sys.path.insert(0, app_root)
    try:
        os.chdir(app_root)
    except OSError:
        pass

    loop_kind = "auto"
    if sys.platform == "win32":
        # Proactor loop is mandatory for asyncio.create_subprocess_* and for the
        # ConPTY reader's run_in_executor threads.
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        loop_kind = "asyncio"

        # The pristine adapters.py does `import fcntl / pty / termios` at module
        # top (Unix-only) — it cannot even be imported on Windows. Inject empty
        # stub modules so the import succeeds; their internals are only ever
        # touched inside claude_stream/grok_stream, which we replace below, so
        # the stubs are never actually called.
        import types
        for _name in ("fcntl", "pty", "termios"):
            sys.modules.setdefault(_name, types.ModuleType(_name))

        # Layer ConPTY streaming over the Unix-only adapters, before any request.
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        try:
            import backend.adapters as adapters  # noqa: WPS433
            import win_adapters  # noqa: WPS433
            adapters.claude_stream = win_adapters.claude_stream
            adapters.grok_stream = win_adapters.grok_stream
            print("[server] Windows ConPTY adapters active", flush=True)
        except Exception as exc:  # pragma: no cover
            print(f"[server] WARNING: could not install ConPTY adapters: {exc}",
                  file=sys.stderr, flush=True)

    import uvicorn

    print(f"[server] starting on http://{args.host}:{args.port} (loop={loop_kind})",
          flush=True)
    uvicorn.run(
        "backend.main:app",
        host=args.host,
        port=args.port,
        loop=loop_kind,
        log_level="info",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
