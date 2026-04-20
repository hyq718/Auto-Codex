# Auto-Codex

`Auto-Codex` turns a single `autoresearch.md` mission file into a persistent Codex runtime.

It now also exposes a conversation-mode adapter so the runtime can be surfaced as a `/autoresearch`-style control loop instead of only as low-level supervisor commands.

The design goal is simple:

- user input: one mission markdown
- runtime behavior: Codex works in short bursts
- persistence: state is stored on disk
- recovery: the supervisor wakes Codex again after long waits

This is aimed at long-running engineering or research tasks where Codex may need to:

- read a mission file
- modify code or scripts
- submit jobs
- wait for hours
- come back later and continue from disk state instead of chat memory

Repository:

- `https://github.com/hyq718/Auto-Codex`

## What is here

- [`scripts/autoresearch.py`](./scripts/autoresearch.py): init, start, status, and stop commands
- installable Codex skill: [`skills/auto-codex`](./skills/auto-codex)
- conversation-mode commands: `mode-start`, `mode-status`, `mode-sync`, `mode-update`, `mode-plan`, `mode-jobs`, `mode-pause`, `mode-resume`, `mode-stop`
- background supervisor support: `daemon-start`, `daemon-stop`, `daemon-status`
- persisted input support: `add-input`, `list-inputs`, `ack-input`
- Feishu polling and heartbeat support built into the supervisor
- baseline Slurm helpers: `submit-job`, `sync-jobs`, `list-jobs`
- [`skills/autoresearch/SKILL.md`](./skills/autoresearch/SKILL.md): reusable Codex skill entry
- [`schemas/agent_response.schema.json`](./schemas/agent_response.schema.json): structured worker response contract
- [`templates/`](./templates): generated prompt and runbook templates

## How it works

Instead of asking one long-lived Codex session to stay alive forever, this project uses a persistent runtime:

1. `init` copies your mission markdown into a runtime directory and creates on-disk state.
2. `start` launches a supervisor loop.
3. On each tick, the supervisor calls `codex exec` with a structured prompt.
4. Before each worker burst, the supervisor polls pending inputs and can also poll Feishu for new user-visible document content.
5. Codex does one useful chunk of work, writes artifacts, and returns structured JSON.
6. The supervisor updates local state, optional Lark reporting, and sleeps until the next tick.
7. When a job is still running, the runtime waits and resumes later.

That means the real memory is in files like `state.json`, `plan.json`, `jobs/*.json`, and `notes/latest_summary.md`, not in a single chat session.

## Requirements

Minimum requirements:

- `python3`
- `codex`

Optional but recommended:

- `lark-cli` for Feishu/Lark doc updates
- `sbatch` / `squeue` if your mission submits cluster jobs

The current implementation assumes:

- `codex exec` is available on `PATH`
- the runtime directory is writable
- your mission file already contains enough instructions for Codex to act

## Quick start

```bash
python3 scripts/autoresearch.py init /path/to/autoresearch.md --runtime-dir /path/to/runtime
python3 scripts/autoresearch.py start /path/to/runtime --search
```

Check progress:

```bash
python3 scripts/autoresearch.py status /path/to/runtime --json
```

Conversation-style entry:

```bash
python3 scripts/autoresearch.py mode-start /path/to/runtime --mission /path/to/autoresearch.md
python3 scripts/autoresearch.py mode-status /path/to/runtime
python3 scripts/autoresearch.py mode-update /path/to/runtime \
  --title "New direction" \
  --message "Please prioritize the job-monitoring path first."
```

## Install as a Codex skill

The repository now ships with a dedicated skill package at [`skills/auto-codex`](./skills/auto-codex).

Install it into the local Codex skill directory:

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
ln -sfn /home/yqhao/autoresearch_for_codex/skills/auto-codex \
  "${CODEX_HOME:-$HOME/.codex}/skills/auto-codex"
```

After that, new Codex sessions can discover `auto-codex` as a first-class skill.

The skill ships with a wrapper launcher:

```bash
python3 /home/yqhao/autoresearch_for_codex/skills/auto-codex/scripts/auto_codex.py repo-root
python3 /home/yqhao/autoresearch_for_codex/skills/auto-codex/scripts/auto_codex.py mode-status /path/to/runtime
```

If the repo is installed somewhere else, set `AUTO_CODEX_REPO=/path/to/Auto-Codex` so the wrapper can locate the runtime entrypoint.

Run in the background:

```bash
python3 scripts/autoresearch.py daemon-start /path/to/runtime --search
python3 scripts/autoresearch.py daemon-status /path/to/runtime --json
```

Add a persisted user instruction:

```bash
python3 scripts/autoresearch.py add-input /path/to/runtime \
  --title "Change direction" \
  --message "Please prioritize the job-monitoring path first."
