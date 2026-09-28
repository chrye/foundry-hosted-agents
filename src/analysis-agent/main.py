"""Data-analysis specialist: a Foundry hosted agent reachable over Responses and A2A.

Phase 1 role: given a question plus whatever text / data / file parts the supervisor
forwards, return a quantitative analysis and report which part kinds actually arrived.
Phase 2 will grow this agent into the large-spreadsheet / long-running executor.
"""

import os

from a2a_parts import ReceivedPartsMiddleware, with_received_parts_evidence
from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from excel_tools import EXCEL_TOOLS, ExcelWorkbookMiddleware, with_excel_evidence

load_dotenv()

INSTRUCTIONS = """You are the Data Analysis specialist in a multi-agent system. You are
called by a supervisor agent over the Responses protocol.

You receive a question and, usually, a dataset — as structured data, an attached file, or
inline text. Produce a quantitative analysis in Markdown:

## Metrics
A Markdown table of the key numbers you derived, with units.

## Trends and comparisons

## Insights
Three to five bullets, each tied to a number.

## Confidence
High / Medium / Low with a one-line justification.

Rules:
- Compute from the data you were actually given; show simple calculations inline.
- Never invent precise figures. Label estimates as estimates.
- If a dataset was attached, state its shape (rows / columns or record count) before analysing.
- For .xlsx, always inspect_excel_workbook, then use the exact sheet/column names it returns.
  For shape, use the returned data_rows and total_data_rows, not rows (which includes the
  header). Do not recompute or invent row counts.
  Use read_excel_rows only for bounded inspection. Compute statistics with aggregate_excel
  and cross-sheet actual/target comparisons with compare_excel_sheets. Never calculate from
  just a preview or invent workbook values. Explain the returned Python-tool results.
- Only first-row-header tables are supported by the Excel tools. If a tool fails, report
  its error; do not invent a substitute result or silently ignore unmatched keys.
- Formula values are saved Excel caches, not recalculated. Include any warning about stale
  caches, missing cached results, undefined attainment when a target is zero, or rows
  labelled like totals that may double-count detail rows.
- Do not repeat diagnostic JSON blocks; the application appends them automatically.
- Close with one sentence naming the part kinds you were able to read.
- Always start your reply with the line: `[analysis-agent]`"""


def main() -> None:
    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
        credential=DefaultAzureCredential(),
    )

    agent = Agent(
        client=client,
        name="analysis-agent",
        instructions=INSTRUCTIONS,
        tools=EXCEL_TOOLS,
        middleware=[ReceivedPartsMiddleware(), ExcelWorkbookMiddleware()],
        # History is managed by the hosting infrastructure.
        default_options={"store": False},
    )

    ResponsesHostServer(with_excel_evidence(with_received_parts_evidence(agent))).run()


if __name__ == "__main__":
    main()
