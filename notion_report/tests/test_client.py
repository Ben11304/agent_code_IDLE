from __future__ import annotations

import json
import unittest

from notion_report.client import NOTION_API_VERSION, NotionAPIError, NotionClient


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class RecordingOpener:
    def __init__(self):
        self.requests = []

    def __call__(self, request, timeout):
        payload = json.loads(request.data.decode("utf-8")) if request.data else None
        self.requests.append(
            {
                "method": request.method,
                "url": request.full_url,
                "headers": dict(request.header_items()),
                "payload": payload,
                "timeout": timeout,
            }
        )
        if request.method == "POST":
            return FakeResponse({"id": "main-page", "url": "https://www.notion.so/main-page"})
        return FakeResponse({"object": "list", "results": []})


class NotionClientTests(unittest.TestCase):
    def test_create_never_omits_parent_and_batches_over_100_blocks(self):
        opener = RecordingOpener()
        client = NotionClient("test-token", opener=opener, max_retries=0)
        blocks = [{"object": "block", "type": "divider", "divider": {}} for _ in range(101)]
        client.create_page("parent-page", "Report", blocks)

        create, append = opener.requests
        self.assertEqual(create["method"], "POST")
        self.assertEqual(create["payload"]["parent"], {"type": "page_id", "page_id": "parent-page"})
        self.assertEqual(len(create["payload"]["children"]), 100)
        self.assertEqual(append["method"], "PATCH")
        self.assertEqual(len(append["payload"]["children"]), 1)
        self.assertEqual(append["payload"]["position"], {"type": "end"})
        self.assertEqual(create["headers"]["Notion-version"], NOTION_API_VERSION)

    def test_workspace_root_creation_is_rejected_without_network(self):
        opener = RecordingOpener()
        client = NotionClient("test-token", opener=opener)
        with self.assertRaisesRegex(NotionAPIError, "parent_page_id is required"):
            client.create_page("", "Unsafe", [])
        self.assertEqual(opener.requests, [])

    def test_create_markdown_uses_exclusive_markdown_payload(self):
        opener = RecordingOpener()
        client = NotionClient("test-token", opener=opener, max_retries=0)

        client.create_page_markdown(
            "parent-page",
            "Report v0009",
            "## Results\n\n```mermaid\nflowchart LR\nA --> B\n```",
        )

        request = opener.requests[0]
        self.assertEqual(request["method"], "POST")
        self.assertIn("markdown", request["payload"])
        self.assertNotIn("children", request["payload"])
        self.assertIn("```mermaid", request["payload"]["markdown"])

    def test_retrieve_markdown_uses_page_markdown_endpoint(self):
        opener = RecordingOpener()
        client = NotionClient("test-token", opener=opener, max_retries=0)

        client.retrieve_page_markdown("page-id")

        request = opener.requests[0]
        self.assertEqual(request["method"], "GET")
        self.assertTrue(request["url"].endswith("/pages/page-id/markdown"))

    def test_archive_block_uses_patch(self):
        opener = RecordingOpener()
        client = NotionClient("test-token", opener=opener, max_retries=0)
        client.archive_block("block-id")
        request = opener.requests[0]
        self.assertEqual(request["method"], "PATCH")
        self.assertEqual(request["payload"], {"archived": True})


if __name__ == "__main__":
    unittest.main()
