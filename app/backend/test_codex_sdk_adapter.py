from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from openai_codex.generated.v2_all import (
    AgentMessageDeltaNotification,
    CommandExecutionThreadItem,
    ErrorNotification,
    ThreadTokenUsage,
    ThreadTokenUsageUpdatedNotification,
    TokenUsageBreakdown,
    Turn,
    TurnCompletedNotification,
    TurnError,
    TurnStatus,
)
from openai_codex.models import Notification

from . import adapters, capabilities


def _usage() -> ThreadTokenUsage:
    last = TokenUsageBreakdown(
        input_tokens=100,
        cached_input_tokens=60,
        cache_write_input_tokens=5,
        output_tokens=20,
        reasoning_output_tokens=3,
        total_tokens=120,
    )
    return ThreadTokenUsage(last=last, total=last, model_context_window=200_000)


def _success_events() -> list[Notification]:
    turn = Turn(id="turn-1", items=[], status=TurnStatus.completed)
    return [
        Notification(
            method="item/agentMessage/delta",
            payload=AgentMessageDeltaNotification(
                delta="hello SDK",
                item_id="message-1",
                thread_id="thread-1",
                turn_id="turn-1",
            ),
        ),
        Notification(
            method="thread/tokenUsage/updated",
            payload=ThreadTokenUsageUpdatedNotification(
                thread_id="thread-1",
                turn_id="turn-1",
                token_usage=_usage(),
            ),
        ),
        Notification(
            method="turn/completed",
            payload=TurnCompletedNotification(thread_id="thread-1", turn=turn),
        ),
    ]


class _FakeTurn:
    def __init__(self, events: list[Notification] | None = None, block: bool = False) -> None:
        self.id = "turn-1"
        self.events = events or []
        self.block = block
        self.interrupted = False

    async def stream(self):
        if self.block:
            await asyncio.Event().wait()
        for event in self.events:
            yield event

    async def interrupt(self):
        self.interrupted = True


class _FakeThread:
    def __init__(self, turn: _FakeTurn, thread_id: str = "thread-1") -> None:
        self.id = thread_id
        self.fake_turn = turn
        self.turn_kwargs: dict = {}
        self.turn_input = None

    async def turn(self, turn_input, **kwargs):
        self.turn_input = turn_input
        self.turn_kwargs = kwargs
        return self.fake_turn


class _FakeCodex:
    last: "_FakeCodex | None" = None
    next_turn = _FakeTurn(_success_events())
    start_error: Exception | None = None

    def __init__(self, config=None) -> None:
        type(self).last = self
        self._client = self
        self.config = config
        self.closed = False
        self.started: dict | None = None
        self.resumed: tuple[str, dict] | None = None
        self.thread = _FakeThread(type(self).next_turn)
        self.extra_root_requests: list[dict] = []

    async def __aenter__(self):
        return self

    async def request(self, method: str, params: dict, **_kwargs):
        if method == "skills/extraRoots/set":
            self.extra_root_requests.append(params)
        return None

    async def thread_start(self, **kwargs):
        if type(self).start_error:
            raise type(self).start_error
        self.started = kwargs
        return self.thread

    async def thread_resume(self, thread_id: str, **kwargs):
        self.resumed = (thread_id, kwargs)
        self.thread.id = thread_id
        return self.thread

    async def close(self) -> None:
        self.closed = True


class CodexSdkAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _FakeCodex.start_error = None
        _FakeCodex.next_turn = _FakeTurn(_success_events())

    async def test_new_thread_streams_normalized_events_and_usage(self) -> None:
        with patch.object(adapters, "AsyncCodex", _FakeCodex):
            events = [event async for event in adapters.codex_stream(
                message="hello",
                system_prompt="agent rules",
                cwd="/tmp",
                model="gpt-test",
                effort="high",
            )]

        self.assertEqual(events[0]["type"], "meta")
        self.assertEqual(events[0]["data"]["codex_transport"], "sdk")
        self.assertEqual(events[1], {"type": "delta", "text": "hello SDK"})
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["text"], "hello SDK")
        self.assertEqual(events[-1]["meta"]["usage"], {
            "input_tokens": 40,
            "cache_read_input_tokens": 60,
            "cache_creation_input_tokens": 5,
            "output_tokens": 20,
        })
        instance = _FakeCodex.last
        assert instance is not None
        self.assertEqual(instance.started["developer_instructions"], "agent rules")
        self.assertTrue(instance.closed)

    async def test_existing_thread_uses_sdk_resume(self) -> None:
        with patch.object(adapters, "AsyncCodex", _FakeCodex):
            events = [event async for event in adapters.codex_stream(
                message="continue",
                system_prompt="rules",
                cwd="/tmp",
                resume_session_id="old-thread",
            )]

        instance = _FakeCodex.last
        assert instance is not None
        self.assertIsNone(instance.started)
        self.assertEqual(instance.resumed[0], "old-thread")
        self.assertEqual(events[0]["data"]["claude_session_id"], "old-thread")

    async def test_agent_capability_policy_is_passed_to_runtime(self) -> None:
        managed_skill_path = capabilities.idle_inventory_skill_paths()[0]
        policy = {
            "revision": 7,
            "skills": {managed_skill_path: True},
            "mcp_servers": {
                "openaiDeveloperDocs": {"tools": {"search_openai_docs": False}}
            },
        }
        with patch.object(adapters, "AsyncCodex", _FakeCodex):
            events = [event async for event in adapters.codex_stream(
                message="isolated",
                system_prompt="",
                cwd="/tmp",
                capability_policy=policy,
            )]

        instance = _FakeCodex.last
        assert instance is not None
        self.assertTrue(any(
            item.startswith("skills.config=") and managed_skill_path in item
            for item in instance.config.config_overrides
        ))
        self.assertIn(
            'mcp_servers.openaiDeveloperDocs.disabled_tools=["search_openai_docs"]',
            instance.config.config_overrides,
        )
        self.assertEqual(events[0]["data"]["capability_revision"], 7)

    async def test_explicit_skill_is_a_real_sdk_input_and_emits_usage(self) -> None:
        skill = capabilities.enabled_inventory_skills({})[0]
        with patch.object(adapters, "AsyncCodex", _FakeCodex):
            events = [event async for event in adapters.codex_stream(
                message=f"${skill['name']} do the task",
                system_prompt="",
                cwd="/tmp",
                requested_skills=[skill],
            )]

        instance = _FakeCodex.last
        assert instance is not None
        self.assertEqual(instance.thread.turn_input[0].text, f"${skill['name']} do the task")
        self.assertEqual(instance.thread.turn_input[1].name, skill["name"])
        self.assertEqual(instance.thread.turn_input[1].path, skill["path"])
        usage = next(event for event in events if event["type"] == "skill_use")
        self.assertEqual(usage["path"], skill["path"])
        self.assertEqual(usage["turn_id"], "turn-1")
        self.assertEqual(usage["source"], "explicit_input")

    def test_completed_command_can_confirm_implicit_skill_read(self) -> None:
        skill = capabilities.enabled_inventory_skills({})[0]
        item = CommandExecutionThreadItem(
            id="command-1",
            type="commandExecution",
            command=f"rtk cat {skill['path']}",
            commandActions=[],
            cwd="/tmp",
            status="completed",
        )
        self.assertEqual(adapters._codex_sdk_skill_reads(item, [skill]), [skill])

    async def test_start_failure_closes_runtime_before_error(self) -> None:
        _FakeCodex.start_error = RuntimeError("auth failed")
        with patch.object(adapters, "AsyncCodex", _FakeCodex):
            events = [event async for event in adapters.codex_stream(
                message="hello", system_prompt="", cwd="/tmp",
            )]

        self.assertEqual(events, [{"type": "error", "message": "auth failed"}])
        assert _FakeCodex.last is not None
        self.assertTrue(_FakeCodex.last.closed)

    async def test_retryable_error_does_not_override_successful_turn(self) -> None:
        retry_notice = Notification(
            method="error",
            payload=ErrorNotification(
                error=TurnError(message="temporary transport failure"),
                thread_id="thread-1",
                turn_id="turn-1",
                will_retry=True,
            ),
        )
        _FakeCodex.next_turn = _FakeTurn([retry_notice, *_success_events()])

        with patch.object(adapters, "AsyncCodex", _FakeCodex):
            events = [event async for event in adapters.codex_stream(
                message="retry", system_prompt="", cwd="/tmp",
            )]

        self.assertFalse(any(event["type"] == "error" for event in events))
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["text"], "hello SDK")

    async def test_cancellation_interrupts_turn_and_closes_runtime(self) -> None:
        blocking_turn = _FakeTurn(block=True)
        _FakeCodex.next_turn = blocking_turn
        with patch.object(adapters, "AsyncCodex", _FakeCodex):
            stream = adapters.codex_stream(
                message="wait", system_prompt="", cwd="/tmp",
            )
            meta = await anext(stream)
            self.assertEqual(meta["type"], "meta")
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending

        self.assertTrue(blocking_turn.interrupted)
        assert _FakeCodex.last is not None
        self.assertTrue(_FakeCodex.last.closed)


if __name__ == "__main__":
    unittest.main()
