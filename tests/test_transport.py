import base64
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "supervisor-agent"))

import main as supervisor
from a2a_client import A2AError, FoundryA2AClient, file_part_bytes, file_part_uri
from a2a_parts import inventory_block
from agent_directory import Peer
from agent_framework import Content, Message
from responses_client import FoundryResponsesClient, ResponsesError, ResponsesReply
from turn_state import (
    capture_a2a_attachments,
    capture_responses_attachments,
    hop_log_block,
    reset_hop_log,
    set_attachments,
)


def peer(name, *tags, kind="hosted"):
    return Peer(name, kind, f"https://example.test/agents/{name}/endpoint/protocols/a2a/agentCard/v1.0",
                {"description": name, "skills": [{"id": "skill", "name": "Skill", "description": "d",
                                                  "tags": list(tags)}]})


class FakeDirectory:
    def __init__(self, *peers):
        self._peers = {item.name: item for item in peers}

    async def peers(self, *, refresh=False):
        return dict(self._peers)

    async def get(self, name):
        return self._peers.get(name)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_card_without_transport_does_not_fabricate_endpoint(self):
        client = FoundryA2AClient("https://example.test/project")
        client.fetch_agent_card = AsyncMock(return_value={"skills": []})
        try:
            with self.assertRaisesRegex(A2AError, "no JSONRPC"):
                await client._transport_url("peer")
        finally:
            await client.aclose()

    async def test_card_transport_preserves_query(self):
        client = FoundryA2AClient("https://example.test/project")
        url = "https://example.test/custom?route=one"
        client.fetch_agent_card = AsyncMock(return_value={
            "supportedInterfaces": [{"protocolBinding": "JSONRPC", "url": url}]
        })
        try:
            self.assertEqual(await client._transport_url("peer"), url + "&api-version=v1")
        finally:
            await client.aclose()

    async def test_failed_task_is_not_a_successful_reply(self):
        client = FoundryA2AClient("https://example.test/project")
        client._transport_url = AsyncMock(return_value="https://example.test/rpc")
        client._rpc = AsyncMock(return_value={
            "kind": "task", "id": "task-1",
            "status": {"state": "failed", "message": {"parts": [{"kind": "text", "text": "error"}]}},
        })
        try:
            with self.assertRaisesRegex(A2AError, "without completing"):
                await client.send_message("peer", [])
        finally:
            await client.aclose()

    async def test_task_lifecycle_polls_to_completion(self):
        client = FoundryA2AClient("https://example.test/project")
        client._transport_url = AsyncMock(return_value="https://example.test/rpc")
        client._rpc = AsyncMock(side_effect=[
            {"kind": "task", "id": "task-1", "status": {"state": "submitted"}},
            {"kind": "task", "id": "task-1", "status": {"state": "completed"},
             "artifacts": [{"parts": [{"kind": "text", "text": "pong"}]}]},
        ])
        try:
            reply = await client.send_message("peer", [], poll_interval=0)
            self.assertEqual(reply.text, "pong")
            self.assertEqual(client._rpc.call_args_list[1].args[1], "tasks/get")
        finally:
            await client.aclose()

    async def test_http_200_failed_response_is_not_success(self):
        client = FoundryResponsesClient("https://example.test/project")
        client._headers = AsyncMock(return_value={})
        client._client.post = AsyncMock(return_value=httpx.Response(
            200, json={"status": "failed", "error": {"message": "model failed"}, "output": []}
        ))
        try:
            with self.assertRaisesRegex(ResponsesError, "did not complete"):
                await client.send("peer", [])
        finally:
            await client.aclose()

    async def test_probe_really_sends_three_native_parts(self):
        responses = AsyncMock()
        responses.endpoint_url = lambda name: f"https://example.test/{name}"
        received = [{"received_as": "data", "filename": "targets.json"}]
        responses.send.return_value = ResponsesReply(
            text=inventory_block(received), status="completed", content_types=["output_text"]
        )
        reset_hop_log()
        directory = FakeDirectory(peer("research-agent-a2a", kind="prompt"), peer("research-agent", "file"))
        with patch.object(supervisor, "_clients", AsyncMock(return_value=(AsyncMock(), responses))), \
             patch.object(supervisor, "_get_directory", AsyncMock(return_value=directory)), \
             patch.object(supervisor, "_delegate_a2a", AsyncMock(return_value=("known limit", {}))):
            await supervisor.probe_part_support("research-agent-a2a", "research-agent")
        content = responses.send.call_args.args[1]
        self.assertEqual([part["type"] for part in content], ["input_text", "input_file", "input_file"])
        payload = json.loads(base64.b64decode(content[1]["file_data"].partition(",")[2]))
        self.assertEqual(payload["targets"]["EMEA"], 50000)
        self.assertIn('"received_parts"', hop_log_block())

    async def test_delegation_records_received_inventory(self):
        responses = AsyncMock()
        responses.endpoint_url = lambda name: f"https://example.test/{name}"
        received = [{"received_as": "text", "preview": "[File: sales.csv]\nx,y"}]
        responses.send.return_value = ResponsesReply(text=inventory_block(received), status="completed")
        reset_hop_log()
        set_attachments([], [])
        with patch.object(supervisor, "_clients", AsyncMock(return_value=(AsyncMock(), responses))):
            await supervisor._delegate_responses(peer("research-agent", "file"), "research", [], [])
        self.assertIn('"received_parts"', hop_log_block())
        self.assertEqual(supervisor._received_parts(responses.send.return_value.text), received)


class AttachmentTests(unittest.TestCase):
    def test_flattened_csv_survives_both_projections(self):
        messages = [Message(role="user", contents=[
            Content.from_text("Analyze this"),
            Content.from_text("[File: sales.csv]\nx,y\n1,2"),
        ])]
        a2a = capture_a2a_attachments(messages)
        responses = capture_responses_attachments(messages)
        self.assertEqual(a2a[0]["file"]["name"], "sales.csv")
        self.assertEqual(base64.b64decode(a2a[0]["file"]["bytes"]), b"x,y\n1,2")
        self.assertEqual(responses[0]["filename"], "sales.csv")
        self.assertEqual(base64.b64decode(responses[0]["file_data"].partition(",")[2]), b"x,y\n1,2")

    def test_file_builders(self):
        self.assertEqual(file_part_bytes("a.csv", "text/csv", b"a"), {
            "kind": "file", "file": {"name": "a.csv", "mimeType": "text/csv", "bytes": "YQ=="}
        })
        self.assertEqual(file_part_uri("a.csv", "text/csv", "https://example.test/a.csv")["file"]["uri"],
                         "https://example.test/a.csv")


if __name__ == "__main__":
    unittest.main()
