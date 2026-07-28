"""Utility module whose only LLM call sits under the __main__ guard."""

import requests


def format_report(rows: list) -> str:
    return "\n".join(str(r) for r in rows)


if __name__ == "__main__":
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "smoke test"}],
        },
    )
    print(resp.json()["choices"][0]["message"]["content"])
