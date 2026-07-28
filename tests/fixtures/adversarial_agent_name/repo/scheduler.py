"""A perfectly ordinary batch scheduler. Nothing here talks to any AI."""


class Agent:
    """Internal task runner (no relation to any AI SDK)."""

    def __init__(self, name: str, schedule: list | None = None) -> None:
        self.name = name
        self.schedule = schedule or []

    def run(self) -> None:
        for job in self.schedule:
            job()


def nightly_cleanup() -> str:
    return "ok"


agent = Agent("nightly-batch", schedule=[nightly_cleanup])

backup_agent = Agent(name="backup", schedule=[nightly_cleanup])
