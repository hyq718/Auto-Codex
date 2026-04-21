#!/usr/bin/env python3
"""Thin launcher for the Auto-Codex runtime."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_RUNTIME_DIRNAME = "auto-codex"
ACTIVE_RUNTIME_PATH = Path.home() / ".codex" / "auto-codex-active.json"
OPTIONAL_RUNTIME_COMMANDS = {
    "start",
    "status",
    "stop",
    "mode-start",
    "mode-approve-plan",
    "mode-revise-plan",
    "mode-status",
    "mode-sync",
    "mode-update",
    "mode-plan",
    "mode-jobs",
    "mode-pause",
    "mode-resume",
    "mode-stop",
    "add-input",
    "list-inputs",
    "sync-jobs",
    "list-jobs",
    "daemon-start",
    "daemon-stop",
    "daemon-status",
}


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
        "or install this skill from inside the repository."
    )


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_active_runtime() -> dict[str, str] | None:
    if not ACTIVE_RUNTIME_PATH.exists():
        return None
    try:
        payload = json.loads(ACTIVE_RUNTIME_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    runtime_dir = str(payload.get("runtime_dir", "")).strip()
    if not runtime_dir:
        return None
    return {"runtime_dir": runtime_dir, "entered_at": str(payload.get("entered_at", "")).strip()}


def write_active_runtime(runtime_dir: Path) -> None:
    ACTIVE_RUNTIME_PATH.parent.mkdir(parents=True, exist_ok=True)
    ACTIVE_RUNTIME_PATH.write_text(
        json.dumps(
            {
                "runtime_dir": str(runtime_dir.resolve()),
                "entered_at": now_iso(),
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def clear_active_runtime() -> None:
    if ACTIVE_RUNTIME_PATH.exists():
        ACTIVE_RUNTIME_PATH.unlink()


def resolve_runtime_for_command(argv: list[str]) -> Path | None:
    if not argv:
        return None
    command = argv[0]
    if command not in OPTIONAL_RUNTIME_COMMANDS:
        return None
    if len(argv) > 1 and not argv[1].startswith("-"):
        return Path(argv[1]).expanduser().resolve()
    active = read_active_runtime()
    if active:
        return Path(active["runtime_dir"]).expanduser().resolve()
    return (Path.cwd() / DEFAULT_RUNTIME_DIRNAME).resolve()


def inject_active_runtime(argv: list[str]) -> list[str]:
    if not argv:
        return argv
    command = argv[0]
    if command not in OPTIONAL_RUNTIME_COMMANDS:
        return argv
    if len(argv) > 1 and not argv[1].startswith("-"):
        return argv
    active = read_active_runtime()
    if not active:
        return argv
    return [command, active["runtime_dir"], *argv[1:]]


def handle_wrapper_command(argv: list[str]) -> int | None:
    if not argv:
        return None
    command = argv[0]
    if command in {"mode-enter", "enter"}:
        runtime_dir = resolve_runtime_for_command(["mode-status", *argv[1:]]) or (Path.cwd() / DEFAULT_RUNTIME_DIRNAME).resolve()
        if not (runtime_dir / "state.json").exists():
            raise SystemExit(f"Runtime not initialized: {runtime_dir}")
        write_active_runtime(runtime_dir)
        print(json.dumps({"active": True, "runtime_dir": str(runtime_dir), "entered_at": now_iso()}, ensure_ascii=False))
        return 0
    if command in {"mode-exit", "exit"}:
        previous = read_active_runtime()
        clear_active_runtime()
        print(
            json.dumps(
                {
                    "active": False,
                    "previous_runtime_dir": previous["runtime_dir"] if previous else "",
                },
                ensure_ascii=False,
            )
        )
        return 0
    if command in {"mode-active", "mode-where"}:
        active = read_active_runtime()
        print(json.dumps({"active": bool(active), **(active or {})}, ensure_ascii=False))
        return 0
    return None


def main(argv: list[str]) -> int:
    repo_root = find_repo_root()
    if argv and argv[0] == "repo-root":
        print(str(repo_root))
        return 0
    wrapper_exit = handle_wrapper_command(argv)
    if wrapper_exit is not None:
        return wrapper_exit

    entrypoint = repo_root / "scripts" / "autoresearch.py"
    resolved_argv = inject_active_runtime(argv)
    active_runtime = resolve_runtime_for_command(resolved_argv)
    cmd = [sys.executable, str(entrypoint), *resolved_argv]
    completed = subprocess.run(cmd)  # noqa: S603
    if completed.returncode == 0 and active_runtime is not None:
        write_active_runtime(active_runtime)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
