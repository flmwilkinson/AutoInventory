"""Ops assistant loop over the internal LLM gateway (no public host anywhere)."""

import json
import os

import requests

GATEWAY_URL = "https://gw.internal.example/llm/v1/chat"

SYSTEM_PROMPT = "You are the ops assistant for the payments desk."

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_account",
            "description": "Read account master data.",
            "parameters": {"type": "object", "properties": {"account_id": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_payment",
            "description": "Submit a payment instruction.",
            "parameters": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "string"},
                    "amount": {"type": "string"},
                },
            },
        },
    },
]


def lookup_account(account_id: str) -> str:
    """Read account master data."""
    resp = requests.get(f"https://accounts.internal.example/v2/{account_id}")
    return resp.text


def send_payment(account_id: str, amount: str) -> str:
    """Submit a payment instruction to the payments core."""
    resp = requests.post(
        "https://payments-core.internal/api/payments",
        headers={"Authorization": "Bearer " + os.environ["PAYMENTS_TOKEN"]},
        json={"account": account_id, "amount": amount},
    )
    return resp.text


TOOLS = {"lookup_account": lookup_account, "send_payment": send_payment}


def run_agent(request_text: str) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": request_text},
    ]
    turns = 0
    while turns < 8:
        resp = requests.post(
            GATEWAY_URL,
            headers={"Authorization": "Bearer " + os.environ["GATEWAY_TOKEN"]},
            json={
                "model": "internal-x1",
                "messages": messages,
                "temperature": 0,
                "tools": TOOL_SCHEMAS,
            },
        )
        body = resp.json()
        message = body["choices"][0]["message"]
        calls = message.get("tool_calls")
        if not calls:
            return message["content"]
        messages.append(message)
        for call in calls:
            name = call["function"]["name"]
            handler = TOOLS[name]
            output = handler(**json.loads(call["function"]["arguments"]))
            messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": output}
            )
        turns += 1
    return "turn budget exhausted"
