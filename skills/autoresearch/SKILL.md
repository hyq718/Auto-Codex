---
name: autoresearch
description: Turn a single mission markdown such as autoresearch.md into a persistent Codex research runtime with on-disk state, repeated codex exec bursts, job polling, and optional Lark progress updates. Use when the user wants long-running autonomous research that survives waiting on jobs or interrupted sessions.
---

# autoresearch

Use this skill when the user wants Codex to keep pushing a research or engineering task from one mission markdown instead of relying on one long chat session.

The repository now also includes a conversation-mode adapter that exposes the runtime as `/autoresearch`-style commands on top of the same persisted state.

## Workflow

1. Treat the mission markdown as the single source of truth.
2. Bootstrap a runtime with `scripts/autoresearch.py init`.
3. Start or resume the supervisor with `scripts/autoresearch.py start` or run it in the background with `daemon-start`.
4. Keep progress in `state.json`, `plan.json`, `events.jsonl`, `jobs/`, and `notes/latest_summary.md`.
5. Persist out-of-band user feedback with `add-input` / `list-inputs` / `ack-input`.
6. Let the supervisor inject pending inputs into the worker prompt and acknowledge consumed ones.
7. Append meaningful updates to the mission's Lark document when a doc URL is available.
8. Let the supervisor poll the same Lark document for new user-visible content and treat it as input.
9. Use the Slurm helper commands when you need a small standardized `sbatch` / `squeue` path.
10. Prefer the `mode-*` commands when the user wants a chat-visible control surface rather than raw runtime plumbing.

## Commands

```bash
python3 scripts/autoresearch.py init /path/to/autoresearch.md --runtime-dir /path/to/runtime
python3 scripts/autoresearch.py mode-start /path/to/runtime --mission /path/to/autoresearch.md
python3 scripts/autoresearch.py mode-status /path/to/runtime
python3 scripts/autoresearch.py mode-sync /path/to/runtime
python3 scripts/autoresearch.py mode-update /path/to/runtime --message "New suggestion"
python3 scripts/autoresearch.py mode-plan /path/to/runtime
python3 scripts/autoresearch.py mode-jobs /path/to/runtime
python3 scripts/autoresearch.py mode-pause /path/to/runtime
python3 scripts/autoresearch.py mode-resume /path/to/runtime
python3 scripts/autoresearch.py mode-stop /path/to/runtime --reason "manual stop"
python3 scripts/autoresearch.py start /path/to/runtime --search
python3 scripts/autoresearch.py daemon-start /path/to/runtime --search
python3 scripts/autoresearch.py status /path/to/runtime --json
python3 scripts/autoresearch.py daemon-status /path/to/runtime --json
python3 scripts/autoresearch.py add-input /path/to/runtime --message "New suggestion"
python3 scripts/autoresearch.py list-inputs /path/to/runtime --pending-only --json
python3 scripts/autoresearch.py ack-input /path/to/runtime <input-id> --resolution "Queued"
python3 scripts/autoresearch.py submit-job /path/to/runtime ./train.sbatch --notes "smoke test"
python3 scripts/autoresearch.py sync-jobs /path/to/runtime --json
python3 scripts/autoresearch.py list-jobs /path/to/runtime --json
python3 scripts/autoresearch.py stop /path/to/runtime --reason "manual stop"
python3 scripts/autoresearch.py daemon-stop /path/to/runtime --reason "manual stop"
```

## Guidance

- Prefer short, resumable bursts. The supervisor is responsible for waking Codex again.
- If the mission already contains a Feishu/Lark doc URL, let the runtime use it as the default progress target.
- Pending inputs can come from both local runtime commands and Feishu polling.
- `mode-update` should be treated as the chat bridge into the same persisted input queue used by the supervisor.
- `mode-status` and `mode-sync` are the preferred user-facing views when you want a stable, readable progress report in the active conversation.
- When the user asks for packaging or publishing, treat this repository as a reusable `skill + scripts` project rather than a single prompt file.