python3 scripts/autoresearch.py list-inputs /path/to/runtime --pending-only --json
```

Submit and sync a Slurm job:

```bash
python3 scripts/autoresearch.py submit-job /path/to/runtime ./train.sbatch --notes "smoke test"
python3 scripts/autoresearch.py sync-jobs /path/to/runtime --json
python3 scripts/autoresearch.py list-jobs /path/to/runtime --json
```

Stop gracefully:

```bash
python3 scripts/autoresearch.py stop /path/to/runtime --reason "manual stop"
python3 scripts/autoresearch.py daemon-stop /path/to/runtime --reason "manual stop"
```

## Example

If your mission file is:

- `/home/yqhao/autoresearch.md`

you can create and run a runtime like this:

```bash
cd /home/yqhao/autoresearch_for_codex
python3 scripts/autoresearch.py init /home/yqhao/autoresearch.md \
  --runtime-dir /home/yqhao/autoresearch_for_codex/runtime/megatron-depth-softmax

python3 scripts/autoresearch.py start \
  /home/yqhao/autoresearch_for_codex/runtime/megatron-depth-softmax \
  --search
```

For a long-running unattended session, use the daemon form instead:

```bash
python3 scripts/autoresearch.py daemon-start \
  /home/yqhao/autoresearch_for_codex/runtime/megatron-depth-softmax \
  --search
```

If the mission file already contains a Feishu/Lark doc URL, the runtime will try to append progress there.

If you want to force a reporting target explicitly:

```bash
python3 scripts/autoresearch.py init /home/yqhao/autoresearch.md \
  --runtime-dir /home/yqhao/autoresearch_for_codex/runtime/megatron-depth-softmax \
  --doc-url "https://xxx.feishu.cn/docx/xxxx"
```

## Commands

### Conversation mode

The new mode adapter is meant to mirror an in-chat `/autoresearch` experience on top of the existing runtime.

Recommended mapping:

- `/autoresearch start` -> `mode-start`
- `/autoresearch status` -> `mode-status`
- `/autoresearch sync` -> `mode-sync`
- `/autoresearch update` -> `mode-update`
- `/autoresearch plan` -> `mode-plan`
- `/autoresearch jobs` -> `mode-jobs`
- `/autoresearch pause` -> `mode-pause`
- `/autoresearch resume` -> `mode-resume`
- `/autoresearch stop` -> `mode-stop`

`mode-*` commands do not replace the runtime. They render and manipulate the same persisted state that the supervisor uses.

### `mode-start`

Enter Auto-Codex mode for a runtime. If the runtime does not exist yet, `mode-start` can bootstrap it from one mission markdown.

```bash
python3 scripts/autoresearch.py mode-start RUNTIME_DIR --mission /path/to/autoresearch.md
```

Useful flags:

- `--mission`: bootstrap the runtime if it does not exist yet
- `--doc-url`: attach a Lark/Feishu doc target during bootstrap
- `--daemon`: also start the background supervisor
- `--search`: enable web search when `--daemon` starts the supervisor
- `--codex-config key=value`: pass extra `-c key=value` to `codex exec` when `--daemon` is used

### `mode-status`

Render the current runtime as a conversation-style status report.

```bash
python3 scripts/autoresearch.py mode-status RUNTIME_DIR
```

### `mode-sync`

Render a sync report with recent progress, inputs, and runtime events.

```bash
python3 scripts/autoresearch.py mode-sync RUNTIME_DIR
```

### `mode-update`

Persist a chat-style input and immediately re-render the sync report.

```bash
python3 scripts/autoresearch.py mode-update RUNTIME_DIR \
  --title "Change direction" \
  --message "Please prioritize the job-monitoring path first."
