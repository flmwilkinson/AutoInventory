"""Second-order convenience wrapper around the in-house client."""

from bank_ai.client import LLMClient

_client = LLMClient()


def ask_llm(prompt: str) -> str:
    """One-shot ask against the gateway."""
    messages = [{"role": "user", "content": prompt}]
    reply = _client.complete(messages)
    return reply["content"]
