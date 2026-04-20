#!/usr/bin/env python3
"""Thin launcher for the Auto-Codex runtime."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def find_repo_root() -> Path:
    env_value = os.environ.get("AUTO_CODEX_REPO", "").strip()
    candidates: list[Path] = []
    if env_value:
        candidates.append(Path(env_value).expanduser().resolve())

    current = Path(__file__).resolve()
    candidates.extend(current.parents)
    candidates.append(Path("/home/yqhao/autoresearch_for_codex"))

    for base in candidates:
        candidate = base / "scripts" / "autoresearch.py"
        if candidate.exists():
            return base
    raise SystemExit(
        "Could not locate the Auto-Codex repo. Set AUTO_CODEX_REPO=/path/to/Auto-Codex "
        "or install this plugin from inside the repository."
    )


def main(argv: list[str]) -> int:
    repo_root = find_repo_root()
    if argv and argv[0] == "repo-root":
        print(str(repo_root))
        return 0

    entrypoint = repo_root / "scripts" / "autoresearch.py"
    cmd = [sys.executable, str(entrypoint), *argv]
    completed = subprocess.run(cmd)  # noqa: S603
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
