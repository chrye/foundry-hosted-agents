"""Card-based routing: peers come from the project's agent list and their A2A cards."""

import copy
import importlib.util
import json
import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from agent_framework import Agent, AgentContext, Message

ROOT = Path(__file__).resolve().parents[1]
_original_path = list(sys.path)
sys.path.insert(0, str(ROOT / "src" / "supervisor-agent"))

from a2a_client import A2AError, A2AProtocolError, A2AReply, FoundryA2AClient
from a2a_parts import inventory_block
from agent_directory import AgentDirectory
from responses_client import ResponsesReply
from turn_state import hop_log_block, reset_hop_log, set_attachments
from xlsx_attachments import XLSX_MEDIA_TYPE

_spec = importlib.util.spec_from_file_location("card_routing_supervisor", ROOT / "src" / "supervisor-agent" / "main.py")
assert _spec is not None and _spec.loader is not None
supervisor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(supervisor)
sys.path[:] = _original_path


def skill(skill_id, *tags):
    return {"id": skill_id, "name": skill_id, "description": f"{skill_id} skill", "tags": list(tags)}


def listed(name, kind):
    return {"name": name, "versions": {"latest": {"definition": {"kind": kind}}}}


LISTING = [
    listed("supervisor-agent", "hosted"), listed("research-agent", "hosted"),
    listed("analysis-agent", "hosted"), listed("research-agent-a2a", "prompt"),
    listed("no-card-agent", "prompt"),
]
CARDS = {
    "supervisor-agent": {"description": "Routes work", "skills": [skill("orchestrate")]},
    "research-agent": {"description": "Research briefs",
                       "skills": [skill("research-brief", "research"), skill("document-review", "file")]},
    "analysis-agent": {"description": "Dataset analysis",
                       "skills": [skill("dataset-analysis", "analysis"), skill("excel-analysis", "excel", "file")]},
    "research-agent-a2a": {"description": "A2A front-end for research",
                           "skills": [skill("research-brief", "research")]},
}
CSV = {"type": "input_file", "filename": "notes.csv", "file_data": "data:text/plain;base64,YSxi"}
XLSX = {"type": "input_file", "filename": "sales.xlsx", "file_data": f"data:{XLSX_MEDIA_TYPE};base64,UEsDBA=="}
HOSTED_REFUSAL = A2AProtocolError("Hosted agents unsupported", code=-32099, data={"code": "HostedAgentNotSupported"})


def registry(cards, listing=LISTING):
    """A fake project registry: the agent list plus the cards each agent publishes."""
    client = Mock(spec=FoundryA2AClient)
    client.list_project_agents = AsyncMock(side_effect=lambda: copy.deepcopy(listing))

    async def fetch(name, *, refresh=False):
        if name not in cards:
            raise A2AError(f"No A2A agent card for '{name}'")
        return copy.deepcopy(cards[name])

    client.fetch_agent_card = AsyncMock(side_effect=fetch)
    client.card_url = lambda name: f"https://example.test/agents/{name}/endpoint/protocols/a2a/agentCard/v1.0"
    return client


def recorded_hops():
    return json.loads(hop_log_block().split("```json\n")[1].split("```")[0])["hops"]


class DirectoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovers_carded_agents_and_excludes_itself(self):
        directory = AgentDirectory(registry(CARDS), self_name="supervisor-agent")
        with self.assertLogs("agent_directory", level="WARNING"):
            peers = await directory.peers()
        self.assertEqual(set(peers), {"research-agent", "analysis-agent", "research-agent-a2a"})
        self.assertEqual(peers["research-agent-a2a"].kind, "prompt")
        self.assertEqual((peers["analysis-agent"].accepts_files, peers["analysis-agent"].accepts_workbooks), (True, True))
        self.assertEqual((peers["research-agent"].accepts_files, peers["research-agent"].accepts_workbooks), (True, False))
        self.assertFalse(peers["research-agent-a2a"].accepts_files)

    async def test_refresh_picks_up_card_edits_and_new_agents(self):
        cards = copy.deepcopy(CARDS)
        listing = list(LISTING)
        directory = AgentDirectory(registry(cards, listing), self_name="supervisor-agent", ttl=3600)
        with self.assertLogs("agent_directory", level="WARNING"):
            await directory.peers()
        cards["research-agent"]["skills"].append(skill("workbooks", "excel"))
        self.assertFalse((await directory.peers())["research-agent"].accepts_workbooks)  # cached
        with self.assertLogs("agent_directory", level="WARNING"):
            self.assertTrue((await directory.peers(refresh=True))["research-agent"].accepts_workbooks)
        listing.append(listed("pricing-agent", "hosted"))
        cards["pricing-agent"] = {"description": "Pricing", "skills": [skill("pricing")]}
        with self.assertLogs("agent_directory", level="WARNING"):
            self.assertEqual((await directory.get("pricing-agent")).name, "pricing-agent")

    async def test_expired_ttl_rediscovers(self):
        client = registry(CARDS)
        directory = AgentDirectory(client, self_name="supervisor-agent", ttl=0)
        with self.assertLogs("agent_directory", level="WARNING"):
            await directory.peers()
            await directory.peers()
        self.assertEqual(client.list_project_agents.await_count, 2)

    async def test_listing_follows_the_pagination_cursor(self):
        pages = {None: {"data": [{"name": "a"}], "has_more": True, "last_id": "a"},
                 "a": {"data": [{"name": "b"}], "has_more": False, "last_id": "b"}}
        seen = []

        def handler(request):
            after = request.url.params.get("after")
            seen.append((after, request.url.params.get("limit")))
            return httpx.Response(200, json=pages[after])

        client = FoundryA2AClient("https://example.test/project")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client._auth_header = AsyncMock(return_value={})
        try:
            self.assertEqual([agent["name"] for agent in await client.list_project_agents()], ["a", "b"])
            self.assertEqual(seen, [(None, "100"), ("a", "100")])
        finally:
            await client.aclose()

    async def test_listing_errors_and_stuck_cursors_are_explicit(self):
        for response in (httpx.Response(403, text="denied"),
                         httpx.Response(200, json={"data": [], "has_more": True, "last_id": "a"})):
            with self.subTest(status=response.status_code):
                client = FoundryA2AClient("https://example.test/project")
                await client._client.aclose()
                client._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request, r=response: r))
                client._auth_header = AsyncMock(return_value={})
                try:
                    with self.assertRaises(A2AError):
                        await client.list_project_agents()
                finally:
                    await client.aclose()


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        supervisor._a2a_inbound.clear()
        reset_hop_log()
        set_attachments([], [])
        self.cards = copy.deepcopy(CARDS)
        self.directory = AgentDirectory(registry(self.cards), self_name="supervisor-agent")
        self.a2a = Mock(spec=FoundryA2AClient)
        self.a2a.card_url = lambda name: f"https://example.test/agents/{name}/endpoint/protocols/a2a/agentCard/v1.0"
        self.a2a.send_message = AsyncMock(return_value=A2AReply(text="pong", part_kinds=["text"], task_id="task-1"))
        self.responses = AsyncMock()
        self.responses.endpoint_url = lambda name: f"https://example.test/agents/{name}/responses"
        self.responses.send.return_value = ResponsesReply(
            text="done\n" + inventory_block([{"received_as": "text", "chars": 4, "preview": "done"}]),
            status="completed",
        )
        for target, value in (("_clients", (self.a2a, self.responses)), ("_get_directory", self.directory)):
            patcher = patch.object(supervisor, target, AsyncMock(return_value=value))
            patcher.start()
            self.addCleanup(patcher.stop)
        # Discovery warns about the listed agent that publishes no card; that is expected here.
        directory_logger = logging.getLogger("agent_directory")
        self.addCleanup(directory_logger.setLevel, directory_logger.level)
        directory_logger.setLevel(logging.ERROR)

    async def test_unknown_agent_is_rejected_without_any_call(self):
        answer = await supervisor.ask_agent("made-up-agent", "hello")
        self.assertIn("No discovered agent is named 'made-up-agent'", answer)
        self.assertIn("analysis-agent", answer)
        self.a2a.send_message.assert_not_awaited()
        self.responses.send.assert_not_awaited()

    async def test_text_goes_over_a2a_from_the_card(self):
        self.assertEqual(await supervisor.ask_agent("research-agent-a2a", "ping"), "pong")
        self.responses.send.assert_not_awaited()
        hop = recorded_hops()[0]
        self.assertEqual((hop["transport"], hop["ok"], hop["task_id"]), ("a2a", True, "task-1"))
        self.assertEqual(supervisor._a2a_inbound["research-agent-a2a"], "accepted")

    async def test_hosted_refusal_falls_back_to_responses_and_is_remembered(self):
        self.a2a.send_message.side_effect = HOSTED_REFUSAL
        await supervisor.ask_agent("research-agent", "first")
        await supervisor.ask_agent("research-agent", "second")
        self.assertEqual(self.a2a.send_message.await_count, 1)
        self.assertEqual(self.responses.send.await_count, 2)
        refused, fallback, remembered = recorded_hops()
        self.assertEqual((refused["transport"], refused["error_code"], refused["fallback"]), ("a2a", -32099, "responses"))
        self.assertEqual(fallback["fallback_from"]["error_reason"], "HostedAgentNotSupported")
        self.assertTrue(fallback["ok"])
        self.assertIn("a2a_skipped", remembered)
        catalog = {entry["name"]: entry for entry in json.loads(await supervisor.list_agents())}
        self.assertTrue(catalog["research-agent"]["a2a_inbound"].startswith("refused by the platform"))
        self.assertIn("over Responses", catalog["research-agent"]["a2a_inbound"])

    async def test_explicit_a2a_and_other_errors_do_not_fall_back(self):
        for transport, error in (("a2a", HOSTED_REFUSAL),
                                 ("auto", A2AProtocolError("off", code=-32099, data={"code": "EndpointProtocolNotEnabled"}))):
            with self.subTest(transport=transport):
                supervisor._a2a_inbound.clear()
                self.a2a.send_message.side_effect = error
                answer = await supervisor.ask_agent("research-agent", "hello", transport)
                self.assertIn("rejected", answer)
                self.responses.send.assert_not_awaited()

    async def test_attachments_reach_only_agents_whose_cards_accept_them(self):
        set_attachments([], [CSV, XLSX])
        with self.assertLogs(supervisor.__name__, level="WARNING"):  # fake reply has no Excel evidence
            await supervisor.ask_agent("analysis-agent", "analyse")
        await supervisor.ask_agent("research-agent", "research")
        answer = await supervisor.ask_agent("research-agent-a2a", "brief")
        analysis_parts, research_parts = (call.args[1] for call in self.responses.send.await_args_list)
        self.assertEqual([part.get("filename") for part in analysis_parts[1:]], ["notes.csv", "sales.xlsx"])
        self.assertEqual([part.get("filename") for part in research_parts[1:]], ["notes.csv"])
        self.a2a.send_message.assert_awaited_once()  # attachments never go over A2A
        analysis_hop, research_hop, front_end_hop = recorded_hops()
        self.assertNotIn("withheld_attachments", analysis_hop)
        self.assertEqual(research_hop["withheld_attachments"], ["sales.xlsx"])
        self.assertEqual((front_end_hop["transport"], front_end_hop["withheld_attachments"]),
                         ("a2a", ["notes.csv", "sales.xlsx"]))
        self.assertIn("not sent to 'research-agent-a2a'", answer)

    async def test_routing_follows_card_edits_not_agent_names(self):
        self.cards["research-agent"]["skills"].append(skill("workbooks", "excel"))
        self.cards["analysis-agent"]["skills"] = [skill("dataset-analysis", "analysis")]
        await supervisor.list_agents()  # refreshes discovery from the edited cards
        set_attachments([], [XLSX])
        with self.assertLogs(supervisor.__name__, level="WARNING"):  # fake reply has no Excel evidence
            await supervisor.ask_agent("research-agent", "open the workbook")
        await supervisor.ask_agent("analysis-agent", "open the workbook")
        research_parts = self.responses.send.await_args_list[0].args[1]
        self.assertEqual(research_parts[1]["filename"], "sales.xlsx")
        self.assertEqual(recorded_hops()[1]["withheld_attachments"], ["sales.xlsx"])

    async def test_middleware_publishes_the_card_catalog_and_discovery_evidence(self):
        # A learned refusal must not leak into the routing catalog: the agent is still
        # reachable over Responses, and a bare "refused" steered the model away from it.
        supervisor._a2a_inbound["research-agent"] = "refused"
        request = AgentContext(agent=Mock(spec=Agent), messages=[Message(role="user", contents=["hi"])])
        seen = []

        async def next_call():
            seen.extend(message.text for message in request.messages
                        if str(message.role) in ("system", "Role.SYSTEM"))

        await supervisor.SupervisorTurnMiddleware().process(request, next_call)
        catalog = next(text for text in seen if "A2A agent cards" in text)
        self.assertIn('"accepts_attachments"', catalog)
        self.assertIn("research-agent-a2a", catalog)
        for leaked in ("a2a_inbound", "refused", "card_url", "https://"):
            self.assertNotIn(leaked, catalog)
        discovery = json.loads(hop_log_block().split("```json\n")[1].split("```")[0])["discovery"]
        self.assertEqual({agent["name"] for agent in discovery["agents"]},
                         {"research-agent", "analysis-agent", "research-agent-a2a"})
        self.assertTrue(all(agent["card_url"].endswith("/agentCard/v1.0") for agent in discovery["agents"]))

    async def test_discovery_failure_is_reported_not_hidden(self):
        failing = Mock(spec=FoundryA2AClient)
        failing.list_project_agents = AsyncMock(side_effect=A2AError("Listing project agents failed (403)"))
        request = AgentContext(agent=Mock(spec=Agent), messages=[Message(role="user", contents=["hi"])])
        with patch.object(supervisor, "_get_directory", AsyncMock(return_value=AgentDirectory(failing, self_name=None))), \
                self.assertLogs(supervisor.__name__, level="WARNING"):
            await supervisor.SupervisorTurnMiddleware().process(request, AsyncMock())
        self.assertIn("discovery failed", request.messages[-1].text)
        self.assertIn("403", json.loads(hop_log_block().split("```json\n")[1].split("```")[0])["discovery"]["error"])


class NoConfiguredPeersTests(unittest.TestCase):
    def test_supervisor_has_no_peer_names_or_mapping(self):
        source = (ROOT / "src" / "supervisor-agent" / "main.py").read_text(encoding="utf-8")
        for name in ("research-agent", "analysis-agent", "_A2A_PEERS", "RESEARCH_AGENT", "ANALYSIS_AGENT"):
            self.assertNotIn(name, source)
        service = (ROOT / "azure.yaml").read_text(encoding="utf-8").split("research-agent:\n")[0]
        self.assertNotIn("_AGENT_NAME", service)


if __name__ == "__main__":
    unittest.main()
