"""Load simple KEY=value entries without overriding the process environment."""

import os
import re
from pathlib import Path

_ENTRY = re.compile(r"""\s*([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^#'"]*?))\s*(?:#.*)?""")


def load_env(path):
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return
    for line in lines:
        match = _ENTRY.fullmatch(line)
        if match:
            key, *values = match.groups()
            value = next(value for value in values if value is not None)
            os.environ.setdefault(key, value)
