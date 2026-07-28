"""Minimal ``.env`` loader (no dependency).

Reads ``KEY=VALUE`` lines into ``os.environ`` without overriding variables that
are already set — the real environment always wins over the file. Blank lines
and ``#`` comments are ignored; surrounding single/double quotes are stripped;
a leading ``export`` is tolerated. Used only to populate the optional
adjudication credentials; the scanner never writes these values back.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: Path) -> int:
    """Load ``path`` into ``os.environ`` (set-if-absent). Returns keys loaded."""
    if not path.is_file():
        return 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    loaded = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]  # quoted: keep '#' inside the quotes
        else:
            # Strip an inline comment: ' #...' after an unquoted value.
            hash_at = value.find(" #")
            if hash_at != -1:
                value = value[:hash_at].rstrip()
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded
