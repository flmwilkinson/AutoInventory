"""A single completion call: model usage, but not an agent."""

from openai import OpenAI

client = OpenAI()


def summarise(text: str) -> str:
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Summarise in one paragraph."},
            {"role": "user", "content": text},
        ],
    )
    return resp.choices[0].message.content
