# Auto-Codex Conversation Mode Spec

This document defines the current conversation-mode layer that sits on top of the persistent Auto-Codex runtime.

## Goal

Expose the runtime as a stable `/autoresearch`-style interaction loop:

- the user sees structured progress in the active Codex conversation
- user messages can be persisted as runtime input
- the same runtime can still continue in the background through the supervisor

## Layers

- `mode adapter`
  - CLI surface for conversation-style control
  - renders runtime state into stable chat-friendly sections
  - maps user updates into persisted runtime input records
- `runtime`
  - persistent state
  - plan tracking
  - worker prompt generation
  - job tracking
  - Feishu polling and reporting
  - daemonized supervisor execution
- `worker`
  - one short Codex burst per tick
  - returns structured JSON according to `schemas/agent_response.schema.json`

## Command mapping

- `/autoresearch start` -> `mode-start`
- `/autoresearch status` -> `mode-status`
- `/autoresearch sync` -> `mode-sync`
- `/autoresearch update` -> `mode-update`
- `/autoresearch plan` -> `mode-plan`
- `/autoresearch jobs` -> `mode-jobs`
- `/autoresearch pause` -> `mode-pause`
- `/autoresearch resume` -> `mode-resume`
- `/autoresearch stop` -> `mode-stop`

## Runtime contract

The mode adapter reads the same persistent files as the supervisor:

- `state.json`
- `plan.json`
- `events.jsonl`
- `inputs.jsonl`
- `jobs/*.json`
- `notes/latest_summary.md`

No separate mode-only state is introduced.

## Input bridge

The conversation-mode input bridge is:

- user speaks in chat
- `mode-update` writes an input item into `inputs.jsonl`
- the supervisor injects pending inputs into the worker prompt
- the worker acknowledges consumed inputs through `acknowledged_input_ids`

This keeps interactive steering and unattended execution on the same state backbone.

## Output contract

`mode-status` and `mode-sync` render the runtime into a stable structure:

1. `Goal`
2. `Current Plan`
3. `Latest Progress`
4. `Waiting / Blockers`
5. `Active Jobs`
6. `Pending Inputs`
7. `Recent Runtime Events`
8. `Next Action`

This is meant to keep the conversation readable and predictable over long-running sessions.

## State semantics

The current mode-related lifecycle states are:

- `initialized`
- `running`
- `waiting_job`
- `paused`
- `blocked`
- `completed`
- `stopped`

The `paused` bit is stored under `state["supervisor"]["paused"]`.

## Current implementation boundary

Implemented:

- mode bootstrap with `mode-start`
- chat-style updates with `mode-update`
- conversation rendering with `mode-status`, `mode-sync`, `mode-plan`, `mode-jobs`
- pause, resume, and stop commands

Not implemented yet:

- native Codex slash-command packaging
- plugin UI integration
- automatic projection of every chat message into the runtime without an explicit mode command
