"""Report pipeline: three graph nodes, only one of which talks to a model."""

from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

llm = ChatOpenAI(model="gpt-4o-mini")


def plan(state: dict) -> dict:
    response = llm.invoke(state["messages"])
    return {"messages": state["messages"] + [response]}


def fetch_data(state: dict) -> dict:
    rows = load_rows(state["messages"])
    return {"rows": rows}


def render(state: dict) -> dict:
    return {"report": "\n".join(str(r) for r in state["rows"])}


def load_rows(messages: list) -> list:
    return [len(m) for m in messages]


def should_render(state: dict) -> str:
    if state.get("rows"):
        return "render"
    return "end"


graph = StateGraph(dict)
graph.add_node("plan", plan)
graph.add_node("fetch_data", fetch_data)
graph.add_node("render", render)
graph.add_edge(START, "plan")
graph.add_edge("plan", "fetch_data")
graph.add_conditional_edges("fetch_data", should_render, {"render": "render", "end": END})
app = graph.compile()
