"""Two prompt styles: file-loaded and f-string constructed."""

from anthropic import Anthropic

client = Anthropic()


def escalation_reply(incident: str) -> str:
    base_prompt = open("prompts/system.md").read()
    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=500,
        system=base_prompt,
        messages=[{"role": "user", "content": incident}],
    )
    return resp.content[0].text


def greeting_reply(customer_name: str) -> str:
    system = f"You greet {customer_name} warmly and briefly."
    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=200,
        system=system,
        messages=[{"role": "user", "content": "hello"}],
    )
    return resp.content[0].text
