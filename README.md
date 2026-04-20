# Auto-Codex

`Auto-Codex` turns one `autoresearch.md` mission into a persistent Codex runtime that can keep working across long waits, track jobs on disk, report to Feishu, and resume with low-token context.

It is built for tasks like:

- long engineering investigations
- code changes followed by cluster jobs
- repeated log inspection and retry loops
- multi-hour or multi-day autoresearch

Repository:

- `https://github.com/hyq718/Auto-Codex`

## Quick Start

### 1. Install

```bash
git clone https://github.com/hyq718/Auto-Codex.git
cd Auto-Codex
./install.sh
```

Then restart Codex so it can rescan local skills and plugins.

### 2. Open Codex in the project you want to research

`Auto-Codex` now defaults its runtime to `./auto-codex` under your current working directory.

Example:

- if you run it inside `/path/to/LLaMA-Factory`
- the runtime will be created at `/path/to/LLaMA-Factory/auto-codex`

### 3. Start from a mission markdown

In the Codex chat, use natural language with `$auto-codex`.

These are chat messages, not shell commands:

```text
$auto-codex 从 ./autoresearch.md 开始一个新的 autoresearch
```

`Auto-Codex` will:

- read the mission
- create `./auto-codex`
- generate a plan preview
- stop and wait for confirmation before execution

### 4. Approve or revise the plan

Approve:

```text
$auto-codex 这个 plan 可以了，开始执行
```

Revise:

```text
$auto-codex 先不要开始，把日志分析提前
```

### 5. Check status or add new directions

```text
$auto-codex 看一下当前状态
```

```text
$auto-codex 同步一下最近进展
```

```text
$auto-codex 加一条新要求：优先检查最新 job 的 10k eval
```

### 6. Pause, resume, or stop

```text
$auto-codex 暂停当前 autoresearch
```

```text
$auto-codex 恢复当前 autoresearch
```

```text
$auto-codex 停止当前 autoresearch
```

## Example Session

Assume you are in a project directory and already have `./autoresearch.md`.

In Codex chat:

```text
$auto-codex 从 ./autoresearch.md 开始一个新的 autoresearch
```

Codex will show a plan preview instead of immediately running.

Then:

```text
$auto-codex 这个 plan 可以了，开始执行
```

Later, while it is running:

```text
$auto-codex 看一下当前状态
```

```text
$auto-codex 加一条新要求：优先对比最新 job 和参考日志的 eval loss
```

If the task is waiting on a long job:

```text
$auto-codex 同步一下最近进展
```

The runtime should tell you:

- current phase
- current phase plan
- next action
- focused jobs
- sleep reason

## What Auto-Codex Actually Does

This project is not just a prompt. It has three practical layers:

### 1. Mode Layer

This is the user-facing control layer inside Codex chat.

It is what `$auto-codex` drives.

Its job is to:

- bootstrap a runtime from one mission markdown
- show the plan preview
- accept user steering in natural language
- render runtime state back into a readable report

### 2. Runtime / Supervisor Layer

This is the persistent execution layer.

Its job is to:

- store state on disk
- wake up future worker bursts
- track inputs, jobs, events, notes, and plan files
- update Feishu
- keep working across long waits

### 3. Worker Layer

This is each short burst of actual Codex work.

A worker does not depend on chat memory. It reads runtime files, performs one useful chunk of work, and writes back structured results.

## The Main Design Principles

### Plan Preview First

`Auto-Codex` no longer starts execution immediately after reading the mission.

It first creates a plan preview and waits for approval.

That means it has:

- `plan.preview.json`
- `plan.json`

The idea is simple:

- preview first
- user confirms
- execution starts

### Summary First, Deep Read Later

The runtime is designed to reduce token waste.

It should:

- read the smallest necessary context first
- read summaries before full files
- search before reading long logs
- widen context only when the current step really needs it

### Recovery Is for Future Workers

The most important state is not “what happened before”.

It is:

- what the next worker should do first
- where it should look
- what it should search for
- how it should widen the read budget if the first retrieval misses

This is why the runtime keeps a `resume_status.md` and an execution packet in `state.json`.

## Runtime Layout

Inside the project you are researching, `Auto-Codex` will create `./auto-codex`.

Important files:

