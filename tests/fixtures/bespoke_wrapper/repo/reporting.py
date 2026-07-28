"""Uses the legacy wrapper package (source not in this repo).

``legacy_ai.OldClient`` is only recognised through the org pack's
known_wrapper_packages seed.
"""

from legacy_ai import OldClient


def summarise_quarter(notes: str) -> str:
    client = OldClient()
    return client.ask(notes, model="legacy-1")
