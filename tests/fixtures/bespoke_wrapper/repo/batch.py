"""Nightly batch call to the gateway with a fully dynamic payload.

The payload cannot be shape-scored statically; only the org pack's
gateway_hosts entry identifies this as an LLM call.
"""

import requests


def build_payload(rows: list, **extra) -> dict:
    payload = dict(extra)
    payload.update({str(i): r for i, r in enumerate(rows)})
    return payload


def nightly_summary(rows: list) -> str:
    payload = build_payload(rows)
    resp = requests.post("https://llm.bank.internal/api/ask", json=payload)
    return resp.text