```

### `mode-plan`

Render the current plan in a conversation-friendly format.

```bash
python3 scripts/autoresearch.py mode-plan RUNTIME_DIR
```

### `mode-jobs`

Render active jobs in a conversation-friendly format.

```bash
python3 scripts/autoresearch.py mode-jobs RUNTIME_DIR
```

### `mode-pause`

Pause the runtime and surface the updated status.

```bash
python3 scripts/autoresearch.py mode-pause RUNTIME_DIR
```

### `mode-resume`

Resume a paused runtime and surface the updated status.

```bash
python3 scripts/autoresearch.py mode-resume RUNTIME_DIR
```

### `mode-stop`

Stop the runtime and surface the final status. Add `--daemon` to also stop the background supervisor process.

```bash
python3 scripts/autoresearch.py mode-stop RUNTIME_DIR --reason "manual stop"
```

### `init`

Create a new runtime from one mission markdown.

```bash
python3 scripts/autoresearch.py init MISSION.md --runtime-dir RUNTIME_DIR
```

Useful flags:

- `--runtime-dir`: target runtime directory
- `--doc-url`: override or add a Lark document URL for updates

### `start`

Start or resume the supervisor loop.

```bash
python3 scripts/autoresearch.py start RUNTIME_DIR --search
```

Useful flags:

- `--once`: run exactly one Codex tick, then exit
- `--search`: enable web search in `codex exec`
- `--disable-lark`: skip Lark document updates
- `--codex-config key=value`: pass extra `-c key=value` to `codex exec`

Example:

```bash
python3 scripts/autoresearch.py start /path/to/runtime \
  --once \
  --search \
  --codex-config 'shell_environment_policy.inherit=all'
```

### `status`

Inspect the current runtime state.

```bash
python3 scripts/autoresearch.py status RUNTIME_DIR --json
```

This includes:

- lifecycle status
- active model
- next sleep interval
- latest summary
- current job ids
- current plan
- persisted input counters
- daemon pid and log path

### `add-input`

Persist a new user or system instruction into the runtime.

```bash
python3 scripts/autoresearch.py add-input RUNTIME_DIR \
  --title "Suggestion" \
  --message "Try the SLURM integration path first."
```

Useful flags:

- `--message`: direct text payload
- `--file`: read payload from a file
- `--source`: source label such as `manual` or `feishu`
- `--author`: author label
- `--json`: print the created record as JSON

### `list-inputs`

Inspect persisted input records.

```bash
python3 scripts/autoresearch.py list-inputs RUNTIME_DIR --pending-only --json
```

### `ack-input`

Mark one persisted input as acknowledged.

```bash
python3 scripts/autoresearch.py ack-input RUNTIME_DIR INPUT_ID \
  --resolution "Captured and queued for the next worker tick."
