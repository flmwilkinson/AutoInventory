"""Test-only LLM call: must not produce an agent or a wrapper."""

import requests


def test_completion() -> None:
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "ping"}],
        },
    )
    assert resp.status_code == 200
