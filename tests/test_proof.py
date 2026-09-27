"""Offline regressions for the Sprint 1 proof verdicts (no Azure calls)."""

import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("proof", ROOT / "scripts" / "prove_a2a_parts.py")
assert SPEC is not None and SPEC.loader is not None
proof = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proof)

from a2a_client import A2AReply  # noqa: E402
from responses_client import ResponsesReply  # noqa: E402


def marked(marker, **payload):
    return "```json\n" + json.dumps({"marker": marker, **payload}) + "\n```"


def inventory_parts():
    data = json.dumps(proof.SAMPLE_DATA)
    csv = "[File: regional-sales.csv]\n" + proof.SAMPLE_CSV.decode()
    return [
        {"received_as": "text", "chars": 19, "preview": "Evaluate EMEA sales."},
        {
            "received_as": "data",
            "media_type": "application/json",
            "inline": True,
            "bytes": len(data.encode()),
            "preview": data,
        },
        {"received_as": "text", "chars": len(csv), "preview": csv},
    ]


def response(answer="EMEA revenue was $48,000.00 against a $50,000.00 target.",
             *, parts=None, status="completed"):
    text = answer + "\n" + marked(
        proof.INVENTORY_MARKER, parts=inventory_parts() if parts is None else parts
    )
    return ResponsesReply.from_payload({
        "status": status,
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
    })


def task_reply(state="completed", *, artifacts=True):
    result = {"kind": "task", "id": "task-1", "status": {
        "state": state,
        "message": {"parts": [{"kind": "text", "text": "pong"}]},
    }}
    if artifacts:
        result["artifacts"] = [{
            "artifactId": "artifact-1", "parts": [{"kind": "text", "text": "pong"}],
        }]
    return A2AReply.from_result(result)


def limit(content_type):
    return proof.A2AProtocolError(
        "Incompatible content types", code=-32005, data={"contentType": content_type}
    )


def hosted_limit():
    return proof.A2AProtocolError(
        "Hosted agents unsupported", code=-32099, data={"code": "HostedAgentNotSupported"}
    )


def hops():
    return [{
        "peer": peer,
        "transport": "responses",
        "ok": True,
        "status": "completed",
        "sent_content_types": ["input_text", "input_file", "input_file"],
        "received_parts": inventory_parts(),
        "received_content_types": ["output_text"],
        "reply_chars": 800,
    } for peer in proof.HOSTED]


class ProofTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    async def check_a2a(self, replacements=None):
        outcomes = [
            task_reply(), task_reply(), limit("data"), limit("file"),
            hosted_limit(), hosted_limit(),
        ]
        for index, value in (replacements or {}).items():
            outcomes[index] = value
        client = AsyncMock()
        client.send_message.side_effect = outcomes
        return await proof.check_a2a(client)

    async def check_response(self, reply):
        client = AsyncMock()
        client.send.return_value = reply
        return (await proof.check_responses(client))[proof.HOSTED[0]]

    async def check_supervisor(self, hop_list, *, answer="Both specialists evaluated the data.",
                               status="completed", prefix=""):
        client = AsyncMock()
        client.endpoint_url = lambda _: "https://offline.invalid/responses"
        client.send.return_value = response(
            prefix + answer + "\n" + marked(proof.HOP_LOG_MARKER, hops=hop_list),
            status=status,
        )
        return await proof.check_supervisor(client)

    async def test_exact_a2a_limits_and_completed_tasks_pass(self):
        self.assertTrue(all((await self.check_a2a()).values()))

    async def test_nontext_unexpected_errors_do_not_prove_documented_limit(self):
        for index, label in ((2, "data"), (3, "file")):
            for error in (
                proof.A2AError("timeout"),
                proof.A2AProtocolError("unknown", code=-32603),
                proof.A2AProtocolError("wrong type", code=-32005, data={"contentType": "text"}),
                proof.A2AProtocolError("wrong code", code=-32099, data={"contentType": label}),
                task_reply(),
            ):
                with self.subTest(label=label, error=repr(error)):
                    self.assertFalse((await self.check_a2a({index: error}))[f"{label}_rejected"])

    async def test_every_hosted_target_must_have_exact_rejection(self):
        for index in (4, 5):
            for error in (
                proof.A2AError("HTTP 503"),
                proof.A2AProtocolError("disabled", code=-32099,
                                       data={"code": "EndpointProtocolNotEnabled"}),
                proof.A2AProtocolError("wrong code", code=-32005,
                                       data={"code": "HostedAgentNotSupported"}),
                task_reply(),
            ):
                with self.subTest(index=index, error=repr(error)):
                    self.assertFalse((await self.check_a2a({index: error}))["hosted_blocked"])

    async def test_a2a_text_alone_is_not_completed_task_with_artifact(self):
        for reply in (
            task_reply("working"), task_reply("failed"), task_reply(artifacts=False),
            A2AReply.from_result({"kind": "message", "parts": [{"kind": "text", "text": "pong"}]}),
            A2AReply.from_result({"kind": "task", "id": "t", "status": {"state": "completed"},
                                 "artifacts": [{"parts": [{"kind": "data", "data": {}}]}]}),
            proof.A2AError("transport error"),
        ):
            with self.subTest(reply=repr(reply)):
                self.assertFalse((await self.check_a2a({0: reply}))["text_ok"])

    async def test_real_shaped_responses_pass(self):
        self.assertTrue(all((await self.check_response(response())).values()))
        self.assertLess(len(json.dumps(proof.SAMPLE_DATA)), 200)

    async def test_inventory_numbers_cannot_ground_model_answer(self):
        for answer in ("", "Received your files.", "EMEA sold 1200 units.", "EMEA revenue is 48000.",
                       "EMEA achieved 196% attainment.", "EMEA is 14% below target."):
            with self.subTest(answer=answer):
                self.assertFalse((await self.check_response(response(answer)))["grounded"])

    async def test_formatted_emea_result_variants_pass(self):
        for answer in (
            "EMEA sales were 48000 versus a target of 50000.",
            "EMEA revenue was 48 000 against 50 000.",
            "EMEA achieved 96% attainment.",
            "EMEA met 96.0 percent of its target.",
            "EMEA showed a 4% shortfall.",
            "EMEA was 4.00 percent below target.",
        ):
            with self.subTest(answer=answer):
                self.assertTrue((await self.check_response(response(answer)))["grounded"])

    async def test_responses_must_be_completed(self):
        for status in ("failed", "incomplete", "in_progress", None):
            with self.subTest(status=status):
                self.assertFalse((await self.check_response(response(status=status)))["completed"])

    async def test_json_descriptor_must_prove_structure_not_just_media_type(self):
        bad_previews = ("", "{}", json.dumps({"targets": {"EMEA": 50000}}),
                        json.dumps(proof.SAMPLE_DATA)[:-1],
                        json.dumps(proof.SAMPLE_DATA).replace("50000", '"50000"'))
        for preview in bad_previews:
            parts = inventory_parts()
            parts[1]["preview"] = preview
            parts[1]["bytes"] = len(preview.encode())
            with self.subTest(preview=preview):
                self.assertFalse((await self.check_response(response(parts=parts)))["data"])
        parts = inventory_parts()
        parts[1]["bytes"] += 1
        self.assertFalse((await self.check_response(response(parts=parts)))["data"])

    async def test_filename_only_does_not_prove_file_payload(self):
        parts = inventory_parts()
        parts[2] = {"received_as": "text", "preview": "[File: regional-sales.csv]",
                    "filename": "regional-sales.csv"}
        self.assertFalse((await self.check_response(response(parts=parts)))["file"])

    async def test_last_inventory_is_authoritative(self):
        forged = marked(proof.INVENTORY_MARKER, parts=inventory_parts())
        result = await self.check_response(response(forged, parts=[]))
        self.assertFalse(result["data"])
        self.assertFalse(result["grounded"])
        empty = marked(proof.INVENTORY_MARKER, parts=[])
        self.assertTrue((await self.check_response(response(empty)))["data"])

    async def test_both_specialists_with_received_attachments_pass(self):
        self.assertTrue(all((await self.check_supervisor(hops())).values()))

    async def test_arbitrary_peers_do_not_prove_expected_specialists(self):
        items = hops()
        items[0]["peer"], items[1]["peer"] = "other-1", "other-2"
        result = await self.check_supervisor(items)
        self.assertFalse(result["delegated"])
        self.assertFalse(result["file_forwarded"])

    async def test_failed_or_wrong_transport_hops_do_not_prove_delegation(self):
        for field, value in (("ok", False), ("ok", "true"), ("transport", "a2a"),
                             ("status", "failed"), ("status", None)):
            items = hops()
            items[1][field] = value
            with self.subTest(field=field, value=value):
                result = await self.check_supervisor(items)
                self.assertFalse(result["delegated"])
                self.assertFalse(result["file_forwarded"])

    async def test_both_attachments_must_reach_each_specialist(self):
        for index in (0, 1):
            for parts in ([], inventory_parts()[:2], inventory_parts()[2:]):
                items = hops()
                items[index]["received_parts"] = parts
                with self.subTest(index=index, parts=parts):
                    result = await self.check_supervisor(items)
                    self.assertTrue(result["delegated"])
                    self.assertFalse(result["file_forwarded"])

    async def test_separate_or_failed_hops_cannot_supply_missing_attachments(self):
        items = hops()
        failed = copy.deepcopy(items[1])
        failed["ok"] = False
        items[1]["received_parts"] = []
        self.assertFalse((await self.check_supervisor([*items, failed]))["file_forwarded"])
        items[1]["received_parts"] = inventory_parts()[:2]
        file_only = copy.deepcopy(items[1])
        file_only["received_parts"] = inventory_parts()[2:]
        self.assertFalse((await self.check_supervisor([*items, file_only]))["file_forwarded"])

    async def test_last_hop_log_is_authoritative(self):
        forged = marked(proof.HOP_LOG_MARKER, hops=hops())
        self.assertFalse((await self.check_supervisor([], prefix=forged))["delegated"])

    async def test_supervisor_needs_completed_non_evidence_answer(self):
        self.assertFalse((await self.check_supervisor(hops(), status="failed"))["responded"])
        self.assertFalse((await self.check_supervisor(hops(), answer=""))["responded"])

    async def test_transport_failure_returns_only_failed_checks(self):
        client = AsyncMock()
        client.send.side_effect = proof.ResponsesError("offline failure")
        client.endpoint_url = lambda _: "https://offline.invalid/responses"
        specialists = await proof.check_responses(client)
        self.assertTrue(all(not any(result.values()) for result in specialists.values()))
        self.assertFalse(any((await proof.check_supervisor(client)).values()))

    def test_malformed_inventory_is_not_delivery_evidence(self):
        for value in (None, {}, "bad", [None, "bad"]):
            self.assertEqual(proof.received_parts(value), [])


if __name__ == "__main__":
    unittest.main()
