"""ReAct agent over the bank's internal OpenAI-compatible gateway."""

import requests
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent


@tool
def search_kb(query: str) -> str:
    """Search the internal knowledge base."""
    resp = requests.get("https://kb.internal.example/search", params={"q": query})
    return resp.text


llm = ChatOpenAI(model="bank-gpt4", base_url="https://gw.internal.example/v1")

agent = create_react_agent(llm, tools=[search_kb])
