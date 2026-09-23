"""Shared helper for writing key=value pairs into a dotenv-style file.

Used by scripts/verify_and_set_env.py (writes XRPL wallet addresses to .env)
and scripts/write_web_env.py (writes deployment outputs to web/.env.local).
Neither script ever passes a wallet seed or AWS credential through this.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path


def upsert_env(env_path: Path, values: dict[str, str], template: Path | None = None) -> None:
    """Set each key in place, keep every other line, and append keys not yet present.

    If `env_path` does not exist yet, it is seeded from `template` when given,
    otherwise created empty. The file may hold other local credentials, so it
    is written atomically and owner-only (mode 0600).
    """

    if env_path.exists():
        lines = env_path.read_text().splitlines()
    elif template is not None and template.exists():
        lines = template.read_text().splitlines()
    else:
        lines = []
    pending = dict(values)
    for index, line in enumerate(lines):
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=", line)
        if match and match.group(1) in pending:
            key = match.group(1)
            lines[index] = f"{key}={pending.pop(key)}"
    lines.extend(f"{key}={value}" for key, value in pending.items())

    env_path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=env_path.parent, prefix=f".{env_path.name}.")
    try:
        with os.fdopen(handle, "w") as stream:
            stream.write("\n".join(lines) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, env_path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def read_env_value(env_path: Path, key: str) -> str | None:
    """Return the value of `key` in a dotenv-style file, or None if absent/empty."""

    if not env_path.exists():
        return None
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(key)}=(.*)$")
    value = None
    for line in env_path.read_text().splitlines():
        match = pattern.match(line)
        if match:
            value = match.group(1).strip().strip('"').strip("'")
    return value or None
