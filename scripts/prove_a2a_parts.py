"""End-to-end proof for Phase 1 of the Foundry hosted-agents A2A POC.

Runs four checks and prints a verdict matrix:

  1. Discovery  - fetch the published A2A **agent card** for every agent in the system.
  2. A2A        - call each prompt-agent front-end with `message/send`, follow the task to
                  completion, and probe whether DataPart / FilePart are accepted.
  3. Responses  - call each hosted specialist with input_text + input_file and read back the
                  deterministic inventory of what it actually received.
  4. Supervisor - invoke the supervisor over the Responses API with text + file, and confirm
                  from its hop log that both specialists received both attachments.

Usage:
    python scripts/prove_a2a_parts.py
    python scripts/prove_a2a_parts.py --skip-supervisor
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src" / "supervisor-agent"))

# Agent replies routinely contain characters cp1252 cannot encode.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from a2a_client import (  # noqa: E402  (path shim above must run first)
    A2AError,
    A2AProtocolError,
    FoundryA2AClient,
    data_part,
    file_part_bytes,
    text_part,
)
from responses_client import (  # noqa: E402
    FoundryResponsesClient,
    ResponsesError,
    file_content,
    text_content,
)

SUPERVISOR = "supervisor-agent"
HOSTED = ("research-agent", "analysis-agent")
A2A_FRONT_ENDS = ("research-agent-a2a", "analysis-agent-a2a")
ALL_AGENTS = (SUPERVISOR, *HOSTED, *A2A_FRONT_ENDS)

INVENTORY_MARKER = "A2A-PART-INVENTORY"
HOP_LOG_MARKER = "A2A-HOP-LOG"

SAMPLE_CSV = (
    b"region,units,revenue\n"
    b"EMEA,1200,48000\n"
    b"AMER,1850,79500\n"
    b"APAC,940,31200\n"
    b"LATAM,510,17800\n"
)
SAMPLE_DATA = {
    "probe": "a2a-part-support",
    "quarter": "FY26Q1",
    "currency": "USD",
    "targets": {"EMEA": 50000, "AMER": 75000, "APAC": 35000, "LATAM": 20000},
}

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def json_content(filename: str, payload: dict[str, Any]) -> dict[str, Any]:
    """A structured **data part**: a non-text media type survives as a distinct payload."""
    import base64

    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return {
        "type": "input_file",
        "filename": filename,
        "file_data": f"data:application/json;base64,{encoded}",
    }


def load_env() -> dict[str, str]:
    """Read non-secret deployment values written by scripts/deploy-infra.ps1."""
    env_file = REPO_ROOT / "infra" / "outputs.env"
    if not env_file.exists():
        sys.exit("infra/outputs.env not found. Run ./scripts/deploy-infra.ps1 first.")
    values: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"')
    if "FOUNDRY_PROJECT_ENDPOINT" not in values:
        sys.exit("FOUNDRY_PROJECT_ENDPOINT missing from infra/outputs.env.")
    return values


def extract_marked_json(text: str, marker: str) -> dict[str, Any] | None:
    """Use the final matching block, appended by middleware after model output."""
    for block in reversed(re.findall(r"```json\s*(.*?)```", text, re.DOTALL)):
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("marker") == marker:
            return payload
    return None


def received_parts(value: Any) -> list[dict[str, Any]]:
    return [part for part in value if isinstance(part, dict)] if isinstance(value, list) else []


def has_sample_data(parts: list[dict[str, Any]]) -> bool:
    """An intact decoded preview is required; media type alone proves no structure."""
    for part in parts:
        if part.get("received_as") != "data" or part.get("media_type") != "application/json":
            continue
        preview = part.get("preview")
        if not isinstance(preview, str):
            continue
        if part.get("bytes", len(preview.encode("utf-8"))) != len(preview.encode("utf-8")):
            continue
        try:
            if json.loads(preview) == SAMPLE_DATA:
                return True
        except json.JSONDecodeError:
            pass
    return False


def has_sample_file(parts: list[dict[str, Any]]) -> bool:
    for part in parts:
        preview = str(part.get("preview", "")).replace("\r\n", "\n")
        flattened = (
            part.get("received_as") == "text"
            and preview.startswith("[File: regional-sales.csv]")
        )
        named = part.get("filename") == "regional-sales.csv"
        if (flattened or named) and SAMPLE_CSV.decode("utf-8").strip() in preview:
            return True
    return False


def model_answer(text: str) -> str:
    def without_evidence(match: re.Match[str]) -> str:
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            return match.group(0)
        if isinstance(payload, dict) and payload.get("marker") in (INVENTORY_MARKER, HOP_LOG_MARKER):
            return ""
        return match.group(0)

    return re.sub(r"```json\s*(.*?)```", without_evidence, text, flags=re.DOTALL).strip()


def grounded_emea_result(text: str) -> bool:
    answer = model_answer(text)
    # Keep evidence local to EMEA rather than matching unrelated regional results.
    for mention in re.finditer(r"\bEMEA\b", answer, re.IGNORECASE):
        context = answer[max(0, mention.start() - 80):mention.end() + 250]
        actual = re.search(r"(?<![\d.])48[,\s]?000(?:\.0+)?(?!\d|\.\d)", context)
        target = re.search(r"(?<![\d.])50[,\s]?000(?:\.0+)?(?!\d|\.\d)", context)
        if actual and target and re.search(r"\b(revenue|sales|actual|target|against|versus|vs)\b", context, re.I):
            return True
        attainment = re.search(r"(?<![\d.])96(?:\.0+)?\s*(?:%|percent\b)", context, re.I)
        shortfall = re.search(r"(?<![\d.])4(?:\.0+)?\s*(?:%|percent\b)", context, re.I)
        if attainment and re.search(r"\b(attainment|achieved|met|target)\b", context, re.I):
            return True
        if shortfall and re.search(r"\b(shortfall|below|under|missed)\b", context, re.I):
            return True
    return False


def verdict(ok: bool, label: str) -> str:
    return f"{GREEN}PASS{RESET} {label}" if ok else f"{RED}FAIL{RESET} {label}"


def expected(label: str, detail: str) -> str:
    return f"{YELLOW}KNOWN LIMIT{RESET} {label} {DIM}({detail}){RESET}"


def heading(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


# --------------------------------------------------------------------------------------


async def check_discovery(client: FoundryA2AClient) -> dict[str, bool]:
    heading("1. Agent card discovery")
    results: dict[str, bool] = {}
    for agent in ALL_AGENTS:
        try:
            card = await client.fetch_agent_card(agent)
        except A2AError as exc:
            results[agent] = False
            print(f"  {verdict(False, agent)}\n      {exc}")
            continue

        results[agent] = True
        skills = ", ".join(s.get("id", "?") for s in card.get("skills") or [])
        bindings = sorted({i.get("protocolBinding") for i in card.get("supportedInterfaces") or []})
        print(f"  {verdict(True, agent)}")
        print(f"      {DIM}card      {RESET}{client.card_url(agent)}")
        print(f"      {DIM}interfaces{RESET}{' '}{bindings}")
        print(f"      {DIM}skills    {RESET}{skills}")
        print(f"      {DIM}modes     {RESET}{card.get('defaultInputModes')} -> {card.get('defaultOutputModes')}")
    return results


async def check_a2a(client: FoundryA2AClient) -> dict[str, Any]:
    heading("2. A2A transport (prompt-agent front-ends, discovered via agent card)")
    out: dict[str, Any] = {"text_ok": True, "hosted_blocked": True, "data_rejected": True, "file_rejected": True}

    for peer in A2A_FRONT_ENDS:
        print(f"\n  --- {peer} ---")
        try:
            reply = await client.send_message(peer, [text_part("Reply with exactly: pong.")])
            result = reply.raw
            ok = (
                result.get("kind") == "task"
                and bool(reply.task_id)
                and (result.get("status") or {}).get("state") == "completed"
                and any(
                    part.get("kind", part.get("type")) == "text" and bool(part.get("text"))
                    for artifact in received_parts(result.get("artifacts"))
                    for part in received_parts(artifact.get("parts"))
                )
            )
            print(f"  {verdict(ok, 'text part delivered, task completed')}")
            print(f"      {DIM}task={reply.task_id} parts={reply.part_kinds}{RESET}")
            print(f"      {DIM}reply: {reply.text[:160]}{RESET}")
            out["text_ok"] &= ok
        except A2AError as exc:
            out["text_ok"] = False
            print(f"  {verdict(False, 'text part delivered')}\n      {exc}")

    peer = A2A_FRONT_ENDS[0]
    print(f"\n  --- non-text parts against {peer} ---")
    for label, part in (("data", data_part(SAMPLE_DATA)), ("file", file_part_bytes("s.csv", "text/csv", SAMPLE_CSV))):
        out[f"{label}_rejected"] = False
        try:
            await client.send_message(peer, [text_part("probe"), part])
            print(f"  {GREEN}ACCEPTED{RESET} {label} part (platform behaviour changed - update the docs)")
            out[f"{label}_rejected"] = False
        except A2AProtocolError as exc:
            ok = exc.code == -32005 and exc.data.get("contentType") == label
            out[f"{label}_rejected"] = ok
            detail = f"code={exc.code} {exc.reason}"
            print(f"  {expected(f'{label} part rejected', detail) if ok else verdict(False, detail)}")
        except A2AError as exc:
            print(f"  {verdict(False, f'{label} part probe')}\n      {exc}")

    print("\n  --- hosted agents as A2A targets ---")
    for peer in HOSTED:
        blocked = False
        try:
            await client.send_message(peer, [text_part("ping")])
            print(f"  {GREEN}ACCEPTED{RESET} {peer} answered over A2A (platform behaviour changed)")
            out["hosted_blocked"] = False
        except A2AProtocolError as exc:
            blocked = exc.code == -32099 and exc.data.get("code") == "HostedAgentNotSupported"
            detail = f"code={exc.code} {exc.reason}"
            print(f"  {expected(f'{peer} refused as A2A target', detail) if blocked else verdict(False, detail)}")
        except A2AError as exc:
            print(f"  {verdict(False, f'{peer} A2A probe')}\n      {exc}")
        out["hosted_blocked"] &= blocked
    return out


async def check_responses(client: FoundryResponsesClient) -> dict[str, dict[str, bool]]:
    heading("3. Responses transport (hosted specialists) — text, data and file parts")
    content = [
        # text part
        text_content(
            "Part-support probe. Using the attached JSON targets and the attached CSV, say in "
            "two sentences how EMEA performed, then name the part kinds you received."
        ),
        # data part: a non-text media type stays a distinct structured payload
        json_content("targets.json", SAMPLE_DATA),
        # file part
        file_content("regional-sales.csv", "text/csv", SAMPLE_CSV),
    ]
    print(f"  {DIM}sending content types: {[c['type'] for c in content]}{RESET}")
    print(f"  {DIM}media types:           ['-', 'application/json', 'text/csv']{RESET}")

    results: dict[str, dict[str, bool]] = {}
    for agent in HOSTED:
        print(f"\n  --- {agent} ---")
        try:
            reply = await client.send(agent, content)
        except ResponsesError as exc:
            results[agent] = dict.fromkeys(("completed", "text", "data", "file", "evidence", "grounded"), False)
            print(f"  {verdict(False, 'Responses call')}\n      {exc}")
            continue

        inventory = extract_marked_json(reply.text, INVENTORY_MARKER)
        parts = received_parts((inventory or {}).get("parts"))

        got_text = any(
            p.get("received_as") == "text" and not str(p.get("preview", "")).startswith("[File:")
            for p in parts
        )
        got_data = has_sample_data(parts)
        got_file = has_sample_file(parts)
        grounded = grounded_emea_result(reply.text)

        results[agent] = {
            "completed": reply.status == "completed",
            "text": got_text,
            "data": got_data,
            "file": got_file,
            "evidence": inventory is not None,
            "grounded": grounded,
        }
        print(f"  {verdict(reply.status == 'completed', 'Responses call completed')}")
        print(f"  {verdict(inventory is not None, f'{INVENTORY_MARKER} evidence block present')}")
        print(f"  {verdict(got_text, 'TEXT part delivered')}")
        print(f"  {verdict(got_data, 'DATA part delivered (application/json, structure intact)')}")
        print(f"  {verdict(got_file, 'FILE part delivered (text/* flattened to text by the host)')}")
        print(f"  {verdict(grounded, 'payload values reached the model')}")
        for part in parts:
            print(f"      {DIM}received: {json.dumps(part)[:180]}{RESET}")
    return results


async def check_supervisor(client: FoundryResponsesClient) -> dict[str, bool]:
    heading("4. Supervisor end-to-end (Responses API, text + data + file)")

    # Hosted agents must be called through their own agent endpoint; the project-level
    # /responses route with an agent_reference is rejected with `bad_request`.
    content = [
        text_content(
            "Research how regional cloud sales are benchmarked, then have analysis evaluate "
            "the attached CSV against the attached FY26Q1 targets. Keep it short."
        ),
        json_content("targets.json", SAMPLE_DATA),
        file_content("regional-sales.csv", "text/csv", SAMPLE_CSV),
    ]
    print(f"  {DIM}POST {client.endpoint_url(SUPERVISOR)}{RESET}")
    print(f"  {DIM}sending content types: {[c['type'] for c in content]}{RESET}")

    try:
        reply = await client.send(SUPERVISOR, content)
    except ResponsesError as exc:
        print(f"  {verdict(False, 'supervisor call')}\n      {exc}")
        return {"responded": False, "hop_log": False, "delegated": False, "file_forwarded": False}

    text = reply.text
    hop_log = extract_marked_json(text, HOP_LOG_MARKER)
    hops = received_parts((hop_log or {}).get("hops"))
    successful = [
        hop for hop in hops
        if hop.get("ok") is True and hop.get("transport") == "responses"
        and hop.get("status") == "completed"
    ]
    peers = {hop.get("peer") for hop in successful}
    forwarded_peers = {
        hop.get("peer") for hop in successful
        if isinstance(hop.get("sent_content_types"), list)
        and hop["sent_content_types"].count("input_file") >= 2
        and has_sample_data(received_parts(hop.get("received_parts")))
        and has_sample_file(received_parts(hop.get("received_parts")))
    }

    checks = {
        "responded": reply.status == "completed" and bool(model_answer(text)),
        "hop_log": hop_log is not None,
        "delegated": set(HOSTED).issubset(peers),
        "file_forwarded": set(HOSTED).issubset(forwarded_peers),
    }
    print(f"  {verdict(checks['responded'], 'supervisor returned a response')}")
    print(f"  {verdict(checks['hop_log'], f'{HOP_LOG_MARKER} evidence block present')}")
    print(f"  {verdict(checks['delegated'], f'delegated to both specialists (reached: {sorted(peers)})')}")
    print(f"  {verdict(checks['file_forwarded'], 'data + file parts received by both specialists')}")
    for hop in hops:
        print(f"      {DIM}hop: {json.dumps(hop)[:230]}{RESET}")

    print(f"\n{DIM}--- supervisor answer ---{RESET}")
    print(re.sub(r"```json\s*.*?```", "", text, flags=re.DOTALL).strip()[:2000])
    return checks


# --------------------------------------------------------------------------------------


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-supervisor", action="store_true")
    args = parser.parse_args()

    env = load_env()
    endpoint = env["FOUNDRY_PROJECT_ENDPOINT"]
    print(f"Foundry project: {endpoint}")

    a2a = FoundryA2AClient(endpoint)
    responses = FoundryResponsesClient(endpoint)
    supervisor: dict[str, bool] = {}
    try:
        discovery = await check_discovery(a2a)
        a2a_results = await check_a2a(a2a)
        responses_results = await check_responses(responses)
        if not args.skip_supervisor:
            supervisor = await check_supervisor(responses)
    finally:
        await a2a.aclose()
        await responses.aclose()

    heading("Verdict")
    rows = [
        ("Agent cards published for every agent", all(discovery.values())),
        ("A2A: text part delivered, task ran to completion", a2a_results["text_ok"]),
        ("A2A: data part rejected by the platform (documented limit)", a2a_results["data_rejected"]),
        ("A2A: file part rejected by the platform (documented limit)", a2a_results["file_rejected"]),
        ("A2A: hosted agents refused as targets (documented limit)", a2a_results["hosted_blocked"]),
        ("Responses: every specialist call completed", all(r["completed"] for r in responses_results.values())),
        ("Responses: deterministic evidence block present", all(r["evidence"] for r in responses_results.values())),
        ("Responses: TEXT part delivered", all(r["text"] for r in responses_results.values())),
        ("Responses: DATA part delivered", all(r["data"] for r in responses_results.values())),
        ("Responses: FILE part delivered", all(r["file"] for r in responses_results.values())),
        ("Responses: payload values reached the model", all(r["grounded"] for r in responses_results.values())),
    ]
    if supervisor:
        rows += [
            ("Supervisor answered over Responses API", supervisor["responded"]),
            ("Supervisor emitted a hop log", supervisor["hop_log"]),
            ("Supervisor delegated to both specialists", supervisor["delegated"]),
            ("Supervisor forwarded data + file parts onward", supervisor["file_forwarded"]),
        ]
    for label, ok in rows:
        print(f"  {verdict(ok, label)}")

    return 0 if all(ok for _, ok in rows) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
