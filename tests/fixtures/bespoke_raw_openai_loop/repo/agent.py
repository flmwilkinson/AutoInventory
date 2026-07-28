"""Hand-rolled tool-use loop over the raw OpenAI HTTP API."""

import json
import os

import requests

API_URL = "https://api.openai.com/v1/chat/completions"


def get_weather(city: str) -> str:
    """Read the current weather for a city."""
    resp = requests.get(f"https://weather.example.com/api?city={city}")
    return resp.text


def save_note(text: str) -> str:
    """Append a note to the local scratch file."""
    with open("notes.txt", "a") as fh:
        fh.write(text + "\n")
    return "saved"


TOOLS = {"get_weather": get_weather, "save_note": save_note}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Read the current weather for a city.",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_note",
            "description": "Append a note to the local scratch file.",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
        },
    },
]


def run_agent(question: str) -> str:
    messages = [
        {"role": "system", "content": "You are a helpful weather assistant."},
        {"role": "user", "content": question},
    ]
    while True:
        resp = requests.post(
            API_URL,
            headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
            json={
                "model": "gpt-4o",
                "messages": messages,
                "tools": TOOL_SCHEMAS,
                "temperature": 0,
            },
        )
        data = resp.json()
        choice = data["choices"][0]
        message = choice["message"]
        if choice["finish_reason"] == "tool_calls":
            messages.append(message)
            for call in message["tool_calls"]:
                fn = TOOLS[call["function"]["name"]]
                result = fn(**json.loads(call["function"]["arguments"]))
                messages.append(
                    {"role": "tool", "tool_call_id": call["id"], "content": result}
                )
            continue
        return message["content"]