- `mission.md`: copied mission
- `state.json`: main machine-readable state
- `plan.preview.json`: unapproved plan
- `plan.json`: current approved plan
- `events.jsonl`: append-only event log
- `inputs.jsonl`: persisted chat or Feishu inputs
- `jobs/<job_id>.json`: per-job metadata
- `notes/plan_preview.md`: human-readable preview plan
- `notes/plan.md`: human-readable current plan
- `notes/jobs_focus.md`: only the jobs the next tick should inspect first
- `notes/latest_summary.md`: concise latest checkpoint
- `notes/resume_status.md`: current phase plus next action packet
- `logs/supervisor.log`: supervisor output
- `logs/codex/`: per-worker raw logs

## Token-Aware Job Reading

Workers should not scan the entire `jobs/` directory by default.

Instead, each tick writes:

- `notes/jobs_focus.md`

That file contains only the small set of currently relevant jobs.

The intended read order is:

1. read `jobs_focus.md`
2. open the most relevant job log
3. search for the exact signal
4. read a small local window
5. only then widen to more logs or more jobs

This is the default strategy for saving tokens.

## Recovery-Oriented Status

`Auto-Codex` now persists not only the plan, but also the exact recovery packet for the next worker.

The runtime keeps:

- `execution.current_phase`
- `execution.phase_plan`
- `execution.next_action`

And also renders them to:

- `notes/resume_status.md`

This is what tells a future worker:

- what to do first
- why this is the next step
- where to look
- what patterns to search
- what counts as success
- what to do if the signal is missing

## Sleep Strategy

The runtime now uses a dual sleep strategy.

### Default fallback

- wake within `1 hour`

### Preferred path

If the current critical path is a long-running job, the runtime tries to estimate a better wake-up time from logs.

It can use signals such as:

- `5s/it`
- `it/s`
- `iteration time: 4.0 s`
- `global_step=9500`
- `iteration 8200`
- `10k`
- `10000`
- `eval every 1000 steps`

Then it estimates when the next useful checkpoint should appear.

The estimate is still capped at `1 hour`.

So the effective rule is:

- estimate if possible
- otherwise fall back to `1 hour`
- never sleep longer than `1 hour`

## Feishu Integration

If the mission contains a Feishu/Lark doc URL, or you pass one explicitly, the runtime can:

- append progress updates
- append stop notes
- append final summaries
- periodically poll the doc for new user-visible input

So Feishu plays three roles:

- progress panel
- asynchronous input panel
- remote review panel

## Installation Modes

This repo provides:

- installable skill: [`skills/auto-codex`](./skills/auto-codex)
- repo-local plugin: [`plugins/auto-codex`](./plugins/auto-codex/.codex-plugin/plugin.json)
- installer: [`install.sh`](./install.sh)

By default `./install.sh` installs:

- `auto-codex` into `~/.agents/skills/auto-codex`
- `auto-codex` into `~/.codex/skills/auto-codex`
- a local plugin into `~/plugins/auto-codex`
- a marketplace entry into `~/.agents/plugins/marketplace.json`

## When You Need Explicit Commands

Natural language should be the normal user experience in Codex chat.

If you need exact low-level control, the underlying mode commands are:

- `mode-start`
- `mode-approve-plan`
- `mode-revise-plan`
- `mode-status`
- `mode-sync`
- `mode-update`
- `mode-plan`
- `mode-jobs`
- `mode-pause`
- `mode-resume`
- `mode-stop`

The wrapper script is:

- [`skills/auto-codex/scripts/auto_codex.py`](./skills/auto-codex/scripts/auto_codex.py)

And the lower-level runtime entrypoint is:

- [`scripts/autoresearch.py`](./scripts/autoresearch.py)

## Recommended Environment

For long-running autoresearch on a cluster, the practical recommendation is:

- run Codex in `tmux`
- run it on a CPU compute node if that is how your cluster is meant to be used
- avoid leaving long-lived supervisor processes on login nodes

## Current Limits

This project already has a strong base, but it is not finished in every direction.

Current boundaries:

- it is still a skill-driven mode, not a native built-in `/autoresearch` command
- plugin discovery still depends on the Codex frontend
- Slurm support is still a baseline helper layer, not a full scheduler integration
- the main runtime is still single-worker, not a full multi-agent coordinator
- the sleep estimator covers common training-log formats, not every framework-specific style

## In One Sentence

`Auto-Codex` is currently a low-token, recovery-oriented autoresearch runtime for Codex: one mission in, persistent execution out.
