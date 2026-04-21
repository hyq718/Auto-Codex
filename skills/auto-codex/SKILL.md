---
name: auto-codex
description: Run or steer the Auto-Codex persistent autoresearch runtime from one mission markdown. Use when the user wants a `/autoresearch`-style workflow in Codex, wants to bootstrap a runtime from `autoresearch.md`, inspect or sync progress, inject new chat directions, pause or resume work, or drive the same long-running runtime that also reports to Feishu and tracks jobs on disk.
---

# Auto-Codex

Use this skill to operate the Auto-Codex runtime through a conversation-friendly control layer instead of manually remembering the underlying repo paths and script entrypoints.

## Quick start

Prefer the wrapper script in this skill:

```bash
python3 scripts/auto_codex.py mode-start --mission /path/to/autoresearch.md
python3 scripts/auto_codex.py mode-approve-plan
python3 scripts/auto_codex.py mode-status
python3 scripts/auto_codex.py mode-update --message "New direction"
```

If the repo is installed somewhere else, set `AUTO_CODEX_REPO` first so the wrapper can find it.
If `RUNTIME_DIR` is omitted, the runtime defaults to `./auto-codex` under the current working directory.

## Workflow

1. Bootstrap or enter mode with `mode-start`.
2. Review the preview and use `mode-approve-plan` or `mode-revise-plan`.
3. Show the current report with `mode-status` or `mode-sync`.
4. Convert user steering into persisted runtime input with `mode-update`.
5. Use `mode-plan` and `mode-jobs` when the user asks for narrower views.
6. Use `mode-pause`, `mode-resume`, or `mode-stop` to control runtime execution state.
7. Fall back to lower-level runtime commands only when you need daemon, Slurm, or raw JSON inspection.

## Command mapping

Treat these as the conversation-mode equivalents of `/autoresearch` commands:

- `/autoresearch start` -> `mode-start`
- `/autoresearch approve-plan` -> `mode-approve-plan`
- `/autoresearch revise-plan` -> `mode-revise-plan`
- `/autoresearch status` -> `mode-status`
- `/autoresearch sync` -> `mode-sync`
- `/autoresearch update` -> `mode-update`
- `/autoresearch plan` -> `mode-plan`
- `/autoresearch jobs` -> `mode-jobs`
- `/autoresearch pause` -> `mode-pause`
- `/autoresearch resume` -> `mode-resume`
- `/autoresearch stop` -> `mode-stop`

Read [references/commands.md](references/commands.md) only when you need the exact command shapes or a reminder of the lower-level runtime commands.

## Guidance

- Prefer `mode-*` commands when the user wants to stay oriented in the active Codex conversation.
- Treat `mode-start` as a plan-preview entrypoint, not as immediate execution.
- Treat `mode-update` as the bridge from chat into the runtime inbox.
- Reuse the same runtime directory across turns; the persistent state is the real source of continuity.
- Prefer the default `./auto-codex` runtime under the user's current project directory unless the user explicitly asks for another location.
- Use `status --json`, `daemon-status --json`, `list-inputs --json`, or `list-jobs --json` only when you need machine-readable inspection.
- Use `start` or `daemon-start` only when you are intentionally running the underlying supervisor loop.
- Worker bursts inherit the current Codex session's sandbox and approval settings when possible. If the user explicitly needs unrestricted worker execution, pass `--worker-full-access` through to the underlying runtime command.
