"""Read and update backend/.env values."""

import os
from pathlib import Path

ENV_FILE = Path(__file__).parent.parent / ".env"


def set_env_value(key: str, value: str) -> None:
    lines: list[str] = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8").splitlines()

    prefix = f"{key}="
    updated = False
    new_lines: list[str] = []

    for line in lines:
        if line.startswith(prefix):
            new_lines.append(f"{prefix}{value}")
            updated = True
        else:
            new_lines.append(line)

    if not updated:
        new_lines.append(f"{prefix}{value}")

    ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    os.environ[key] = value
