"""In-house client for the bank LLM gateway. No public SDK involved."""

import os

import httpx


class LLMClient:
    """Thin chat client for the internal gateway."""

    def __init__(self, model: str = "bank-small-1") -> None:
        self.model = model
        self.base_url = "https://llm.bank.internal/v1/chat/completions"

    def complete(self, messages: list, temperature: float = 0.0) -> dict:
        """Send a chat request and return the first message."""
        resp = httpx.post(
            self.base_url,
            headers={"x-api-key": os.environ["BANK_LLM_KEY"]},
            json={
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
            },
        )
        data = resp.json()
        return data["choices"][0]["message"]
