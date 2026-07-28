"""Customer-support triage built on the OpenAI Agents SDK."""

import requests
from agents import Agent, HostedMCPTool, Runner, function_tool


@function_tool
def get_billing(account_id: str) -> str:
    """Fetch the billing status for an account."""
    resp = requests.get(f"https://billing.internal.example/accounts/{account_id}")
    return resp.text


docs_mcp = HostedMCPTool(
    tool_config={
        "type": "mcp",
        "server_label": "docs",
        "server_url": "https://mcp.docs.example.com/sse",
        "require_approval": "always",
    }
)

billing_agent = Agent(
    name="Billing Agent",
    instructions="You handle billing questions for retail customers.",
    model="gpt-4o-mini",
    tools=[get_billing],
)

triage_agent = Agent(
    name="Triage Agent",
    instructions="Route the customer to the right specialist.",
    model="gpt-4o",
    tools=[docs_mcp],
    handoffs=[billing_agent],
)


async def main(question: str) -> str:
    result = await Runner.run(triage_agent, question)
    return str(result.final_output)
