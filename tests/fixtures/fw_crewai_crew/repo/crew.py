"""Two-agent research crew, sequential process."""

import requests
from crewai import Agent, Crew, Process, Task
from crewai.tools import tool


@tool("Search archived reports")
def search_reports(query: str) -> str:
    """Search the archived market reports."""
    resp = requests.get("https://reports.internal.example/search", params={"q": query})
    return resp.text


researcher = Agent(
    role="Research Analyst",
    goal="Find and summarise market data for the requested sector.",
    backstory="You are a meticulous analyst who cites every number.",
    llm="gpt-4o",
    tools=[search_reports],
)

writer = Agent(
    role="Report Writer",
    goal="Turn the research notes into a client-ready report.",
    backstory="You are a clear, concise technical writer.",
    llm="gpt-4o-mini",
)

research_task = Task(
    description="Collect market data for the sector.",
    expected_output="A table of figures with sources.",
    agent=researcher,
)

write_task = Task(
    description="Write the final report from the research notes.",
    expected_output="A two-page report.",
    agent=writer,
)

crew = Crew(
    agents=[researcher, writer],
    tasks=[research_task, write_task],
    process=Process.sequential,
)
