"""IT-support triage loop over the in-house LLMClient wrapper."""

import json
import subprocess

import requests

from bank_ai import LLMClient


def restart_host(hostname: str) -> str:
    """Restart a host via the ops runner."""
    proc = subprocess.run(["ops-runner", "restart", hostname], capture_output=True, text=True)
    return proc.stdout


def open_ticket(summary: str) -> str:
    """Open an ITSM ticket."""
    resp = requests.post(
        "https://itsm.internal.example/api/tickets",
        json={"summary": summary, "queue": "it-support"},
    )
    return resp.text


ACTIONS = {"restart_host": restart_host, "open_ticket": open_ticket}


def triage_ticket(ticket: str) -> str:
    client = LLMClient()
    history = [
        {"role": "system", "content": "You triage IT support tickets."},
        {"role": "user", "content": ticket},
    ]
    while True:
        reply = client.complete(history)
        calls = reply.get("tool_calls")
        if not calls:
            return reply["content"]
        history.append(reply)
        for call in calls:
            action = ACTIONS[call["function"]["name"]]
            outcome = action(**json.loads(call["function"]["arguments"]))
            history.append(
                {"role": "tool", "tool_call_id": call["id"], "content": outcome}
            )
