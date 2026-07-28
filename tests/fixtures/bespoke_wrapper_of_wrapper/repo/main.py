"""Daily digest job using the second-order wrapper."""

from helpers import ask_llm


def daily_digest(notes: str) -> str:
    return ask_llm("Summarise the day: " + notes)