```

### `submit-job`

Submit an `sbatch` script and register the job in runtime state.

```bash
python3 scripts/autoresearch.py submit-job RUNTIME_DIR ./train.sbatch --notes "smoke test"
```

### `sync-jobs`

Refresh known jobs from `squeue`.

```bash
python3 scripts/autoresearch.py sync-jobs RUNTIME_DIR --json
```

### `list-jobs`

List registered job metadata.

```bash
python3 scripts/autoresearch.py list-jobs RUNTIME_DIR --json
```

### `daemon-start`

Launch the supervisor in the background and return immediately.

```bash
python3 scripts/autoresearch.py daemon-start RUNTIME_DIR --search
```

Useful flags:

- `--search`: enable web search in `codex exec`
- `--disable-lark`: skip Lark document updates
- `--codex-config key=value`: pass extra `-c key=value` to `codex exec`

Output:

- runtime path
- daemon pid
- supervisor log path

### `daemon-status`

Inspect the background supervisor state.

```bash
python3 scripts/autoresearch.py daemon-status RUNTIME_DIR --json
```

### `daemon-stop`

Gracefully stop a background supervisor.

```bash
python3 scripts/autoresearch.py daemon-stop RUNTIME_DIR --reason "manual stop"
```

### `stop`

Request a graceful stop and optionally write a stop note to Lark.

```bash
python3 scripts/autoresearch.py stop RUNTIME_DIR --reason "manual stop"
```

## Runtime layout

After `init`, the runtime contains:

- `mission.md`: copied mission file
- `runbook.md`: generated operator summary
- `state.json`: machine-readable state
- `plan.json`: machine-readable current plan
- `events.jsonl`: append-only event log
- `inputs.jsonl`: persisted input queue
- `jobs/`: per-job handoff files
- `notes/latest_summary.md`: latest human-readable checkpoint
- `notes/plan.md`: rendered plan for quick inspection
- `outbox/`: Codex responses and reporting payloads

Important files:

- `state.json`: top-level machine-readable state
- `plan.json`: the current step plan that the worker can update over time
- `events.jsonl`: append-only event stream from the supervisor
- `inputs.jsonl`: append-only style persisted input records managed by CLI
- `jobs/<job_id>.json`: handoff metadata for long-running jobs
- `logs/codex/*.log`: raw logs for each `codex exec` invocation
- `logs/supervisor.log`: stdout/stderr of the background daemon process
- `supervisor.pid`: pid file for `daemon-start`

## Persisted input layer

The runtime now has a first-class input layer for user feedback and future Feishu-synced instructions.

Current behavior:

- `add-input` stores an input record in `inputs.jsonl`
- `list-inputs` shows recent or pending records
- `ack-input` marks one record as acknowledged with an optional resolution
- `status --json` and `daemon-status --json` expose input counters
- supervisor ticks inject pending inputs into the worker prompt
- worker responses can acknowledge consumed inputs with `acknowledged_input_ids`

Inputs can come from two places:

- local runtime commands such as `add-input`
- Feishu document polling when the runtime has a doc URL

The conversation mode uses the same input layer:

- `mode-update` writes `source=chat` input records
- supervisor ticks inject pending inputs into the worker prompt
- the worker can acknowledge them through `acknowledged_input_ids`

## Conversation mode architecture

The current implementation is intentionally split into two layers:

- `runtime`: persistent state, worker bursts, job tracking, Lark sync, recovery
- `mode adapter`: conversation-style commands that render runtime state into a stable chat-friendly structure

The mode report currently surfaces:

- goal
- current plan
- latest progress
- waiting or blocker state
- active jobs
- pending inputs
- recent runtime events
- next action

That means the repo now supports both:

- unattended execution through `start` or `daemon-start`
- user-visible control through `mode-*` commands

## Plan support

The runtime has a built-in plan layer.

When you run `init`:

- the mission markdown is scanned for sections such as `plan`, `priority`, `workflow`, `步骤`, `顺序`, or `工作优先级`
- if such a section exists, its list items are used to seed the plan
- otherwise a default starter plan is created

During execution:

- the worker returns `plan_updates`
- the supervisor writes them into `plan.json`
- a readable copy is rendered to `notes/plan.md`
- `status --json` includes the current plan

This gives you something close to Codex plan mode, but persisted on disk and recoverable across long waits.

## Lark / Feishu updates

If a mission contains a `docx`, `doc`, or `wiki` URL, or if `--doc-url` is passed to `init`, the runtime will use it as the default progress target.

Current behavior:

- update mechanism: `lark-cli docs +update --mode append`
- payload source: worker field `lark_update_markdown`
- supervisor heartbeat also writes to the same document
- completion writes a final summary section
- stop signal handling: a stop message is appended unless `--disable-lark` is set

## Feishu polling

When a runtime has a Feishu/Lark doc URL, the supervisor can periodically read the document and convert new user-visible content into persisted `feishu` inputs.

Current behavior:

- default poll interval: 2 hours
- system-generated document sections use the prefix `Autoresearch System:`
- Feishu polling ignores those system sections to reduce feedback loops
- append-like user edits are converted into new persisted inputs

Current limitation:

- this is best-effort document diffing, not comment-level or block-level semantic tracking

## Slurm helpers

The runtime now includes a baseline Slurm layer:

- `submit-job`: runs `sbatch` and registers the parsed job id
- `sync-jobs`: refreshes known jobs from `squeue`
- `list-jobs`: reads current job metadata from runtime state

## Model behavior

The supervisor currently prefers:

- `gpt-5.4`
- fallback: `gpt-5.3-codex-spark`

If a Codex call appears to fail due to a limit or quota issue, the supervisor can try the fallback model on the next attempt.

## Current limitations

This is a minimal usable prototype, not a finished product.

Current limitations include:

- no hard validation yet that Lark auth/scopes are configured correctly
- no plugin packaging yet; this is currently a `skill + scripts` project
- no deep scheduler semantics yet beyond `sbatch` registration plus `squeue` refresh
- Feishu input detection uses document diffing rather than richer structured signals
- the `/autoresearch` experience is currently exposed as CLI `mode-*` commands, not as a native Codex slash command or plugin UI yet

## Development

Basic validation:

```bash
python3 -m py_compile scripts/autoresearch.py
python3 scripts/autoresearch.py --help
```

Demo runtime:

```bash
python3 scripts/autoresearch.py init examples/minimal-autoresearch.md \
  --runtime-dir runtime/demo
python3 scripts/autoresearch.py status runtime/demo --json
```

Daemon demo:

```bash
python3 scripts/autoresearch.py daemon-start runtime/demo
python3 scripts/autoresearch.py daemon-status runtime/demo --json
tail -f runtime/demo/logs/supervisor.log
```

## Next directions

Natural next steps for this repo:

- add optional tmux/systemd wrappers on top of the built-in daemon mode
- add stronger SLURM helpers around `sbatch`, `squeue`, `sacct`, and log parsing
- add richer Lark reporting helpers
- package the skill for easier installation
- add example missions for research, coding, and experiment management

## Notes

- Lark document updates use `lark-cli docs +update`.
- Model fallback is built in: the supervisor tries `gpt-5.4` first and then `gpt-5.3-codex-spark`.
- The worker is expected to return JSON that matches the bundled schema.
