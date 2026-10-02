from __future__ import annotations

import shutil
import time
import unittest
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from . import terminal_service as terminals


@unittest.skipUnless(shutil.which("tmux"), "tmux is required")
class PersistentTerminalServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.old_socket = terminals._TMUX_SOCKET
        terminals._TMUX_SOCKET = f"agentui-test-{uuid.uuid4().hex[:10]}"

    def tearDown(self) -> None:
        # A dedicated test socket guarantees cleanup cannot touch user sessions.
        terminals._tmux("kill-server")
        terminals._TMUX_SOCKET = self.old_socket

    def test_detached_session_lists_process_and_requires_explicit_kill(self) -> None:
        created = terminals._create_session_sync("persistent test")
        session_id = created["id"]
        sessions = terminals._list_sessions_sync()
        self.assertEqual([session_id], [item["id"] for item in sessions])
        self.assertEqual("persistent test", sessions[0]["title"])
        self.assertFalse(sessions[0]["attached"])
        mouse = terminals._tmux("show-options", "-v", "-t", session_id, "mouse")
        self.assertEqual("on", mouse.stdout.strip())
        history = terminals._tmux(
            "display-message", "-p", "-t", session_id, "#{history_limit}"
        )
        self.assertEqual("50000", history.stdout.strip())
        wheel = terminals._tmux(
            "list-keys", "-T", "copy-mode", "WheelUpPane"
        )
        self.assertIn("-N 1 scroll-up", wheel.stdout)
        terminals._tmux("set-option", "-t", session_id, "mouse", "off")
        self.assertEqual(1, terminals._configure_existing_sessions_sync())
        mouse = terminals._tmux("show-options", "-v", "-t", session_id, "mouse")
        self.assertEqual("on", mouse.stdout.strip())

        terminals._tmux("send-keys", "-t", session_id, "sleep 30", "Enter")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            session = terminals._list_sessions_sync()[0]
            if session["command"] == "sleep":
                break
            time.sleep(0.05)
        self.assertEqual("sleep", session["command"])

        self.assertTrue(terminals._kill_session_sync(session_id))
        self.assertEqual([], terminals._list_sessions_sync())

    def test_routes_expose_list_kill_and_attach(self) -> None:
        app = FastAPI()
        terminals.register_terminal_routes(app)
        paths = {
            (route.path, next(iter(getattr(route, "methods", None) or {"WS"})))
            for route in app.routes
        }
        self.assertTrue(any(path == "/api/terminal/sessions" for path, _ in paths))
        self.assertTrue(any(path == "/api/terminal/sessions/{session_id}" for path, _ in paths))
        self.assertTrue(any(path == "/api/terminal/ws" for path, _ in paths))

    def test_websocket_can_detach_and_reopen_same_session(self) -> None:
        app = FastAPI()
        terminals.register_terminal_routes(app)
        with TestClient(app) as client:
            with client.websocket_connect("/api/terminal/ws") as websocket:
                metadata = websocket.receive_json()
                self.assertEqual("meta", metadata["t"])
                self.assertTrue(metadata["persistent"])
                session_id = metadata["session_id"]

            # Closing the browser-side WebSocket detaches only its tmux client.
            response = client.get("/api/terminal/sessions")
            self.assertEqual(200, response.status_code)
            self.assertEqual(
                [session_id],
                [item["id"] for item in response.json()["sessions"]],
            )

            # Reopening must repair settings on sessions made by older builds.
            terminals._tmux("set-option", "-t", session_id, "mouse", "off")
            with client.websocket_connect(
                f"/api/terminal/ws?session_id={session_id}"
            ) as reopened:
                self.assertEqual(session_id, reopened.receive_json()["session_id"])
            mouse = terminals._tmux("show-options", "-v", "-t", session_id, "mouse")
            self.assertEqual("on", mouse.stdout.strip())

            response = client.delete(f"/api/terminal/sessions/{session_id}")
            self.assertEqual(200, response.status_code)
            self.assertEqual([], terminals._list_sessions_sync())


if __name__ == "__main__":
    unittest.main()
