# Auto-Codex Commands

Use the wrapper script in this skill as the default entrypoint:

```bash
python3 scripts/auto_codex.py <command> ...
```

## Conversation mode

```bash
python3 scripts/auto_codex.py mode-start RUNTIME_DIR --mission /path/to/autoresearch.md
python3 scripts/auto_codex.py mode-start RUNTIME_DIR --mission /path/to/autoresearch.md --daemon --search
python3 scripts/auto_codex.py mode-status RUNTIME_DIR
python3 scripts/auto_codex.py mode-sync RUNTIME_DIR
python3 scripts/auto_codex.py mode-update RUNTIME_DIR --title "Change direction" --message "Please prioritize the job path first."
python3 scripts/auto_codex.py mode-plan RUNTIME_DIR
python3 scripts/auto_codex.py mode-jobs RUNTIME_DIR
python3 scripts/auto_codex.py mode-pause RUNTIME_DIR
python3 scripts/auto_codex.py mode-resume RUNTIME_DIR
python3 scripts/auto_codex.py mode-stop RUNTIME_DIR --reason "manual stop"
```

## Lower-level runtime commands

```bash
python3 scripts/auto_codex.py init /path/to/autoresearch.md --runtime-dir RUNTIME_DIR
python3 scripts/auto_codex.py start RUNTIME_DIR --search
python3 scripts/auto_codex.py status RUNTIME_DIR --json
python3 scripts/auto_codex.py daemon-start RUNTIME_DIR --search
python3 scripts/auto_codex.py daemon-status RUNTIME_DIR --json
python3 scripts/auto_codex.py daemon-stop RUNTIME_DIR --reason "manual stop"
python3 scripts/auto_codex.py add-input RUNTIME_DIR --message "New suggestion"
python3 scripts/auto_codex.py list-inputs RUNTIME_DIR --pending-only --json
python3 scripts/auto_codex.py ack-input RUNTIME_DIR INPUT_ID --resolution "Queued"
python3 scripts/auto_codex.py submit-job RUNTIME_DIR ./train.sbatch --notes "smoke test"
python3 scripts/auto_codex.py sync-jobs RUNTIME_DIR --json
python3 scripts/auto_codex.py list-jobs RUNTIME_DIR --json
```

## Repo discovery

The wrapper resolves the repo in this order:

1. `AUTO_CODEX_REPO`
2. walk upward from the wrapper script location
3. `/home/yqhao/autoresearch_for_codex`
