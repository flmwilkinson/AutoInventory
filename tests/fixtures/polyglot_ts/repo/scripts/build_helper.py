"""Harmless Python build helper — the only file aiscan can actually analyse."""

import shutil
from pathlib import Path


def copy_templates(src: Path, dst: Path) -> int:
    count = 0
    for path in src.glob("*.html"):
        shutil.copy(path, dst / path.name)
        count += 1
    return count
