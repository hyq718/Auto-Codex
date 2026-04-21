#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import textwrap
import time
import uuid
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from string import Template
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = PROJECT_ROOT / "templates"
SCHEMA_PATH = PROJECT_ROOT / "schemas" / "agent_response.schema.json"
DEFAULT_MODEL = "gpt-5.4"
DEFAULT_FALLBACK_MODEL = "gpt-5.3-codex-spark"
DEFAULT_INTERVAL_SECONDS = 3600
DEFAULT_FAILURE_RETRY_SECONDS = 300
DEFAULT_LARK_POLL_INTERVAL_SECONDS = 7200
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 7200
DEFAULT_WORKER_SANDBOX = "workspace-write"
DEFAULT_WORKER_APPROVAL_POLICY = "on-request"
DEFAULT_WORKER_TICK_TIMEOUT_SECONDS = 1200
MIN_SLEEP_SECONDS = 0
MAX_SLEEP_SECONDS = 3600
MAX_INPUT_EXCERPT_CHARS = 6000
MAX_LOG_ESTIMATE_LINES = 400
SYSTEM_SECTION_PREFIX = "Autoresearch System:"
MODE_RECENT_EVENT_LIMIT = 8
DEFAULT_RUNTIME_DIRNAME = "auto-codex"
STOP_SIGNALS = {signal.SIGINT, signal.SIGTERM}
GLOBAL_STOP_REQUESTED = False
JOB_TERMINAL_STATUSES = {"completed", "succeeded", "success", "done", "cancelled", "stopped", "not_in_queue"}
JOB_ATTENTION_STATUSES = {
    "failed",
    "error",
    "timeout",
    "oom",
    "out_of_memory",
    "node_fail",
    "preempted",
    "boot_fail",
    "deadline",
    "revoked",
}
JOB_RUNNING_STATUSES = {
    "submitted",
    "queued",
    "pending",
    "running",
    "configuring",
    "completing",
    "stage_out",
    "r",
    "pd",
    "cf",
    "cg",
    "so",
}
MAX_FOCUSED_JOB_COUNT = 5
VALID_SANDBOX_MODES = {"read-only", "workspace-write", "danger-full-access"}
VALID_APPROVAL_POLICIES = {"untrusted", "on-failure", "on-request", "never"}
CODEX_EXEC_SEARCH_SUPPORT: bool | None = None


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def iso_after_seconds(seconds: int) -> str:
    return (datetime.now().astimezone() + timedelta(seconds=max(0, seconds))).isoformat(timespec="seconds")


def normalize_job_status(status: str) -> str:
    lowered = status.strip().lower().rstrip("+")
    return re.split(r"[\s+]+", lowered, maxsplit=1)[0] if lowered else ""


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return cleaned or "mission"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(read_text(path))


def write_json(path: Path, payload: Any) -> None:
    write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def append_jsonl(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def load_template(name: str) -> Template:
    return Template(read_text(TEMPLATES_DIR / name))


def extract_title(markdown: str, fallback_name: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback_name


def extract_doc_urls(markdown: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for match in re.finditer(r"https?://[^\s)]+/(?:docx|doc|wiki)/[A-Za-z0-9]+", markdown):
        url = match.group(0)
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def extract_path_mentions(markdown: str) -> list[str]:
    seen: set[str] = set()
    paths: list[str] = []
    for match in re.finditer(r"(?<!https:)(?<!http:)(/[A-Za-z0-9._/\-]+)", markdown):
        candidate = match.group(1).rstrip(".,:;)]}")
        if len(candidate) < 2:
            continue
        if re.match(r"^/[A-Za-z0-9.-]+\.[A-Za-z]{2,}/", candidate):
            continue
        if candidate not in seen:
            seen.add(candidate)
            paths.append(candidate)
    return paths


def plan_steps_need_refinement(plan_steps: list[str]) -> bool:
    if not plan_steps:
        return True
    generic_needles = (
        "read the mission carefully",
        "extract constraints",
        "set up the runtime workspace",
        "identify the next executable unit of work",
        "run the highest-priority experiment",
        "record evidence",
        "queue follow-up work",
        "repeat until the mission is complete",
    )
    generic_hits = sum(1 for step in plan_steps if any(needle in step.lower() for needle in generic_needles))
    avg_words = sum(len(step.split()) for step in plan_steps) / max(1, len(plan_steps))
    return generic_hits >= max(2, len(plan_steps) // 2) or avg_words < 10


def select_relevant_path(paths: list[str], *, contains: tuple[str, ...] = (), suffixes: tuple[str, ...] = ()) -> str:
    lowered_contains = tuple(item.lower() for item in contains)
    lowered_suffixes = tuple(item.lower() for item in suffixes)
    for path in paths:
        lowered = path.lower()
        if lowered_contains and not any(item in lowered for item in lowered_contains):
            continue
        if lowered_suffixes and not any(lowered.endswith(item) for item in lowered_suffixes):
            continue
        return path
    return ""


def workspace_root_from_paths(paths: list[str], mission_path: Path | None = None) -> str:
    preferred = (
        select_relevant_path(paths, contains=("megatron-lm",))
        or select_relevant_path(paths, contains=("megatron",))
        or select_relevant_path(paths, contains=("workspace", "repo"))
        or (paths[0] if paths else "")
    )
    if preferred:
        candidate = Path(preferred)
        return str(candidate.parent if candidate.suffix else candidate)
    if mission_path is not None:
        return str(mission_path.parent)
    return ""


def mission_behavior_targets(markdown: str) -> list[str]:
    lowered = markdown.lower()
    targets: list[str] = []
    if "router" in lowered or "adaptive" in lowered:
        targets.append("router gating")
    if "probability" in lowered or "flow" in lowered or "adaptive" in lowered:
        targets.append("probability-flow mixing")
    if "ponder" in lowered or "adaptive" in lowered:
        targets.append("ponder regularization")
    if "训练" in markdown or "train" in lowered or "forward" in lowered:
        targets.append("multi-step training forward")
    if "generate" in lowered or "cache" in lowered or "kv" in lowered or "adaptive" in lowered:
        targets.append("generation/cache handling")
    if not targets:
        targets = [
            "behavioral invariants",
            "tensor shapes",
            "training/inference branches",
            "acceptance criteria",
        ]
    return targets


def synthesize_detailed_plan_steps(markdown: str, mission_path: Path | None = None, doc_urls: list[str] | None = None) -> list[str]:
    doc_urls = doc_urls or []
    paths = extract_path_mentions(markdown)
    source_file = select_relevant_path(paths, suffixes=(".py", ".cpp", ".cu", ".cc", ".c", ".go", ".rs", ".ts", ".tsx", ".js"))
    target_repo = workspace_root_from_paths(paths, mission_path=mission_path)

    behavior_targets = ", ".join(mission_behavior_targets(markdown))
    lowered = markdown.lower()
    mentions_sbatch = "sbatch" in lowered
    mentions_speed = "加速" in markdown or "speed" in lowered or "throughput" in lowered
    mentions_equivalence = "等价" in markdown or "equivalent" in lowered or "parity" in lowered
    mentions_megatron = "megatron" in lowered

    if mentions_megatron:
        step1 = (
            f"Clone a clean NVIDIA Megatron-LM baseline from the official GitHub repo into {target_repo}, "
            "record the exact upstream commit, bring up the recommended environment until a minimal baseline run path works, "
            "and keep the implementation surface isolated from unrelated changes."
            if target_repo
            else "Clone a clean NVIDIA Megatron-LM baseline from the official GitHub repo, record the exact upstream commit, "
            "bring up the recommended environment until a minimal baseline run path works, and isolate the implementation surface from unrelated changes."
        )
    else:
        step1 = (
            f"Freeze the working baseline under {target_repo}, record exact commits or versions, and isolate the implementation surface from unrelated changes."
            if target_repo
            else "Freeze the working baseline, record exact commits or versions, and isolate the implementation surface from unrelated changes."
        )
    if source_file:
        step2 = (
            f"Reverse-engineer {source_file} into concrete behaviors to preserve: {behavior_targets}."
        )
    else:
        step2 = (
            f"Reverse-engineer the mission's reference implementation into concrete behaviors to preserve: {behavior_targets}."
        )

    step3 = (
        "Choose the minimal target-stack integration seam for a correctness-first port, preferring config plumbing plus a sibling wrapper that keeps the baseline execution path intact unless a direct patch is clearly safer."
        if mentions_megatron
        else "Choose the minimal integration seam for a correctness-first implementation while keeping the baseline path intact unless a direct patch is clearly safer."
    )
    step4 = (
        f"Implement the first correctness-first path and add small local equivalence checks that compare forward and loss behavior against the reference{' to preserve ' + ('behavioral parity' if mentions_equivalence else 'core behavior')}."
    )
    step5 = (
        "Optimize the training-critical path only after correctness is established, then use sbatch jobs to measure throughput, functional parity, and regression risk on cluster runs."
        if mentions_sbatch or mentions_speed or mentions_equivalence
        else "Run higher-cost experiments only after correctness is established, and measure performance, behavior, and regression risk before broadening scope."
    )
    if doc_urls:
        step6 = (
            f"Continuously append concrete evidence, open risks, and next actions to {doc_urls[0]} and iterate until the implementation is accepted or a real blocker requires escalation."
        )
    else:
        step6 = (
            "Continuously append concrete evidence, open risks, and next actions to the designated report sink and iterate until the implementation is accepted or a real blocker requires escalation."
        )
    return [step1, step2, step3, step4, step5, step6]


def seeded_plan_steps(markdown: str, mission_path: Path | None = None, doc_urls: list[str] | None = None) -> list[dict[str, str]]:
    target_heading = re.compile(r"(plan|priority|workflow|step|步骤|顺序|工作优先级)", re.IGNORECASE)
    headings = re.compile(r"^##\s+(.+)$")
    ordered_item = re.compile(r"^\s*(?:[-*]|\d+\.)\s+(.+?)\s*$")

    active_target = False
    plan_steps: list[str] = []
    for raw_line in markdown.splitlines():
        heading_match = headings.match(raw_line.strip())
        if heading_match:
            active_target = bool(target_heading.search(heading_match.group(1)))
            continue
        if not active_target:
            continue
        item_match = ordered_item.match(raw_line)
        if item_match:
            plan_steps.append(item_match.group(1))

    if not plan_steps or plan_steps_need_refinement(plan_steps):
        plan_steps = synthesize_detailed_plan_steps(markdown, mission_path=mission_path, doc_urls=doc_urls)

    seeded: list[dict[str, str]] = []
    for index, step in enumerate(plan_steps):
        seeded.append(
            {
                "step": step,
                "status": "in_progress" if index == 0 else "pending",
            }
        )
    return seeded


def ensure_runtime_layout(runtime_dir: Path) -> dict[str, Path]:
    paths = {
        "runtime": runtime_dir,
        "state": runtime_dir / "state.json",
        "plan": runtime_dir / "plan.json",
        "plan_preview": runtime_dir / "plan.preview.json",
        "events": runtime_dir / "events.jsonl",
        "inputs": runtime_dir / "inputs.jsonl",
        "mission": runtime_dir / "mission.md",
        "runbook": runtime_dir / "runbook.md",
        "worker_prompt": runtime_dir / "prompts" / "worker_prompt.md",
        "supervisor_log": runtime_dir / "logs" / "supervisor.log",
        "codex_logs": runtime_dir / "logs" / "codex",
        "daemon_pid": runtime_dir / "supervisor.pid",
        "notes_dir": runtime_dir / "notes",
        "jobs_dir": runtime_dir / "jobs",
        "outbox_dir": runtime_dir / "outbox",
        "inbox_dir": runtime_dir / "inbox",
        "snapshots_dir": runtime_dir / "snapshots",
    }
    for path in paths.values():
        if path.suffix:
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True)
    return paths


def resolve_runtime_dir(runtime_dir_arg: str | None) -> Path:
    raw = (runtime_dir_arg or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.cwd() / DEFAULT_RUNTIME_DIRNAME).resolve()


def default_state(
    mission_path: Path,
    runtime_dir: Path,
    title: str,
    doc_urls: list[str],
    plan_steps: list[dict[str, str]],
) -> dict[str, Any]:
    created_at = now_iso()
    return {
        "mission": {
            "title": title,
            "source_path": str(mission_path),
            "runtime_path": str(runtime_dir / "mission.md"),
            "doc_urls": doc_urls,
        },
        "lifecycle": {
            "status": "initialized",
            "created_at": created_at,
            "updated_at": created_at,
            "stop_requested": False,
            "stop_reason": "",
            "completed_at": "",
        },
        "supervisor": {
            "active_model": DEFAULT_MODEL,
            "fallback_model": DEFAULT_FALLBACK_MODEL,
            "last_tick_at": "",
            "last_sleep_seconds": 0,
            "consecutive_failures": 0,
            "paused": False,
            "active_tick": {
                "running": False,
                "started_at": "",
                "model": "",
                "worker_pid": 0,
                "response_path": "",
                "session_log_path": "",
            },
            "worker_policy": {
                "sandbox_mode": DEFAULT_WORKER_SANDBOX,
                "approval_policy": DEFAULT_WORKER_APPROVAL_POLICY,
                "dangerous_bypass": False,
                "source": "default",
                "session_source": "",
            },
        },
        "progress": {
            "summary": "Plan preview generated. Waiting for confirmation before execution.",
            "last_worker_status": "",
            "artifacts_updated": [],
            "next_sleep_seconds": DEFAULT_INTERVAL_SECONDS,
            "sleep_reason": "Default one-hour polling interval.",
            "planned_wake_at": "",
        },
        "execution": {
            "current_phase": {
                "title": "",
                "goal": "",
                "related_plan_step": "",
                "related_job_ids": [],
                "status": "",
            },
            "phase_plan": [],
            "next_action": {
                "summary": "",
                "reason": "",
                "primary_target": "",
                "resume_targets": [],
                "search_patterns": [],
                "read_ladder": [],
                "success_condition": "",
                "fallback_if_missing": "",
            },
            "updated_at": created_at,
        },
        "plan": {
            "items": [],
        },
        "planning": {
            "preview": {
                "version": 1,
                "items": deepcopy(plan_steps),
                "generated_at": created_at,
                "source": "mission_seeded",
                "approved": False,
                "approved_at": "",
                "revision_note": "",
            },
            "current": {
                "version": 0,
                "items": [],
                "approved_at": "",
            },
        },
        "jobs": {},
        "history": {
            "lark_updates_sent": 0,
            "ticks_completed": 0,
        },
        "inputs": {
            "total": 0,
            "pending": 0,
            "last_added_at": "",
            "last_acknowledged_at": "",
        },
        "lark": {
            "poll_interval_seconds": DEFAULT_LARK_POLL_INTERVAL_SECONDS,
            "heartbeat_interval_seconds": DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
            "last_poll_at": "",
            "last_heartbeat_at": "",
            "last_user_visible_hash": "",
            "last_snapshot_path": "",
            "final_summary_written": False,
        },
    }


def render_worker_prompt(runtime_dir: Path, state: dict[str, Any]) -> str:
    pending_inputs = [item for item in load_inputs(runtime_dir) if item.get("status") == "pending"]
    pending_inputs_path = write_pending_inputs_markdown(runtime_dir, pending_inputs)
    pending_summary_lines = []
    for item in pending_inputs[-10:]:
        title = f" | {item['title']}" if item.get("title") else ""
        pending_summary_lines.append(
            f"- {item['id']} | {item['source']} | {item['created_at']}{title}\n  {truncate_text(item.get('content', ''), 240)}"
        )
    pending_inputs_summary = "\n".join(pending_summary_lines) if pending_summary_lines else "- none"
    focused_job_ids = select_focused_job_ids(runtime_dir, state)
    focused_jobs_path = write_focused_jobs_markdown(runtime_dir, state, focused_job_ids)
    focused_job_ids_text = ", ".join(focused_job_ids) if focused_job_ids else "none"

    template = load_template("worker_prompt.md.tmpl")
    return template.substitute(
        runtime_dir=str(runtime_dir),
        mission_path=str(runtime_dir / "mission.md"),
        state_path=str(runtime_dir / "state.json"),
        plan_path=str(runtime_dir / "plan.json"),
        pending_inputs_path=str(pending_inputs_path),
        pending_inputs_summary=pending_inputs_summary,
        resume_status_path=str(runtime_dir / "notes" / "resume_status.md"),
        focused_jobs_path=str(focused_jobs_path),
        focused_job_ids=focused_job_ids_text,
        jobs_dir=str(runtime_dir / "jobs"),
        notes_dir=str(runtime_dir / "notes"),
        live_status_path=str(runtime_dir / "notes" / "live_status.md"),
        outbox_dir=str(runtime_dir / "outbox"),
        worker_schema_path=str(SCHEMA_PATH),
        active_model=state["supervisor"]["active_model"],
    )


def render_runbook(state: dict[str, Any]) -> str:
    template = load_template("runbook.md.tmpl")
    doc_urls = state["mission"]["doc_urls"]
    doc_lines = "\n".join(f"- {url}" for url in doc_urls) if doc_urls else "- none detected"
    return template.substitute(
        title=state["mission"]["title"],
        created_at=state["lifecycle"]["created_at"],
        source_path=state["mission"]["source_path"],
        runtime_path=state["mission"]["runtime_path"],
        doc_urls=doc_lines,
    )


def append_event(runtime_dir: Path, event_type: str, payload: dict[str, Any]) -> None:
    append_jsonl(
        runtime_dir / "events.jsonl",
        {
            "ts": now_iso(),
            "type": event_type,
            "payload": payload,
        },
    )


def ensure_state_defaults(state: dict[str, Any]) -> dict[str, Any]:
    state.setdefault("mission", {})
    state.setdefault("lifecycle", {})
    state.setdefault("supervisor", {})
    state.setdefault("progress", {})
    state.setdefault("execution", {})
    state.setdefault("plan", {"items": []})
    state.setdefault("planning", {})
    state.setdefault("jobs", {})
    state.setdefault("history", {})
    state.setdefault("inputs", {})
    state.setdefault("lark", {})

    state["lifecycle"].setdefault("status", "initialized")
    state["lifecycle"].setdefault("created_at", "")
    state["lifecycle"].setdefault("updated_at", "")
    state["lifecycle"].setdefault("stop_requested", False)
    state["lifecycle"].setdefault("stop_reason", "")
    state["lifecycle"].setdefault("completed_at", "")
    if state["lifecycle"].get("status") == "initialized" and not state.get("plan", {}).get("items"):
        state["lifecycle"]["status"] = "awaiting_plan_confirmation"

    state["supervisor"].setdefault("active_model", DEFAULT_MODEL)
    state["supervisor"].setdefault("fallback_model", DEFAULT_FALLBACK_MODEL)
    state["supervisor"].setdefault("last_tick_at", "")
    state["supervisor"].setdefault("last_sleep_seconds", 0)
    state["supervisor"].setdefault("consecutive_failures", 0)
    state["supervisor"].setdefault("paused", False)
    active_tick = state["supervisor"].setdefault("active_tick", {})
    active_tick.setdefault("running", False)
    active_tick.setdefault("started_at", "")
    active_tick.setdefault("model", "")
    active_tick.setdefault("worker_pid", 0)
    active_tick.setdefault("response_path", "")
    active_tick.setdefault("session_log_path", "")

    state["progress"].setdefault("summary", "")
    state["progress"].setdefault("last_worker_status", "")
    state["progress"].setdefault("artifacts_updated", [])
    state["progress"].setdefault("next_sleep_seconds", DEFAULT_INTERVAL_SECONDS)
    state["progress"].setdefault("sleep_reason", "")
    state["progress"].setdefault("planned_wake_at", "")

    execution = state["execution"]
    current_phase = execution.setdefault("current_phase", {})
    current_phase.setdefault("title", "")
    current_phase.setdefault("goal", "")
    current_phase.setdefault("related_plan_step", "")
    current_phase.setdefault("related_job_ids", [])
    current_phase.setdefault("status", "")
    execution.setdefault("phase_plan", [])
    next_action = execution.setdefault("next_action", {})
    next_action.setdefault("summary", "")
    next_action.setdefault("reason", "")
    next_action.setdefault("primary_target", "")
    next_action.setdefault("resume_targets", [])
    next_action.setdefault("search_patterns", [])
    next_action.setdefault("read_ladder", [])
    next_action.setdefault("success_condition", "")
    next_action.setdefault("fallback_if_missing", "")
    execution.setdefault("updated_at", "")

    planning = state["planning"]
    preview = planning.setdefault("preview", {})
    current = planning.setdefault("current", {})
    preview.setdefault("version", 1)
    preview.setdefault("items", [])
    preview.setdefault("generated_at", "")
    preview.setdefault("source", "mission_seeded")
    preview.setdefault("approved", False)
    preview.setdefault("approved_at", "")
    preview.setdefault("revision_note", "")
    current.setdefault("version", 0)
    current.setdefault("items", [])
    current.setdefault("approved_at", "")

    # Migrate older runtimes that only stored a single state["plan"].
    if not current["items"] and state.get("plan", {}).get("items"):
        current["items"] = deepcopy(state["plan"]["items"])
        if current["version"] <= 0:
            current["version"] = 1
        if not current["approved_at"]:
            current["approved_at"] = state["lifecycle"].get("updated_at", "")
    if not preview["items"] and not current["items"] and state.get("plan", {}).get("items"):
        preview["items"] = deepcopy(state["plan"]["items"])
    state["plan"] = {"items": deepcopy(current["items"])}

    state["inputs"].setdefault("total", 0)
    state["inputs"].setdefault("pending", 0)
    state["inputs"].setdefault("last_added_at", "")
    state["inputs"].setdefault("last_acknowledged_at", "")

    state["lark"].setdefault("poll_interval_seconds", DEFAULT_LARK_POLL_INTERVAL_SECONDS)
    state["lark"].setdefault("heartbeat_interval_seconds", DEFAULT_HEARTBEAT_INTERVAL_SECONDS)
    state["lark"].setdefault("last_poll_at", "")
    state["lark"].setdefault("last_heartbeat_at", "")
    state["lark"].setdefault("last_user_visible_hash", "")
    state["lark"].setdefault("last_snapshot_path", "")
    state["lark"].setdefault("final_summary_written", False)
    return state


def write_plan_markdown(runtime_dir: Path, state: dict[str, Any]) -> None:
    lines = ["# Current Plan", "", f"Updated: {now_iso()}", ""]
    current_items = state.get("planning", {}).get("current", {}).get("items", state.get("plan", {}).get("items", []))
    for item in current_items:
        status = item.get("status", "pending")
        step = item.get("step", "")
        lines.append(f"- [{status}] {step}")
    lines.append("")
    write_text(runtime_dir / "notes" / "plan.md", "\n".join(lines))


def write_plan_preview_markdown(runtime_dir: Path, state: dict[str, Any]) -> None:
    preview = state.get("planning", {}).get("preview", {})
    lines = [
        "# Plan Preview",
        "",
        f"Updated: {now_iso()}",
        f"Version: {preview.get('version', 1)}",
        "",
    ]
    if preview.get("revision_note"):
        lines.extend(["## Latest Revision Note", "", preview["revision_note"], ""])
    lines.extend(["## Proposed Steps", ""])
    for item in preview.get("items", []):
        status = item.get("status", "pending")
        step = item.get("step", "")
        lines.append(f"- [{status}] {step}")
    if not preview.get("items"):
        lines.append("- none")
    lines.append("")
    write_text(runtime_dir / "notes" / "plan_preview.md", "\n".join(lines))


def write_execution_markdown(runtime_dir: Path, state: dict[str, Any]) -> None:
    execution = state.get("execution", {})
    current_phase = execution.get("current_phase", {})
    next_action = execution.get("next_action", {})
    progress = state.get("progress", {})
    lines = [
        "# Resume Status",
        "",
        f"Updated: {execution.get('updated_at', now_iso())}",
        "",
        "## Current Phase",
        "",
        f"- title: {current_phase.get('title', '') or 'none'}",
        f"- goal: {current_phase.get('goal', '') or 'none'}",
        f"- related_plan_step: {current_phase.get('related_plan_step', '') or 'none'}",
        f"- status: {current_phase.get('status', '') or 'none'}",
        "",
        "## Phase Plan",
        "",
    ]
    phase_plan = execution.get("phase_plan", [])
    if phase_plan:
        lines.extend(f"- {item}" for item in phase_plan)
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Next Action",
            "",
            f"- summary: {next_action.get('summary', '') or 'none'}",
            f"- reason: {next_action.get('reason', '') or 'none'}",
            f"- primary_target: {next_action.get('primary_target', '') or 'none'}",
            f"- sleep_hint: {(progress.get('sleep_reason', '') or 'none')}",
            f"- success_condition: {next_action.get('success_condition', '') or 'none'}",
            f"- fallback_if_missing: {next_action.get('fallback_if_missing', '') or 'none'}",
            "",
            "### Resume Targets",
            "",
        ]
    )
    resume_targets = next_action.get("resume_targets", [])
    if resume_targets:
        lines.extend(f"- {item}" for item in resume_targets)
    else:
        lines.append("- none")
    lines.extend(["", "### Search Patterns", ""])
    search_patterns = next_action.get("search_patterns", [])
    if search_patterns:
        lines.extend(f"- {item}" for item in search_patterns)
    else:
        lines.append("- none")
    lines.extend(["", "### Read Ladder", ""])
    read_ladder = next_action.get("read_ladder", [])
    if read_ladder:
        lines.extend(f"- {item}" for item in read_ladder)
    else:
        lines.append("- none")
    lines.append("")
    write_text(runtime_dir / "notes" / "resume_status.md", "\n".join(lines))


def write_live_status_markdown(runtime_dir: Path, state: dict[str, Any]) -> None:
    live = collect_live_worker_snapshot(runtime_dir, state)
    lines = [
        "# Live Status",
        "",
        f"Updated: {now_iso()}",
        "",
        f"- lifecycle: {state.get('lifecycle', {}).get('status', 'unknown')}",
        f"- summary: {state.get('progress', {}).get('summary', '') or 'none'}",
        f"- next_sleep_seconds: {state.get('progress', {}).get('next_sleep_seconds', 0)}",
        "",
    ]
    if live.get("running"):
        lines.extend(
            [
                "## In-Flight Worker Burst",
                "",
                f"- started_at: {live.get('started_at', '') or 'none'}",
                f"- elapsed: {live.get('elapsed', '') or '0s'}",
                f"- model: {live.get('model', '') or 'none'}",
                f"- worker_pid: {live.get('worker_pid', 'none')}",
                f"- session_log_path: {live.get('session_log_path', '') or 'none'}",
                f"- current_action: {live.get('current_action', '') or 'none'}",
                f"- next_hint: {live.get('next_hint', '') or 'none'}",
                "",
                "## Recent Actions",
                "",
            ]
        )
        recent_actions = live.get("recent_actions", [])
        if recent_actions:
            lines.extend(f"- {item}" for item in recent_actions)
        else:
            lines.append("- none recorded yet")
    else:
        lines.extend(["## In-Flight Worker Burst", "", "- none"])
    lines.append("")
    write_text(runtime_dir / "notes" / "live_status.md", "\n".join(lines))


def save_state(runtime_dir: Path, state: dict[str, Any]) -> None:
    state = ensure_state_defaults(state)
    merge_execution_packet(runtime_dir, state, packet={}, overwrite_missing_only=True)
    state["execution"]["current_phase"]["status"] = state["lifecycle"].get("status", "")
    state["lifecycle"]["updated_at"] = now_iso()
    state["plan"] = {"items": deepcopy(state.get("planning", {}).get("current", {}).get("items", []))}
    write_json(runtime_dir / "state.json", state)
    write_json(runtime_dir / "plan.json", state.get("plan", {"items": []}))
    write_json(runtime_dir / "plan.preview.json", state.get("planning", {}).get("preview", {"items": []}))
    write_plan_markdown(runtime_dir, state)
    write_plan_preview_markdown(runtime_dir, state)
    write_execution_markdown(runtime_dir, state)
    write_live_status_markdown(runtime_dir, state)


def load_state(runtime_dir: Path) -> dict[str, Any]:
    state = ensure_state_defaults(read_json(runtime_dir / "state.json"))
    merge_execution_packet(runtime_dir, state, packet={}, overwrite_missing_only=True)
    state["execution"]["current_phase"]["status"] = state["lifecycle"].get("status", "")
    return state


def load_inputs(runtime_dir: Path) -> list[dict[str, Any]]:
    path = runtime_dir / "inputs.jsonl"
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    return items


def write_inputs(runtime_dir: Path, items: list[dict[str, Any]]) -> None:
    path = runtime_dir / "inputs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def truncate_text(content: str, limit: int) -> str:
    normalized = content.strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def write_pending_inputs_markdown(runtime_dir: Path, items: list[dict[str, Any]]) -> Path:
    lines = ["# Pending Inputs", "", f"Updated: {now_iso()}", ""]
    if not items:
        lines.append("- none")
    else:
        for item in items:
            lines.extend(
                [
                    f"## {item['id']}",
                    f"- source: {item.get('source', '')}",
                    f"- author: {item.get('author', '')}",
                    f"- created_at: {item.get('created_at', '')}",
                    f"- title: {item.get('title', '') or '(none)'}",
                    "",
                    item.get("content", "").strip(),
                    "",
                ]
            )
    path = runtime_dir / "notes" / "pending_inputs.md"
    write_text(path, "\n".join(lines))
    return path


def load_events(runtime_dir: Path) -> list[dict[str, Any]]:
    path = runtime_dir / "events.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            events.append(json.loads(line))
    return events


def refresh_input_counters(state: dict[str, Any], items: list[dict[str, Any]]) -> None:
    pending = [item for item in items if item.get("status") == "pending"]
    acknowledged = [item for item in items if item.get("status") == "acknowledged"]
    state["inputs"] = {
        "total": len(items),
        "pending": len(pending),
        "last_added_at": items[-1]["created_at"] if items else "",
        "last_acknowledged_at": acknowledged[-1]["acknowledged_at"] if acknowledged else "",
    }


def summarize_jobs(state: dict[str, Any], limit: int = 10) -> list[str]:
    lines: list[str] = []
    for job_id, metadata in list(state.get("jobs", {}).items())[:limit]:
        status = metadata.get("status", "unknown")
        script = Path(metadata.get("script", "")).name if metadata.get("script") else ""
        suffix = f" | {script}" if script else ""
        lines.append(f"- `{job_id}`: {status}{suffix}")
    return lines or ["- none"]


def current_plan_items(state: dict[str, Any]) -> list[dict[str, str]]:
    return deepcopy(state.get("planning", {}).get("current", {}).get("items", state.get("plan", {}).get("items", [])))


def preview_plan_items(state: dict[str, Any]) -> list[dict[str, str]]:
    return deepcopy(state.get("planning", {}).get("preview", {}).get("items", []))


def set_current_plan_items(state: dict[str, Any], items: list[dict[str, str]]) -> None:
    state["planning"]["current"]["items"] = deepcopy(items)
    state["plan"]["items"] = deepcopy(items)


def set_preview_plan_items(state: dict[str, Any], items: list[dict[str, str]], source: str, revision_note: str = "") -> None:
    preview = state["planning"]["preview"]
    preview["version"] = int(preview.get("version", 0)) + 1
    preview["items"] = deepcopy(items)
    preview["generated_at"] = now_iso()
    preview["source"] = source
    preview["approved"] = False
    preview["approved_at"] = ""
    preview["revision_note"] = revision_note.strip()


def lifecycle_plan_items(state: dict[str, Any]) -> list[dict[str, str]]:
    if state.get("lifecycle", {}).get("status") == "awaiting_plan_confirmation":
        return preview_plan_items(state)
    return current_plan_items(state)


def active_plan_item(state: dict[str, Any]) -> tuple[int, dict[str, str] | None]:
    items = lifecycle_plan_items(state)
    if not items:
        return -1, None
    for index, item in enumerate(items):
        if item.get("status") != "completed":
            return index, item
    return len(items) - 1, items[-1]


def looks_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return len(value) == 0
    if isinstance(value, dict):
        return len(value) == 0 or all(looks_missing(item) for item in value.values())
    return False


def clamp_sleep_seconds(value: int | float) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = DEFAULT_INTERVAL_SECONDS
    return max(MIN_SLEEP_SECONDS, min(MAX_SLEEP_SECONDS, seconds))


def format_sleep_duration(seconds: int | float) -> str:
    try:
        total_seconds = max(0, int(seconds))
    except (TypeError, ValueError, OverflowError):
        total_seconds = 0
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def has_delayed_wake(state: dict[str, Any]) -> bool:
    try:
        seconds = int(state.get("progress", {}).get("next_sleep_seconds", 0))
    except (TypeError, ValueError):
        seconds = 0
    return seconds > 0


def tail_lines(path: Path, max_lines: int) -> list[str]:
    if not path.exists() or not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    return [line.rstrip("\n") for line in lines[-max_lines:]]


def expand_metric_number(raw: str) -> int:
    cleaned = raw.strip().lower().replace(",", "")
    multiplier = 1
    if cleaned.endswith("k"):
        multiplier = 1000
        cleaned = cleaned[:-1]
    try:
        return int(float(cleaned) * multiplier)
    except ValueError:
        return 0


def extract_target_steps(texts: list[str]) -> list[int]:
    targets: list[int] = []
    patterns = [
        re.compile(r"\b(\d+(?:\.\d+)?)\s*k\b", re.IGNORECASE),
        re.compile(r"\b(\d{4,7})\b"),
        re.compile(r"\beval(?:uate|uation)?\s+every\s+(\d+(?:\.\d+)?)(k)?\s*(?:steps?|iters?|iterations?)", re.IGNORECASE),
        re.compile(r"\bevery\s+(\d+(?:\.\d+)?)(k)?\s*(?:steps?|iters?|iterations?)", re.IGNORECASE),
    ]
    for text in texts:
        if not text:
            continue
        for match in patterns[0].finditer(text):
            value = expand_metric_number(match.group(1) + "k")
            if value >= 1000 and value not in targets:
                targets.append(value)
        for match in patterns[1].finditer(text):
            value = expand_metric_number(match.group(1))
            if value >= 1000 and value not in targets:
                targets.append(value)
        for pattern in patterns[2:]:
            for match in pattern.finditer(text):
                raw = match.group(1) + ("k" if match.lastindex and match.group(2) else "")
                value = expand_metric_number(raw)
                if value >= 10 and value not in targets:
                    targets.append(value)
    return sorted(targets)


def extract_current_step(lines: list[str]) -> int:
    step_patterns = [
        re.compile(r"\b(?:step|steps|iter|iteration|global[_ -]?step)\b[^0-9]{0,12}(\d{1,7})", re.IGNORECASE),
        re.compile(r"\b(\d{1,7})\s*/\s*(\d{1,7})\b"),
        re.compile(r"\bglobal[_ -]?step\s*[=:]\s*(\d{1,7})", re.IGNORECASE),
        re.compile(r"\biteration\s*[=:]?\s*(\d{1,7})\b", re.IGNORECASE),
    ]
    best = 0
    for line in lines:
        for pattern in step_patterns:
            match = pattern.search(line)
            if not match:
                continue
            value = int(match.group(1))
            if value > best:
                best = value
    return best


def extract_seconds_per_iter(lines: list[str]) -> float:
    patterns = [
        re.compile(r"(\d+(?:\.\d+)?)\s*s/it\b", re.IGNORECASE),
        re.compile(r"(\d+(?:\.\d+)?)\s*sec(?:onds)?/it\b", re.IGNORECASE),
        re.compile(r"(?:iter(?:ation)?[_ -]?time|time per iter(?:ation)?)\s*[=:]?\s*(\d+(?:\.\d+)?)\s*s\b", re.IGNORECASE),
        re.compile(r"(?:samples/sec|tokens/sec)[^0-9]{0,12}(\d+(?:\.\d+)?)", re.IGNORECASE),
        re.compile(r"it/s[^0-9]{0,8}(\d+(?:\.\d+)?)", re.IGNORECASE),
        re.compile(r"(\d+(?:\.\d+)?)\s*it/s\b", re.IGNORECASE),
    ]
    candidates: list[float] = []
    for line in lines:
        lowered = line.lower()
        for index, pattern in enumerate(patterns):
            match = pattern.search(lowered)
            if not match:
                continue
            value = float(match.group(1))
            if index == 3:
                # samples/sec or tokens/sec alone are not enough to infer iteration time.
                continue
            if index in {4, 5}:
                if value > 0:
                    candidates.append(1.0 / value)
            else:
                candidates.append(value)
    return candidates[-1] if candidates else 0.0


def infer_target_step(current_step: int, candidate_texts: list[str]) -> int:
    targets = extract_target_steps(candidate_texts)
    larger_targets = [value for value in targets if value > current_step]
    if larger_targets:
        return min(larger_targets)

    interval_patterns = [
        re.compile(r"\beval(?:uate|uation)?\s+every\s+(\d+(?:\.\d+)?)(k)?\s*(?:steps?|iters?|iterations?)", re.IGNORECASE),
        re.compile(r"\bevery\s+(\d+(?:\.\d+)?)(k)?\s*(?:steps?|iters?|iterations?)", re.IGNORECASE),
    ]
    for text in candidate_texts:
        for pattern in interval_patterns:
            match = pattern.search(text)
            if not match:
                continue
            raw = match.group(1) + ("k" if match.lastindex and match.group(2) else "")
            interval = expand_metric_number(raw)
            if interval > 0 and current_step > 0:
                return ((current_step // interval) + 1) * interval
    return 0


def infer_primary_log_path(runtime_dir: Path, state: dict[str, Any]) -> Path | None:
    def resolve_candidate(raw: str) -> Path | None:
        direct = Path(raw).expanduser()
        if direct.exists() and direct.is_file():
            return direct.resolve()
        if not direct.is_absolute():
            nested = (runtime_dir / direct).resolve()
            if nested.exists() and nested.is_file():
                return nested
            name_only = (runtime_dir / direct.name).resolve()
            if name_only.exists() and name_only.is_file():
                return name_only
        return None

    next_action = state.get("execution", {}).get("next_action", {})
    candidates: list[str] = []
    for job_id in select_focused_job_ids(runtime_dir, state):
        metadata = state.get("jobs", {}).get(job_id, {})
        if metadata.get("log_out"):
            candidates.append(str(metadata.get("log_out")))
        if metadata.get("log_err"):
            candidates.append(str(metadata.get("log_err")))
    if next_action.get("primary_target"):
        candidates.append(str(next_action.get("primary_target")))
    for item in next_action.get("resume_targets", []):
        candidates.append(str(item))
    for raw in candidates:
        resolved = resolve_candidate(raw)
        if resolved is not None:
            return resolved
    return None


def estimate_sleep_from_logs(runtime_dir: Path, state: dict[str, Any]) -> tuple[int, str] | None:
    log_path = infer_primary_log_path(runtime_dir, state)
    if log_path is None:
        return None
    lines = tail_lines(log_path, MAX_LOG_ESTIMATE_LINES)
    if not lines:
        return None

    seconds_per_iter = extract_seconds_per_iter(lines)
    current_step = extract_current_step(lines)
    next_action = state.get("execution", {}).get("next_action", {})
    candidate_texts = [
        str(next_action.get("summary", "")),
        str(next_action.get("reason", "")),
        str(next_action.get("success_condition", "")),
    ]
    candidate_texts.extend(str(item) for item in next_action.get("search_patterns", []))
    target_step = infer_target_step(current_step, candidate_texts)
    if seconds_per_iter <= 0 or current_step <= 0 or target_step <= current_step:
        return None
    remaining_steps = max(0, target_step - current_step)
    if remaining_steps <= 0:
        return None

    estimated_seconds = int(remaining_steps * seconds_per_iter)
    recommended_sleep = clamp_sleep_seconds(estimated_seconds)
    reason = (
        f"Estimated {estimated_seconds}s until step {target_step} from step {current_step} "
        f"using {seconds_per_iter:.3f}s/it from {log_path}; capped to {recommended_sleep}s."
    )
    return recommended_sleep, reason


def choose_sleep_policy(runtime_dir: Path, state: dict[str, Any], worker_result: dict[str, Any]) -> tuple[int, str]:
    worker_status = worker_result.get("status", "")
    default_sleep = DEFAULT_INTERVAL_SECONDS if worker_status == "waiting_job" else DEFAULT_FAILURE_RETRY_SECONDS
    worker_sleep = clamp_sleep_seconds(worker_result.get("next_sleep_seconds", default_sleep))
    worker_reason = str(worker_result.get("sleep_reason", "")).strip()

    if worker_status == "working":
        if not worker_reason:
            worker_reason = "No external wait or cluster job is blocking progress; continue immediately with the next worker burst."
        return 0, worker_reason

    if worker_status != "waiting_job":
        if not worker_reason:
            worker_reason = (
                "Use the worker-provided cadence. If this is an internal retry or uncertain recovery path, "
                "prefer the default five-minute retry window instead of a one-hour wait."
            )
        return worker_sleep, worker_reason

    helper_estimate = estimate_sleep_from_logs(runtime_dir, state)
    if helper_estimate is not None:
        helper_sleep, helper_reason = helper_estimate
        if not worker_reason or worker_sleep >= DEFAULT_INTERVAL_SECONDS:
            return helper_sleep, helper_reason
        if helper_sleep < worker_sleep:
            return helper_sleep, f"{helper_reason} Worker requested {worker_sleep}s; using the earlier checkpoint."

    if not worker_reason:
        worker_reason = "No reliable runtime estimate was returned; fall back to the default one-hour polling interval."
    return worker_sleep, worker_reason


def job_status_bucket(status: str) -> int:
    lowered = normalize_job_status(status)
    if lowered in JOB_ATTENTION_STATUSES:
        return 0
    if lowered in JOB_RUNNING_STATUSES:
        return 1
    if lowered in JOB_TERMINAL_STATUSES:
        return 3
    return 2


def job_is_active(metadata: dict[str, Any]) -> bool:
    return normalize_job_status(str(metadata.get("status", ""))) in JOB_RUNNING_STATUSES


def has_active_jobs(state: dict[str, Any]) -> bool:
    return any(job_is_active(metadata) for metadata in state.get("jobs", {}).values())


def iso_timestamp_rank(value: str) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


def job_sort_key(metadata: dict[str, Any]) -> tuple[int, float, float]:
    status = str(metadata.get("status", ""))
    last_seen = str(metadata.get("last_seen_at", ""))
    submitted = str(metadata.get("submitted_at", ""))
    return (job_status_bucket(status), -iso_timestamp_rank(last_seen or submitted), -iso_timestamp_rank(submitted))


def extract_referenced_job_ids(state: dict[str, Any], runtime_dir: Path) -> list[str]:
    known = list(state.get("jobs", {}).keys())
    if not known:
        return []
    text_parts = [state.get("progress", {}).get("summary", "")]
    text_parts.extend(item.get("step", "") for item in current_plan_items(state))
    text_parts.extend(item.get("step", "") for item in preview_plan_items(state))
    for item in load_inputs(runtime_dir):
        if item.get("status") == "pending":
            text_parts.append(item.get("title", ""))
            text_parts.append(item.get("content", ""))
    joined = "\n".join(part for part in text_parts if part)
    found: list[str] = []
    for job_id in known:
        if job_id and job_id in joined and job_id not in found:
            found.append(job_id)
    return found


def select_focused_job_ids(runtime_dir: Path, state: dict[str, Any], limit: int = MAX_FOCUSED_JOB_COUNT) -> list[str]:
    jobs = state.get("jobs", {})
    if not jobs:
        return []

    chosen: list[str] = []
    referenced = extract_referenced_job_ids(state, runtime_dir)
    for job_id in referenced:
        if job_id in jobs and job_id not in chosen:
            chosen.append(job_id)

    active_sorted = sorted(jobs.items(), key=lambda item: job_sort_key(item[1]))
    for job_id, metadata in active_sorted:
        status = normalize_job_status(str(metadata.get("status", "")))
        if status in JOB_TERMINAL_STATUSES:
            continue
        if job_id not in chosen:
            chosen.append(job_id)
        if len(chosen) >= limit:
            break

    if not chosen:
        for job_id, _metadata in active_sorted[:limit]:
            chosen.append(job_id)
    return chosen[:limit]


def write_focused_jobs_markdown(runtime_dir: Path, state: dict[str, Any], focused_job_ids: list[str]) -> Path:
    path = runtime_dir / "notes" / "jobs_focus.md"
    lines = [
        "# Focused Jobs",
        "",
        f"Updated: {now_iso()}",
        "",
        "Read this file first. Only open detailed job JSON files or logs for these focused jobs unless new evidence makes a different job necessary.",
        "",
    ]
    if not focused_job_ids:
        lines.extend(["- none", ""])
        write_text(path, "\n".join(lines))
        return path

    for job_id in focused_job_ids:
        metadata = state.get("jobs", {}).get(job_id, {})
        lines.append(f"## Job {job_id}")
        lines.append("")
        lines.append(f"- status: {metadata.get('status', 'unknown')}")
        if metadata.get("script"):
            lines.append(f"- script: {metadata.get('script')}")
        if metadata.get("submitted_at"):
            lines.append(f"- submitted_at: {metadata.get('submitted_at')}")
        if metadata.get("last_seen_at"):
            lines.append(f"- last_seen_at: {metadata.get('last_seen_at')}")
        if metadata.get("queue_time"):
            lines.append(f"- queue_time: {metadata.get('queue_time')}")
        if metadata.get("log_out"):
            lines.append(f"- log_out: {metadata.get('log_out')}")
        if metadata.get("log_err"):
            lines.append(f"- log_err: {metadata.get('log_err')}")
        if metadata.get("notes"):
            lines.append(f"- notes: {truncate_text(str(metadata.get('notes', '')), 240)}")
        lines.append("")
    write_text(path, "\n".join(lines))
    return path


def derive_phase_title(step: str, lifecycle: str) -> str:
    cleaned = step.strip()
    if cleaned:
        return truncate_text(cleaned, 120)
    if lifecycle == "awaiting_plan_confirmation":
        return "Plan confirmation"
    if lifecycle == "waiting_job":
        return "Job verification"
    if lifecycle == "blocked":
        return "Blocker resolution"
    return "Execution"


def derive_execution_defaults(runtime_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    lifecycle = state.get("lifecycle", {}).get("status", "")
    phase_index, phase_item = active_plan_item(state)
    phase_step = phase_item.get("step", "").strip() if phase_item else ""
    focused_job_ids = select_focused_job_ids(runtime_dir, state)
    primary_job_id = focused_job_ids[0] if focused_job_ids else ""
    primary_job = state.get("jobs", {}).get(primary_job_id, {}) if primary_job_id else {}
    primary_target = str(primary_job.get("log_out") or primary_job.get("log_err") or (runtime_dir / "notes" / "jobs_focus.md"))

    if lifecycle == "awaiting_plan_confirmation":
        return {
            "current_phase": {
                "title": "Plan confirmation",
                "goal": "Review the proposed plan and confirm that the first execution step is correct before any work starts.",
                "related_plan_step": phase_step,
                "related_job_ids": [],
                "status": lifecycle,
            },
            "phase_plan": [
                "Read notes/plan_preview.md and compare it against mission constraints.",
                "Adjust priorities or boundaries if the next execution step is not the best one.",
                "Approve the plan only when the immediate next step is clear.",
            ],
            "next_action": {
                "summary": "Review the proposed plan and either approve it or revise it before execution starts.",
                "reason": "Execution is intentionally gated on plan confirmation.",
                "primary_target": str(runtime_dir / "notes" / "plan_preview.md"),
                "resume_targets": [
                    str(runtime_dir / "mission.md"),
                    str(runtime_dir / "notes" / "plan_preview.md"),
                ],
                "search_patterns": [],
                "read_ladder": [
                    "Read notes/plan_preview.md.",
                    "Cross-check the preview against mission.md constraints and priorities.",
                    "Approve the plan or revise it with a precise note.",
                ],
                "success_condition": "The plan is approved or a revised preview is saved with a clearer first step.",
                "fallback_if_missing": "If the preview is too vague, revise it before allowing execution.",
            },
        }

    if lifecycle == "waiting_job" or primary_job_id:
        return {
            "current_phase": {
                "title": derive_phase_title(phase_step, "waiting_job"),
                "goal": f"Verify the outcome of the current job path and decide whether the latest change worked{f' for job {primary_job_id}' if primary_job_id else ''}.",
                "related_plan_step": phase_step,
                "related_job_ids": focused_job_ids,
                "status": lifecycle or "waiting_job",
            },
            "phase_plan": [
                f"Inspect the focused job summary first: {runtime_dir / 'notes' / 'jobs_focus.md'}.",
                f"Search the primary job output for the expected signal before reading larger chunks{f' ({primary_job_id})' if primary_job_id else ''}.",
                "If the signal is missing, widen the read budget step by step and only then expand to other jobs.",
            ],
            "next_action": {
                "summary": f"Check whether the expected signal has appeared in the current job output{f' for job {primary_job_id}' if primary_job_id else ''}.",
                "reason": "A waiting job should be inspected with precise retrieval before broader log reading.",
                "primary_target": primary_target,
                "resume_targets": [str(runtime_dir / "notes" / "jobs_focus.md")] + ([primary_target] if primary_target else []),
                "search_patterns": ["10k", "10000", "eval", "eval loss", "validation"],
                "read_ladder": [
                    "Run rg on the primary target for the expected signal and nearby keywords.",
                    "If there is no match, read the last 20 lines of the target log.",
                    "If still unclear, read the last 200 lines or a narrow local window around related matches.",
                    "Only then read the whole target log, and only after that expand to other jobs.",
                ],
                "success_condition": "You can confirm whether the expected metric or failure signal appeared in the target job output.",
                "fallback_if_missing": "If the focused job still gives no answer, expand to the remaining focused jobs before scanning the full jobs directory.",
            },
        }

    return {
        "current_phase": {
            "title": derive_phase_title(phase_step, lifecycle),
            "goal": phase_step or "Continue the highest-priority remaining execution phase.",
            "related_plan_step": phase_step,
            "related_job_ids": focused_job_ids,
            "status": lifecycle or "running",
        },
        "phase_plan": [
            f"Continue the active plan step: {phase_step or 'pick the highest-priority remaining step.'}",
            "Keep the recovery packet up to date so the next resume can act without broad rereading.",
            "Only widen context beyond the focused jobs or files if the current step clearly needs it.",
        ],
        "next_action": {
            "summary": f"Continue the active phase by executing the next concrete chunk of work{f': {phase_step}' if phase_step else '.'}",
            "reason": "The runtime should resume from the smallest actionable step rather than re-planning from scratch.",
            "primary_target": str(runtime_dir / "notes" / "latest_summary.md"),
            "resume_targets": [
                str(runtime_dir / "notes" / "latest_summary.md"),
                str(runtime_dir / "plan.json"),
                str(runtime_dir / "notes" / "jobs_focus.md"),
            ],
            "search_patterns": [],
            "read_ladder": [
                "Read latest_summary.md and the current plan first.",
                "Open only the files or jobs directly required by the active phase.",
                "Update the recovery packet before ending the tick.",
            ],
            "success_condition": "The active plan step advances and the next resume point becomes more specific.",
            "fallback_if_missing": "If the active step is ambiguous, tighten the phase plan and next action packet before continuing.",
        },
    }


def merge_execution_packet(runtime_dir: Path, state: dict[str, Any], packet: dict[str, Any], overwrite_missing_only: bool = False) -> None:
    execution = state.setdefault("execution", {})
    derived = derive_execution_defaults(runtime_dir, state)
    source = deepcopy(packet) if packet else {}

    for section_name in ("current_phase", "next_action"):
        target_section = execution.setdefault(section_name, {})
        source_section = source.get(section_name, {}) if isinstance(source.get(section_name, {}), dict) else {}
        derived_section = derived[section_name]
        for key, fallback_value in derived_section.items():
            value = source_section.get(key)
            if overwrite_missing_only:
                if looks_missing(target_section.get(key)):
                    target_section[key] = deepcopy(value if not looks_missing(value) else fallback_value)
            else:
                target_section[key] = deepcopy(value if not looks_missing(value) else fallback_value)

    source_phase_plan = source.get("phase_plan", []) if isinstance(source.get("phase_plan", []), list) else []
    if overwrite_missing_only:
        if looks_missing(execution.get("phase_plan")):
            execution["phase_plan"] = deepcopy(source_phase_plan if source_phase_plan else derived["phase_plan"])
    else:
        execution["phase_plan"] = deepcopy(source_phase_plan if source_phase_plan else derived["phase_plan"])

    execution["updated_at"] = now_iso()


def summarize_plan(state: dict[str, Any], limit: int = 8) -> list[str]:
    lines = [f"- [{item.get('status', 'pending')}] {item.get('step', '')}" for item in current_plan_items(state)[:limit]]
    return lines or ["- none"]


def summarize_preview_plan(state: dict[str, Any], limit: int = 8) -> list[str]:
    lines = [f"- [{item.get('status', 'pending')}] {item.get('step', '')}" for item in preview_plan_items(state)[:limit]]
    return lines or ["- none"]


def summarize_pending_inputs(runtime_dir: Path, limit: int = 8) -> list[str]:
    pending = [item for item in load_inputs(runtime_dir) if item.get("status") == "pending"]
    lines = []
    for item in pending[:limit]:
        title = f" | {item['title']}" if item.get("title") else ""
        lines.append(f"- `{item['id']}` [{item.get('source', '')}] {truncate_text(item.get('content', ''), 180)}{title}")
    return lines or ["- none"]


def summarize_recent_events(runtime_dir: Path, limit: int = MODE_RECENT_EVENT_LIMIT) -> list[str]:
    interesting = []
    for event in load_events(runtime_dir):
        event_type = event.get("type", "")
        if event_type in {
            "tick_completed",
            "tick_failed",
            "input_added",
            "inputs_acknowledged_by_worker",
            "lark_heartbeat_sent",
            "lark_poll_changed",
            "job_submitted",
            "daemon_started",
            "daemon_stop_requested",
            "model_switched",
        }:
            interesting.append(event)
    lines = []
    for event in interesting[-limit:]:
        lines.append(f"- {event.get('ts', '')}: {event.get('type', '')}")
    return lines or ["- none"]


def infer_next_action(state: dict[str, Any], runtime_dir: Path) -> str:
    if state["supervisor"].get("paused"):
        return "Wait for `/autoresearch resume` before doing more work."
    lifecycle = state["lifecycle"].get("status", "")
    if lifecycle == "awaiting_plan_confirmation":
        return "Review the proposed plan, then approve it or revise it before execution starts."
    pending = [item for item in load_inputs(runtime_dir) if item.get("status") == "pending"]
    if pending:
        newest = pending[0]
        return f"Consume pending input `{newest['id']}` and decide whether it changes the plan."
    if lifecycle == "waiting_job":
        return "Poll job state and logs, then resume execution when results are ready."
    if lifecycle == "blocked":
        return "Resolve the current blocker or ask the user for a decision."
    if state.get("jobs"):
        return "Refresh active jobs and continue the highest-priority executable task."
    return "Run the next highest-priority task from the current plan."


def mission_heading_bullets(runtime_dir: Path, keywords: tuple[str, ...]) -> list[str]:
    mission_path = runtime_dir / "mission.md"
    if not mission_path.exists():
        return []
    markdown = read_text(mission_path)
    headings = re.compile(r"^##\s+(.+)$")
    ordered_item = re.compile(r"^\s*(?:[-*]|\d+\.)\s+(.+?)\s*$")
    matches: list[str] = []
    active = False
    lowered_keywords = tuple(keyword.lower() for keyword in keywords)
    for raw_line in markdown.splitlines():
        stripped = raw_line.strip()
        heading_match = headings.match(stripped)
        if heading_match:
            heading = heading_match.group(1).lower()
            active = any(keyword in heading for keyword in lowered_keywords)
            continue
        if not active:
            continue
        item_match = ordered_item.match(raw_line)
        if item_match:
            matches.append(item_match.group(1))
    return matches


def inferred_constraints_from_mission(runtime_dir: Path) -> list[str]:
    mission_path = runtime_dir / "mission.md"
    if not mission_path.exists():
        return []
    markdown = read_text(mission_path)
    paths = extract_path_mentions(markdown)
    constraints: list[str] = []
    editable_root = workspace_root_from_paths(paths, mission_path=mission_path)
    if editable_root:
        constraints.append(f"Editable workspace is explicitly scoped to {editable_root}.")
    if "sbatch" in markdown.lower():
        constraints.append("Cluster experiments may be launched through sbatch when local validation is insufficient.")
    doc_urls = extract_doc_urls(markdown)
    if doc_urls:
        constraints.append(f"User-visible progress should be kept current in {doc_urls[0]}.")
    if "/permissions full-access" in markdown.lower() or "full-access" in markdown.lower():
        constraints.append("Execution may require full-access worker permissions inside the mission-authorized workspace.")
    return constraints


def derive_step_method(runtime_dir: Path, step: str) -> list[str]:
    mission_path = runtime_dir / "mission.md"
    markdown = read_text(mission_path) if mission_path.exists() else ""
    paths = extract_path_mentions(markdown)
    source_file = select_relevant_path(paths, suffixes=(".py", ".cpp", ".cu", ".cc", ".c", ".go", ".rs", ".ts", ".tsx", ".js"))
    target_repo = workspace_root_from_paths(paths, mission_path=mission_path) or str(runtime_dir.parent)
    doc_url = extract_doc_urls(markdown)
    doc_target = doc_url[0] if doc_url else "the designated report sink"
    lowered = step.lower()

    if "clone a clean nvidia megatron-lm baseline" in lowered:
        return [
            f"Clone a fresh NVIDIA Megatron-LM checkout into {target_repo} from the official GitHub repository, or replace any mixed local tree with a clean upstream baseline before porting.",
            "Record the exact upstream commit hash plus dependency manifests so later behavior and performance comparisons stay tied to a fixed baseline.",
            "Create the recommended environment, install the documented dependencies, and run the smallest import or smoke path that proves how the baseline is launched before writing adaptive code.",
        ]
    if "freeze the working baseline" in lowered or "record the exact upstream commit" in lowered:
        return [
            f"Recreate or verify a clean baseline checkout inside {target_repo}.",
            "Record the exact upstream commit hash and note any intentional divergence before writing code.",
            "Confirm the writable surface for the port so unrelated files are not mixed into the implementation branch.",
        ]
    if "reverse-engineer" in lowered or "behaviors to preserve" in lowered:
        source_target = source_file or "the mission's reference implementation"
        return [
            f"Re-open {source_target} around the adaptive forward, router, ponder, and generation paths.",
            "Write down the invariants that must survive the port: tensor flow, loss terms, branch conditions, and cache behavior.",
            "Convert those findings into a symbol-to-symbol mapping note that points from the reference implementation to the target integration seam.",
        ]
    if "integration seam" in lowered or "wrapper" in lowered:
        return [
            "Inspect the smallest set of target entry points that can host the feature without deep rewrites.",
            "Compare wrapper-based integration against direct baseline patching and choose the lower-regret seam.",
            "Document the chosen seam and the files that will become the initial write surface before changing code.",
        ]
    if "equivalence checks" in lowered or "correctness-first" in lowered:
        return [
            "Implement the smallest runnable slice first: config knobs, wrapper skeleton, and the first adaptive forward path.",
            "Add a local harness or toy input path that checks forward outputs, loss terms, or routing behavior against the reference.",
            "Only widen the implementation after the first equivalence check produces an interpretable result.",
        ]
    if "sbatch" in lowered or "optimize" in lowered or "throughput" in lowered:
        return [
            "Keep the optimization pass gated on correctness so speed work does not hide functional regressions.",
            "Prepare sbatch jobs that isolate throughput, parity, and failure-signal checks with explicit logs and metadata.",
            "Use the first cluster results to decide whether to continue optimizing, revert, or narrow the hot path.",
        ]
    if "feishu" in lowered or "progress" in lowered or "evidence" in lowered:
        return [
            f"Append concrete evidence, open risks, and immediate next actions to {doc_target}.",
            "Keep every update tied to a code change, measurement, or decision so the report can serve as an audit trail.",
            "Use the recorded evidence to decide whether the next loop should deepen implementation, run experiments, or escalate a blocker.",
        ]
    return [
        "Read the smallest mission and state artifacts needed to start the step without broad rereading.",
        "Execute one concrete, testable chunk that advances the active step instead of re-planning from scratch.",
        "Record the result and the exact next move so the following worker or user update can resume cleanly.",
    ]


def render_plan_preview(runtime_dir: Path) -> str:
    state = load_state(runtime_dir)
    constraints = mission_heading_bullets(runtime_dir, ("constraint", "限制", "requirement", "要求"))
    if not constraints:
        constraints = inferred_constraints_from_mission(runtime_dir)
    assumptions = mission_heading_bullets(runtime_dir, ("assumption", "假设"))
    risks = mission_heading_bullets(runtime_dir, ("risk", "question", "open question", "风险", "问题"))
    preview = state.get("planning", {}).get("preview", {})
    preview_items = preview_plan_items(state)
    first_step = preview_items[0]["step"] if preview_items else ""
    first_step_method = derive_step_method(runtime_dir, first_step) if first_step else []
    if preview.get("revision_note"):
        assumptions = assumptions + [f"Latest revision request: {preview['revision_note']}"]
    if not assumptions:
        assumptions = [
            "No execution, daemon start, or cluster job submission should happen before plan approval.",
            f"The runtime root defaults to {runtime_dir} unless the user explicitly chose another location.",
        ]
    if not constraints:
        constraints = ["- none explicitly stated in the mission"]
    else:
        constraints = [f"- {item}" for item in constraints]
    assumptions = [f"- {item}" for item in assumptions]
    if not risks:
        risks = [
            "- The initial plan may need reprioritization after the first evidence pass.",
            "- Any missing user preference should be clarified before expensive jobs are launched.",
        ]
    else:
        risks = [f"- {item}" for item in risks]

    sections = [
        "**Auto-Codex Plan Preview**",
        "",
        "**Goal**",
        state["mission"].get("title", Path(runtime_dir).name),
        "",
        "**Constraints**",
        "\n".join(constraints),
        "",
        "**Assumptions**",
        "\n".join(assumptions),
        "",
        "**Proposed Plan**",
        "\n".join(summarize_preview_plan(state)),
        "",
        "**How Step 1 Will Be Done**",
        "\n".join(f"- {item}" for item in first_step_method) if first_step_method else "- none",
        "",
        "**Risks / Open Questions**",
        "\n".join(risks),
        "",
        "**Start Gate**",
        "- Approve this plan to start execution.",
        "- Revise this plan if priorities, constraints, or sequencing need to change first.",
    ]
    return "\n".join(sections).strip() + "\n"


def render_mode_report(runtime_dir: Path, flavor: str = "status") -> str:
    state = load_state(runtime_dir)
    daemon = daemon_snapshot(runtime_dir)
    live = collect_live_worker_snapshot(runtime_dir, state)
    latest_progress = state["progress"].get("summary", "").strip() or "No progress recorded yet."
    if live.get("running"):
        latest_progress = (
            "A worker burst is currently in flight. "
            f"Last completed summary: {latest_progress}"
        )
    goal = state["mission"].get("title", Path(runtime_dir).name)
    waiting_reason = state["lifecycle"].get("stop_reason", "").strip()
    lifecycle = "paused" if state["supervisor"].get("paused") else state["lifecycle"].get("status", "")
    plan_heading = "**Current Plan**"
    plan_lines = summarize_plan(state)
    if lifecycle == "awaiting_plan_confirmation":
        plan_heading = "**Proposed Plan**"
        plan_lines = summarize_preview_plan(state)

    if lifecycle in {"waiting_job", "blocked"} and not waiting_reason:
        waiting_reason = state["progress"].get("summary", "").strip()
    if not waiting_reason:
        waiting_reason = "none"
    runtime_health: list[str] = []
    if lifecycle in {"running", "waiting_job"} and not daemon.get("running") and not live.get("running"):
        runtime_health.append("supervisor is not running; the visible state may be stale until the daemon is restarted.")
    if lifecycle == "waiting_job" and not has_active_jobs(state):
        runtime_health.append("no tracked jobs are currently queued or running; the previous waiting_job state is stale and should be resumed immediately.")
    execution = state.get("execution", {})
    current_phase = execution.get("current_phase", {})
    next_action = execution.get("next_action", {})
    phase_plan = execution.get("phase_plan", [])
    next_step_method = next_action.get("read_ladder", [])
    if not next_step_method:
        next_step_method = derive_step_method(runtime_dir, current_phase.get("related_plan_step", "") or current_phase.get("goal", ""))

    header = "**Auto-Codex Sync**" if flavor == "sync" else "**Auto-Codex Status**"
    wait_banner: list[str] = []
    live_sections: list[str] = []
    if daemon.get("running") and has_delayed_wake(state):
        banner_line = "Auto-Codex is waiting on a real delayed wake and will resume automatically."
        if lifecycle != "waiting_job":
            banner_line = "Auto-Codex has scheduled the next delayed wake and will resume automatically."
        wait_banner = [
            "**[begin sleep]**",
            f"- {banner_line}",
            f"- will come back in: {format_sleep_duration(state.get('progress', {}).get('next_sleep_seconds', 0))}",
            f"- planned wake at: {state.get('progress', {}).get('planned_wake_at', '') or 'none'}",
            f"- reason: {state.get('progress', {}).get('sleep_reason', '') or 'none'}",
            "",
        ]
    if live.get("running"):
        live_sections = [
            "**Live Worker Burst**",
            f"- in flight: yes",
            f"- started at: {live.get('started_at', '') or 'none'}",
            f"- elapsed: {live.get('elapsed', '') or '0s'}",
            f"- model: {live.get('model', '') or 'none'}",
            f"- worker pid: {live.get('worker_pid', 'none')}",
            f"- session log: {live.get('session_log_path', '') or 'none'}",
            "",
            "**What It Just Did**",
            "\n".join(f"- {item}" for item in live.get("recent_actions", [])) if live.get("recent_actions") else "- none recorded yet",
            "",
            "**What It Is Doing**",
            f"- {live.get('current_action', '') or 'Waiting for the worker burst to finish.'}",
            "",
            "**What It Plans Next**",
            f"- {live.get('next_hint', '') or 'Will return a structured handoff when the burst ends.'}",
            "",
        ]
    sections = [
        header,
        "",
        *wait_banner,
        f"**Goal**",
        goal,
        "",
        plan_heading,
        "\n".join(plan_lines),
        "",
        f"**Latest Progress**",
        latest_progress,
        "",
        *(["**Runtime Health**", *[f"- {item}" for item in runtime_health], ""] if runtime_health else []),
        *live_sections,
        f"**Current Phase**",
        f"- title: {current_phase.get('title', '') or 'none'}",
        f"- goal: {current_phase.get('goal', '') or 'none'}",
        f"- related step: {current_phase.get('related_plan_step', '') or 'none'}",
        f"- related jobs: {', '.join(current_phase.get('related_job_ids', [])) or 'none'}",
        "",
        f"**Phase Plan**",
        "\n".join(f"- {item}" for item in phase_plan) if phase_plan else "- none",
        "",
        f"**Waiting / Blockers**",
        f"- lifecycle: {lifecycle}",
        f"- reason: {waiting_reason}",
        f"- pending inputs: {state.get('inputs', {}).get('pending', 0)}",
        f"- next sleep seconds: {state.get('progress', {}).get('next_sleep_seconds', DEFAULT_INTERVAL_SECONDS)}",
        f"- sleep reason: {state.get('progress', {}).get('sleep_reason', '') or 'none'}",
        f"- planned wake at: {state.get('progress', {}).get('planned_wake_at', '') or 'none'}",
        "",
        f"**Active Jobs**",
        "\n".join(summarize_jobs(state)),
        "",
        f"**Pending Inputs**",
        "\n".join(summarize_pending_inputs(runtime_dir)),
        "",
        f"**Recent Runtime Events**",
        "\n".join(summarize_recent_events(runtime_dir)),
        "",
        f"**Next Action**",
        f"- summary: {next_action.get('summary', '') or infer_next_action(state, runtime_dir)}",
        f"- reason: {next_action.get('reason', '') or 'none'}",
        f"- primary target: {next_action.get('primary_target', '') or 'none'}",
        f"- resume targets: {', '.join(next_action.get('resume_targets', [])) or 'none'}",
        f"- search patterns: {', '.join(next_action.get('search_patterns', [])) or 'none'}",
        f"- success condition: {next_action.get('success_condition', '') or 'none'}",
        f"- fallback: {next_action.get('fallback_if_missing', '') or 'none'}",
        "",
        f"**How This Step Will Be Done**",
        "\n".join(f"- {item}" for item in next_step_method) if next_step_method else "- none",
    ]
    return "\n".join(sections).strip() + "\n"


def make_input_item(source: str, content: str, title: str = "", author: str = "user") -> dict[str, Any]:
    created_at = now_iso()
    return {
        "id": f"inp_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}",
        "source": source,
        "author": author,
        "title": title.strip(),
        "content": content.strip(),
        "status": "pending",
        "created_at": created_at,
        "acknowledged_at": "",
        "resolution": "",
    }


def record_summary(runtime_dir: Path, state: dict[str, Any], result: dict[str, Any]) -> None:
    current_phase = state.get("execution", {}).get("current_phase", {})
    next_action = state.get("execution", {}).get("next_action", {})
    summary = textwrap.dedent(
        f"""\
        # Latest Summary

        Updated: {now_iso()}
        Worker status: {result.get("status", "unknown")}
        Current phase: {current_phase.get("title", "") or "none"}
        Next action: {next_action.get("summary", "") or "none"}
        Planned sleep: {state.get("progress", {}).get("next_sleep_seconds", DEFAULT_INTERVAL_SECONDS)}s
        Sleep reason: {state.get("progress", {}).get("sleep_reason", "") or "none"}

        {result.get("summary", "").strip()}
        """
    )
    write_text(runtime_dir / "notes" / "latest_summary.md", summary)


def capture_command(
    cmd: list[str],
    cwd: Path | None = None,
    stdin_text: str | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: int | None = None,
) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    if env:
        command_env.update(env)
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            input=stdin_text,
            text=True,
            capture_output=True,
            env=command_env,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        timeout_note = f"Command timed out after {timeout_seconds} seconds."
        stderr = f"{stderr}\n{timeout_note}".strip()
        return subprocess.CompletedProcess(cmd, 124, stdout=stdout, stderr=stderr)


def codex_exec_supports_search() -> bool:
    global CODEX_EXEC_SEARCH_SUPPORT
    if CODEX_EXEC_SEARCH_SUPPORT is not None:
        return CODEX_EXEC_SEARCH_SUPPORT
    result = capture_command(["codex", "exec", "--help"])
    help_text = "\n".join(part for part in [result.stdout, result.stderr] if part)
    CODEX_EXEC_SEARCH_SUPPORT = "--search" in help_text
    return CODEX_EXEC_SEARCH_SUPPORT


def lark_env() -> dict[str, str]:
    return {"LARK_CLI_NO_PROXY": "1"}


def pid_file_path(runtime_dir: Path) -> Path:
    return runtime_dir / "supervisor.pid"


def daemon_log_path(runtime_dir: Path) -> Path:
    return runtime_dir / "logs" / "supervisor.log"


def read_pid(pid_path: Path) -> int | None:
    if not pid_path.exists():
        return None
    try:
        return int(read_text(pid_path).strip())
    except ValueError:
        return None


def pid_is_running(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def daemon_snapshot(runtime_dir: Path) -> dict[str, Any]:
    pid_path = pid_file_path(runtime_dir)
    pid = read_pid(pid_path)
    running = pid_is_running(pid)
    if pid is not None and not running and pid_path.exists():
        pid_path.unlink()
    return {
        "pid_path": str(pid_path),
        "pid": pid if running else None,
        "running": running,
        "log_path": str(daemon_log_path(runtime_dir)),
    }


def active_tick_state(state: dict[str, Any]) -> dict[str, Any]:
    return state.get("supervisor", {}).get("active_tick", {})


def read_proc_children(pid: int) -> list[int]:
    children_path = Path(f"/proc/{pid}/task/{pid}/children")
    if pid <= 0 or not children_path.exists():
        return []
    try:
        raw = read_text(children_path).strip()
    except OSError:
        return []
    children: list[int] = []
    for item in raw.split():
        try:
            children.append(int(item))
        except ValueError:
            continue
    return children


def descendant_pids(pid: int, max_depth: int = 3) -> list[int]:
    if pid <= 0 or max_depth <= 0:
        return []
    pending = [(pid, 0)]
    seen: set[int] = set()
    descendants: list[int] = []
    while pending:
        current, depth = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        if depth > 0:
            descendants.append(current)
        if depth >= max_depth:
            continue
        for child in read_proc_children(current):
            pending.append((child, depth + 1))
    return descendants


def session_log_path_from_pid(pid: int) -> str:
    fd_dir = Path(f"/proc/{pid}/fd")
    if pid <= 0 or not fd_dir.exists():
        return ""
    try:
        entries = list(fd_dir.iterdir())
    except OSError:
        return ""
    for entry in entries:
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if "/.codex/sessions/" in target and target.endswith(".jsonl"):
            return target
    return ""


def session_log_path_from_pid_tree(pid: int) -> str:
    for candidate in [pid, *descendant_pids(pid)]:
        target = session_log_path_from_pid(candidate)
        if target:
            return target
    return ""


def maybe_parse_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def parse_session_entries(session_log_path: str, max_lines: int = 240) -> list[dict[str, Any]]:
    if not session_log_path:
        return []
    entries: list[dict[str, Any]] = []
    for raw_line in tail_lines(Path(session_log_path), max_lines):
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            entries.append(payload)
    return entries


def shorten_path(raw: str, max_parts: int = 3) -> str:
    if not raw:
        return "unknown"
    path = Path(raw)
    parts = path.parts[-max_parts:]
    return str(Path(*parts)) if parts else raw


def summarize_exec_command_intent(cmd: str) -> str:
    lowered = cmd.lower()
    path_match = re.search(r"(/[^\"'\s]+)", cmd)
    hinted_path = shorten_path(path_match.group(1)) if path_match else ""
    if "sbatch --parsable" in lowered:
        return "Submitting a Slurm smoke job."
    if "squeue " in lowered or "sacct " in lowered:
        return "Checking cluster job status."
    if "sinfo" in lowered:
        return "Inspecting cluster partitions."
    if "python3 -m py_compile" in lowered or "bash -n " in lowered:
        return "Validating newly written smoke scripts."
    if "sed -n " in lowered and hinted_path:
        return f"Reading {hinted_path}."
    if lowered.startswith("cat ") and hinted_path:
        return f"Reading {hinted_path}."
    if "rg -n " in lowered:
        return "Searching the codebase for the next integration point."
    if "git " in lowered and ("status" in lowered or "rev-parse" in lowered or "log " in lowered):
        return "Inspecting the repository baseline and commit state."
    if "python - <<'py'" in lowered or "python - <<" in lowered or "torchrun " in lowered:
        return "Running a local Python or torch smoke check."
    return f"Running `{truncate_text(cmd, 120)}`."


def summarize_exec_command_result(cmd: str, output: str) -> str:
    lowered = cmd.lower()
    if "sbatch --parsable" in lowered:
        match = re.search(r"\b(\d{4,})\b", output)
        if match:
            return f"Submitted Slurm job `{match.group(1)}`."
        return "Submitted a Slurm job."
    if "python3 -m py_compile" in lowered or "bash -n " in lowered:
        return "Validated the new smoke runner and sbatch wrapper."
    if "sinfo" in lowered:
        return "Confirmed Slurm partitions and cluster availability."
    if "squeue " in lowered or "sacct " in lowered:
        return "Checked the submitted job status."
    if "sed -n " in lowered:
        path_match = re.search(r"(/[^\"'\s]+)", cmd)
        return f"Read {shorten_path(path_match.group(1))}." if path_match else "Read the requested source file."
    if lowered.startswith("cat "):
        path_match = re.search(r"(/[^\"'\s]+)", cmd)
        return f"Read {shorten_path(path_match.group(1))}." if path_match else "Read the requested file."
    if "rg -n " in lowered:
        return "Searched the tree for the requested symbols and paths."
    if "git " in lowered and ("status" in lowered or "rev-parse" in lowered or "log " in lowered):
        return "Recorded the baseline repository state."
    if "python - <<'py'" in lowered or "python - <<" in lowered or "torchrun " in lowered:
        return "Ran a local Python smoke check."
    return summarize_exec_command_intent(cmd)


def summarize_patch_intent(raw_patch: str, *, completed: bool) -> str:
    files = re.findall(r"\*\*\* (?:Add|Update|Delete) File: (.+)", raw_patch)
    if not files:
        return "Editing runtime artifacts." if completed else "Preparing runtime file edits."
    labels = ", ".join(shorten_path(path, max_parts=2) for path in files[:3])
    if len(files) > 3:
        labels = f"{labels}, +{len(files) - 3} more"
    verb = "Updated" if completed else "Updating"
    return f"{verb} {labels}."


def summarize_tool_invocation(tool_name: str, raw_arguments: Any, *, completed: bool = False, output: str = "") -> str:
    parsed = maybe_parse_json(raw_arguments)
    if tool_name == "exec_command":
        cmd = parsed.get("cmd", "") if isinstance(parsed, dict) else str(parsed)
        return summarize_exec_command_result(cmd, output) if completed else summarize_exec_command_intent(cmd)
    if tool_name == "apply_patch":
        raw_patch = raw_arguments if isinstance(raw_arguments, str) else str(raw_arguments)
        return summarize_patch_intent(raw_patch, completed=completed)
    if tool_name == "update_plan":
        payload = parsed if isinstance(parsed, dict) else {}
        active_step = next((item.get("step", "") for item in payload.get("plan", []) if item.get("status") == "in_progress"), "")
        explanation = str(payload.get("explanation", "")).strip()
        if completed:
            return truncate_text(active_step or explanation or "Updated the execution plan.", 180)
        return truncate_text(active_step or explanation or "Updating the execution plan.", 180)
    label = tool_name.replace("_", " ")
    return f"Completed {label}." if completed else f"Running {label}."


def collect_live_worker_snapshot(runtime_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    active_tick = active_tick_state(state)
    if not active_tick.get("running"):
        return {}

    worker_pid = int(active_tick.get("worker_pid") or 0)
    session_log_path = str(active_tick.get("session_log_path", "")).strip()
    if worker_pid > 0 and not session_log_path:
        session_log_path = session_log_path_from_pid_tree(worker_pid)

    entries = parse_session_entries(session_log_path)
    pending_calls: dict[str, tuple[str, Any]] = {}
    recent_actions: list[str] = []
    current_action = "The worker burst is running."
    next_hint = state.get("execution", {}).get("next_action", {}).get("summary", "") or "The worker will return a more specific handoff when the burst ends."
    last_entry_type = ""

    for entry in entries:
        entry_type = str(entry.get("type", ""))
        payload = entry.get("payload", {}) if isinstance(entry.get("payload", {}), dict) else {}
        if entry_type != "response_item":
            continue
        payload_type = str(payload.get("type", ""))
        last_entry_type = payload_type or last_entry_type
        if payload_type in {"function_call", "custom_tool_call"}:
            tool_name = str(payload.get("name", ""))
            raw_arguments = payload.get("arguments", payload.get("input", ""))
            pending_calls[str(payload.get("call_id", ""))] = (tool_name, raw_arguments)
            current_action = summarize_tool_invocation(tool_name, raw_arguments, completed=False)
        elif payload_type in {"function_call_output", "custom_tool_call_output"}:
            call_id = str(payload.get("call_id", ""))
            tool_name, raw_arguments = pending_calls.pop(call_id, ("tool", ""))
            summary = summarize_tool_invocation(tool_name, raw_arguments, completed=True, output=str(payload.get("output", "")))
            if summary and (not recent_actions or recent_actions[-1] != summary):
                recent_actions.append(summary)
            current_action = "Preparing the next concrete action from the latest tool results."
        elif payload_type == "reasoning":
            current_action = "Reasoning over the latest results and preparing the structured handoff."

        if payload_type == "function_call" and str(payload.get("name", "")) == "update_plan":
            parsed_arguments = maybe_parse_json(payload.get("arguments", ""))
            if isinstance(parsed_arguments, dict):
                steps = parsed_arguments.get("plan", [])
                candidate = next((item.get("step", "") for item in steps if item.get("status") in {"in_progress", "pending"}), "")
                explanation = str(parsed_arguments.get("explanation", "")).strip()
                next_hint = candidate or explanation or next_hint

    if pending_calls:
        _, (tool_name, raw_arguments) = next(reversed(pending_calls.items()))
        current_action = summarize_tool_invocation(tool_name, raw_arguments, completed=False)
    elif last_entry_type not in {"function_call", "custom_tool_call"}:
        current_action = "Summarizing completed work and preparing the structured JSON handoff."

    started_at = str(active_tick.get("started_at", "")).strip()
    snapshot = {
        "running": True,
        "started_at": started_at,
        "elapsed": format_sleep_duration(seconds_since(started_at) if started_at else 0),
        "model": str(active_tick.get("model", "")).strip() or state.get("supervisor", {}).get("active_model", ""),
        "worker_pid": worker_pid or "none",
        "session_log_path": session_log_path or "none",
        "current_action": current_action,
        "recent_actions": recent_actions[-5:],
        "next_hint": truncate_text(next_hint, 220),
        "last_activity_at": datetime.fromtimestamp(Path(session_log_path).stat().st_mtime).astimezone().isoformat(timespec="seconds")
        if session_log_path and Path(session_log_path).exists()
        else "",
    }
    return snapshot


def parse_iso_timestamp(value: str) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


def seconds_since(value: str) -> float:
    ts = parse_iso_timestamp(value)
    if not ts:
        return float("inf")
    return time.time() - ts


def maybe_capture_squeue(runtime_dir: Path) -> str:
    user = os.environ.get("USER", "")
    if not user:
        return ""
    result = capture_command(["squeue", "-u", user])
    snapshot_path = runtime_dir / "snapshots" / f"squeue-{int(time.time())}.txt"
    write_text(snapshot_path, result.stdout + ("\nSTDERR:\n" + result.stderr if result.stderr else ""))
    return result.stdout.strip()


def maybe_capture_sacct(runtime_dir: Path, job_ids: list[str]) -> str:
    cleaned = [job_id.strip() for job_id in job_ids if str(job_id).strip()]
    if not cleaned:
        return ""
    result = capture_command(
        [
            "sacct",
            "-j",
            ",".join(cleaned),
            "--format=JobIDRaw,State,ExitCode,Elapsed,NodeList,Partition",
            "-P",
            "-n",
        ],
        cwd=runtime_dir,
    )
    snapshot_path = runtime_dir / "snapshots" / f"sacct-{int(time.time())}.txt"
    write_text(snapshot_path, result.stdout + ("\nSTDERR:\n" + result.stderr if result.stderr else ""))
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def parse_squeue_output(output: str) -> dict[str, dict[str, str]]:
    rows = output.splitlines()
    parsed: dict[str, dict[str, str]] = {}
    if len(rows) < 2:
        return parsed
    for row in rows[1:]:
        parts = row.split(None, 7)
        if len(parts) < 5:
            continue
        job_id = parts[0]
        parsed[job_id] = {
            "job_id": job_id,
            "partition": parts[1] if len(parts) > 1 else "",
            "name": parts[2] if len(parts) > 2 else "",
            "user": parts[3] if len(parts) > 3 else "",
            "state": parts[4] if len(parts) > 4 else "",
            "time": parts[5] if len(parts) > 5 else "",
            "nodes": parts[6] if len(parts) > 6 else "",
            "nodelist": parts[7] if len(parts) > 7 else "",
        }
    return parsed


def parse_sacct_output(output: str) -> dict[str, dict[str, str]]:
    parsed: dict[str, dict[str, str]] = {}
    for row in output.splitlines():
        parts = row.split("|")
        if len(parts) < 6:
            continue
        job_id = parts[0].strip()
        if not job_id or "." in job_id:
            continue
        raw_state = parts[1].strip()
        parsed[job_id] = {
            "job_id": job_id,
            "state": normalize_job_status(raw_state),
            "raw_state": raw_state,
            "exit_code": parts[2].strip(),
            "elapsed": parts[3].strip(),
            "nodelist": parts[4].strip(),
            "partition": parts[5].strip(),
        }
    return parsed


def parse_json_response(raw: str) -> dict[str, Any]:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def normalize_result(raw: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(raw)
    result.setdefault("status", "working")
    result.setdefault("summary", "")
    result.setdefault("next_sleep_seconds", DEFAULT_INTERVAL_SECONDS)
    result.setdefault("sleep_reason", "")
    result.setdefault("jobs_submitted", [])
    result.setdefault("artifacts_updated", [])
    result.setdefault("lark_update_markdown", "")
    result.setdefault("model_switch_recommended", "")
    result.setdefault("stop_reason", "")
    result.setdefault("plan_updates", [])
    result.setdefault("acknowledged_input_ids", [])
    result.setdefault("final_summary_markdown", "")
    result.setdefault("current_phase", {})
    result.setdefault("phase_plan", [])
    result.setdefault("next_action", {})
    return result


def write_supervisor_log(runtime_dir: Path, name: str, content: str) -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    write_text(runtime_dir / "logs" / "codex" / f"{stamp}-{name}.log", content)


def detect_model_limit(output: str) -> bool:
    lowered = output.lower()
    needles = ("rate limit", "limit reached", "quota", "too many requests")
    return any(needle in lowered for needle in needles)


def locate_current_session_file() -> Path | None:
    thread_id = os.environ.get("CODEX_THREAD_ID", "").strip()
    if not thread_id:
        return None
    sessions_root = Path.home() / ".codex" / "sessions"
    if not sessions_root.exists():
        return None
    matches = sorted(sessions_root.rglob(f"*{thread_id}*.jsonl"))
    return matches[-1] if matches else None


def session_exec_policy() -> dict[str, str]:
    session_path = locate_current_session_file()
    if session_path is None or not session_path.exists():
        return {}

    sandbox_mode = ""
    approval_policy = ""
    try:
        with session_path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") != "turn_context":
                    continue
                payload = record.get("payload", {})
                sandbox_policy = payload.get("sandbox_policy", {})
                sandbox_value = str(sandbox_policy.get("type", "")).strip()
                approval_value = str(payload.get("approval_policy", "")).strip()
                if sandbox_value in VALID_SANDBOX_MODES:
                    sandbox_mode = sandbox_value
                if approval_value in VALID_APPROVAL_POLICIES:
                    approval_policy = approval_value
    except OSError:
        return {}

    result: dict[str, str] = {}
    if sandbox_mode:
        result["sandbox_mode"] = sandbox_mode
    if approval_policy:
        result["approval_policy"] = approval_policy
    if result:
        result["source"] = str(session_path)
    return result


def effective_worker_policy(args: argparse.Namespace) -> dict[str, str | bool]:
    session_policy = session_exec_policy()
    sandbox_mode = session_policy.get("sandbox_mode", DEFAULT_WORKER_SANDBOX)
    approval_policy = session_policy.get("approval_policy", DEFAULT_WORKER_APPROVAL_POLICY)
    source = "session" if session_policy else "default"

    requested_sandbox = getattr(args, "worker_sandbox", "inherit")
    if requested_sandbox != "inherit":
        sandbox_mode = requested_sandbox
        source = "cli_override"

    requested_approval = getattr(args, "worker_approval_policy", "inherit")
    if requested_approval != "inherit":
        approval_policy = requested_approval
        source = "cli_override"

    if getattr(args, "worker_full_access", False):
        sandbox_mode = "danger-full-access"
        approval_policy = "never"
        source = "cli_full_access"

    dangerous_bypass = sandbox_mode == "danger-full-access" and approval_policy == "never"
    return {
        "sandbox_mode": sandbox_mode,
        "approval_policy": approval_policy,
        "dangerous_bypass": dangerous_bypass,
        "source": source,
        "session_source": session_policy.get("source", ""),
    }


def codex_command(
    runtime_dir: Path,
    model: str,
    search_enabled: bool,
    response_path: Path,
    extra_config: list[str],
    worker_policy: dict[str, str | bool],
) -> list[str]:
    cmd = [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "-C",
        str(runtime_dir),
        "--add-dir",
        str(runtime_dir),
        "-m",
        model,
        "--output-schema",
        str(SCHEMA_PATH),
        "-o",
        str(response_path),
    ]
    if bool(worker_policy.get("dangerous_bypass")):
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        sandbox_mode = str(worker_policy.get("sandbox_mode", "")).strip()
        approval_policy = str(worker_policy.get("approval_policy", "")).strip()
        if sandbox_mode in VALID_SANDBOX_MODES:
            cmd.extend(["-s", sandbox_mode])
        if approval_policy in VALID_APPROVAL_POLICIES:
            cmd.extend(["-c", f'approval_policy="{approval_policy}"'])
    if search_enabled and codex_exec_supports_search():
        cmd.append("--search")
    for item in extra_config:
        cmd.extend(["-c", item])
    return cmd


def update_lark_doc(runtime_dir: Path, state: dict[str, Any], markdown: str) -> bool:
    if not markdown.strip():
        return False
    doc_urls = state["mission"].get("doc_urls", [])
    if not doc_urls:
        append_event(runtime_dir, "lark_skipped", {"reason": "no_doc_url"})
        return False
    payload_path = runtime_dir / "outbox" / f"lark-update-{int(time.time())}.md"
    write_text(payload_path, markdown.strip() + "\n")
    try:
        payload_arg = f"@./{payload_path.relative_to(runtime_dir)}"
    except ValueError:
        payload_arg = f"@{payload_path.name}"
    cmd = [
        "lark-cli",
        "docs",
        "+update",
        "--doc",
        doc_urls[0],
        "--mode",
        "append",
        "--markdown",
        payload_arg,
    ]
    result = capture_command(cmd, cwd=runtime_dir, env=lark_env())
    write_supervisor_log(runtime_dir, "lark", f"CMD: {' '.join(shlex.quote(part) for part in cmd)}\n\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
    if result.returncode == 0:
        state["history"]["lark_updates_sent"] += 1
        append_event(runtime_dir, "lark_update_sent", {"doc": doc_urls[0], "payload_path": str(payload_path)})
        return True
    append_event(
        runtime_dir,
        "lark_update_failed",
        {
            "doc": doc_urls[0],
            "payload_path": str(payload_path),
            "returncode": result.returncode,
        },
    )
    return False


def merge_jobs(state: dict[str, Any], jobs: list[dict[str, Any]]) -> None:
    for job in jobs:
        job_id = str(job.get("job_id", "")).strip()
        if not job_id:
            continue
        previous = state["jobs"].get(job_id, {})
        previous.update(job)
        previous["last_seen_at"] = now_iso()
        state["jobs"][job_id] = previous


def acknowledge_inputs(runtime_dir: Path, state: dict[str, Any], input_ids: list[str], resolution: str) -> None:
    if not input_ids:
        return
    items = load_inputs(runtime_dir)
    target_ids = set(input_ids)
    changed = False
    for item in items:
        if item.get("id") not in target_ids:
            continue
        item["status"] = "acknowledged"
        item["acknowledged_at"] = now_iso()
        if resolution:
            item["resolution"] = resolution
        changed = True
    if changed:
        write_inputs(runtime_dir, items)
        refresh_input_counters(state, items)
        append_event(runtime_dir, "inputs_acknowledged_by_worker", {"ids": sorted(target_ids)})


def merge_plan(state: dict[str, Any], plan_updates: list[dict[str, str]]) -> None:
    if not plan_updates:
        return
    cleaned: list[dict[str, str]] = []
    for item in plan_updates:
        step = str(item.get("step", "")).strip()
        status = str(item.get("status", "pending")).strip()
        if not step:
            continue
        cleaned.append({"step": step, "status": status})
    if cleaned:
        set_current_plan_items(state, cleaned)


def sync_jobs_with_squeue(state: dict[str, Any], queue_snapshot: str) -> None:
    if not state.get("jobs"):
        return
    parsed = parse_squeue_output(queue_snapshot)
    for job_id, metadata in state["jobs"].items():
        if job_id in parsed:
            metadata["status"] = parsed[job_id].get("state", metadata.get("status", ""))
            metadata["queue_time"] = parsed[job_id].get("time", "")
            metadata["last_seen_at"] = now_iso()
        elif normalize_job_status(str(metadata.get("status", ""))) in JOB_RUNNING_STATUSES:
            metadata["status"] = "not_in_queue"
            metadata["last_seen_at"] = now_iso()


def sync_jobs_with_sacct(state: dict[str, Any], sacct_snapshot: str) -> None:
    if not state.get("jobs"):
        return
    parsed = parse_sacct_output(sacct_snapshot)
    if not parsed:
        return
    for job_id, metadata in state["jobs"].items():
        record = parsed.get(job_id)
        if not record:
            continue
        metadata["status"] = record.get("state", metadata.get("status", ""))
        metadata["status_raw"] = record.get("raw_state", "")
        metadata["exit_code"] = record.get("exit_code", "")
        metadata["elapsed"] = record.get("elapsed", "")
        metadata["nodelist"] = record.get("nodelist", "")
        metadata["partition"] = record.get("partition", metadata.get("partition", ""))
        metadata["last_seen_at"] = now_iso()


def maybe_clear_stale_waiting_job(runtime_dir: Path, state: dict[str, Any]) -> bool:
    if state["lifecycle"].get("status") != "waiting_job":
        return False
    if has_active_jobs(state):
        return False
    state["lifecycle"]["status"] = "running"
    state["progress"]["next_sleep_seconds"] = 0
    state["progress"]["sleep_reason"] = (
        "The previous waiting_job state became stale because no tracked jobs remain queued or running; continue immediately with the next worker burst."
    )
    state["progress"]["planned_wake_at"] = ""
    append_event(runtime_dir, "waiting_job_became_stale", {"reason": "no_active_jobs"})
    return True


def fetch_lark_doc_markdown(doc_token_or_url: str, runtime_dir: Path) -> tuple[str | None, str]:
    cmd = [
        "lark-cli",
        "docs",
        "+fetch",
        "--doc",
        doc_token_or_url,
        "--format",
        "pretty",
    ]
    result = capture_command(cmd, cwd=runtime_dir, env=lark_env())
    write_supervisor_log(runtime_dir, "lark-fetch", f"CMD: {' '.join(shlex.quote(part) for part in cmd)}\n\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
    if result.returncode != 0:
        return None, result.stderr or result.stdout
    return result.stdout, ""


def strip_system_sections(markdown: str) -> str:
    lines = markdown.splitlines()
    output: list[str] = []
    skip_depth: int | None = None
    for line in lines:
        heading_match = re.match(r"^(#+)\s+(.+)$", line)
        if heading_match:
            depth = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            if title.startswith(SYSTEM_SECTION_PREFIX):
                skip_depth = depth
                continue
            if skip_depth is not None and depth <= skip_depth:
                skip_depth = None
        if skip_depth is None:
            output.append(line)
    return "\n".join(output).strip() + "\n"


def diff_user_visible_content(old_content: str, new_content: str) -> str:
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()
    prefix = 0
    while prefix < len(old_lines) and prefix < len(new_lines) and old_lines[prefix] == new_lines[prefix]:
        prefix += 1
    if prefix == len(new_lines):
        return ""
    if prefix == len(old_lines):
        delta = "\n".join(new_lines[prefix:]).strip()
        return truncate_text(delta, MAX_INPUT_EXCERPT_CHARS)
    latest_excerpt = truncate_text(new_content, MAX_INPUT_EXCERPT_CHARS)
    return (
        "The Feishu doc changed in a non-append way. Re-read the latest user-visible content.\n\n"
        + latest_excerpt
    )


def maybe_poll_lark_inputs(runtime_dir: Path, state: dict[str, Any], disable_lark: bool) -> None:
    if disable_lark:
        return
    doc_urls = state["mission"].get("doc_urls", [])
    if not doc_urls:
        return
    if seconds_since(state["lark"].get("last_poll_at", "")) < int(state["lark"]["poll_interval_seconds"]):
        return

    raw_markdown, error = fetch_lark_doc_markdown(doc_urls[0], runtime_dir)
    state["lark"]["last_poll_at"] = now_iso()
    if raw_markdown is None:
        append_event(runtime_dir, "lark_poll_failed", {"error": error[-2000:]})
        return

    stripped = strip_system_sections(raw_markdown)
    current_hash = sha256_text(stripped)
    snapshot_path = runtime_dir / "snapshots" / "feishu-user-visible.md"
    previous = read_text(snapshot_path) if snapshot_path.exists() else ""
    previous_hash = state["lark"].get("last_user_visible_hash", "")
    write_text(snapshot_path, stripped)
    state["lark"]["last_snapshot_path"] = str(snapshot_path)

    if not previous_hash:
        state["lark"]["last_user_visible_hash"] = current_hash
        append_event(runtime_dir, "lark_poll_initialized", {"hash": current_hash})
        return

    if previous_hash == current_hash:
        append_event(runtime_dir, "lark_poll_no_change", {"hash": current_hash})
        return

    delta = diff_user_visible_content(previous, stripped)
    state["lark"]["last_user_visible_hash"] = current_hash
    append_event(runtime_dir, "lark_poll_changed", {"hash": current_hash})
    if not delta.strip():
        return

    items = load_inputs(runtime_dir)
    item = make_input_item(
        source="feishu",
        content=delta,
        title="Feishu doc update",
        author="user_doc",
    )
    items.append(item)
    write_inputs(runtime_dir, items)
    refresh_input_counters(state, items)
    append_event(runtime_dir, "input_added", {"id": item["id"], "source": item["source"], "title": item["title"]})


def generate_heartbeat_markdown(state: dict[str, Any]) -> str:
    plan_lines = []
    heartbeat_items = preview_plan_items(state) if state["lifecycle"].get("status") == "awaiting_plan_confirmation" else current_plan_items(state)
    for item in heartbeat_items[:5]:
        plan_lines.append(f"- [{item.get('status', 'pending')}] {item.get('step', '')}")
    if not plan_lines:
        plan_lines.append("- none")

    job_lines = []
    for job_id, metadata in list(state.get("jobs", {}).items())[:10]:
        job_lines.append(f"- `{job_id}`: {metadata.get('status', 'unknown')}")
    if not job_lines:
        job_lines.append("- none")
    next_action = state.get("execution", {}).get("next_action", {})

    return "\n".join(
        [
            f"## {SYSTEM_SECTION_PREFIX} Heartbeat",
            "",
            f"- Time: {now_iso()}",
            f"- Lifecycle status: {state['lifecycle'].get('status', '')}",
            f"- Worker status: {state['progress'].get('last_worker_status', '')}",
            f"- Pending inputs: {state.get('inputs', {}).get('pending', 0)}",
            f"- Active model: {state['supervisor'].get('active_model', '')}",
            f"- Next sleep seconds: {state['progress'].get('next_sleep_seconds', DEFAULT_INTERVAL_SECONDS)}",
            f"- Sleep reason: {state['progress'].get('sleep_reason', '') or 'none'}",
            f"- Planned wake at: {state['progress'].get('planned_wake_at', '') or 'none'}",
            "",
            "### Latest Summary",
            "",
            state["progress"].get("summary", "").strip() or "No summary yet.",
            "",
            "### Current Plan",
            "",
            "\n".join(plan_lines),
            "",
            "### Known Jobs",
            "",
            "\n".join(job_lines),
            "",
            "### Next Action",
            "",
            next_action.get("summary", "").strip() or "No next action recorded.",
            "",
        ]
    )


def generate_final_summary_markdown(state: dict[str, Any], worker_result: dict[str, Any]) -> str:
    body = worker_result.get("final_summary_markdown", "").strip()
    if not body:
        body = textwrap.dedent(
            f"""\
            - Completion time: {now_iso()}
            - Final status: {worker_result.get('status', '')}

            ### Final Summary

            {worker_result.get('summary', '').strip() or state['progress'].get('summary', '').strip() or 'Task marked complete.'}
            """
        )
    return f"## {SYSTEM_SECTION_PREFIX} Final Summary\n\n{body}\n"


def maybe_send_periodic_heartbeat(runtime_dir: Path, state: dict[str, Any], disable_lark: bool) -> None:
    if disable_lark:
        return
    if not state["mission"].get("doc_urls"):
        return
    if seconds_since(state["lark"].get("last_heartbeat_at", "")) < int(state["lark"]["heartbeat_interval_seconds"]):
        return
    markdown = generate_heartbeat_markdown(state)
    if update_lark_doc(runtime_dir, state, markdown):
        state["lark"]["last_heartbeat_at"] = now_iso()
        append_event(runtime_dir, "lark_heartbeat_sent", {})


def invoke_codex_once(
    runtime_dir: Path,
    state: dict[str, Any],
    model: str,
    search_enabled: bool,
    extra_config: list[str],
    worker_policy: dict[str, str | bool],
) -> tuple[dict[str, Any] | None, str]:
    response_path = runtime_dir / "outbox" / f"codex-response-{int(time.time())}.json"
    prompt = render_worker_prompt(runtime_dir, state)
    cmd = codex_command(runtime_dir, model, search_enabled, response_path, extra_config, worker_policy)
    command_env = os.environ.copy()
    process = subprocess.Popen(  # noqa: S603
        cmd,
        cwd=str(runtime_dir),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=command_env,
    )
    active_tick = state["supervisor"]["active_tick"]
    active_tick["worker_pid"] = process.pid
    active_tick["model"] = model
    active_tick["response_path"] = str(response_path)
    session_log_path = ""
    deadline = time.time() + 2.0
    while time.time() < deadline and not session_log_path:
        session_log_path = session_log_path_from_pid_tree(process.pid)
        if session_log_path:
            break
        time.sleep(0.1)
    if session_log_path:
        active_tick["session_log_path"] = session_log_path
    save_state(runtime_dir, state)
    try:
        stdout, stderr = process.communicate(input=prompt, timeout=DEFAULT_WORKER_TICK_TIMEOUT_SECONDS)
        result = subprocess.CompletedProcess(cmd, process.returncode, stdout=stdout, stderr=stderr)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        timeout_note = f"Command timed out after {DEFAULT_WORKER_TICK_TIMEOUT_SECONDS} seconds."
        stderr = f"{stderr}\n{timeout_note}".strip()
        result = subprocess.CompletedProcess(cmd, 124, stdout=stdout, stderr=stderr)
    log_text = f"CMD: {' '.join(shlex.quote(part) for part in cmd)}\n\nPROMPT:\n{prompt}\n\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    write_supervisor_log(runtime_dir, f"codex-{slugify(model)}", log_text)
    combined_output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0:
        return None, combined_output
    try:
        raw = read_text(response_path)
        parsed = normalize_result(parse_json_response(raw))
    except Exception as exc:  # noqa: BLE001
        return None, f"{combined_output}\nFailed to parse response: {exc}"
    return parsed, combined_output


def perform_tick(runtime_dir: Path, args: argparse.Namespace) -> int:
    state = load_state(runtime_dir)
    if state["lifecycle"].get("status") == "awaiting_plan_confirmation":
        append_event(runtime_dir, "tick_skipped", {"reason": "awaiting_plan_confirmation"})
        return 0
    if state["lifecycle"].get("stop_requested"):
        append_event(runtime_dir, "tick_skipped", {"reason": "stop_requested"})
        return 0
    if state["supervisor"].get("paused"):
        append_event(runtime_dir, "tick_skipped", {"reason": "paused"})
        state["lifecycle"]["status"] = "paused"
        save_state(runtime_dir, state)
        return 0

    state["supervisor"]["last_tick_at"] = now_iso()
    maybe_poll_lark_inputs(runtime_dir, state, args.disable_lark)
    queue_snapshot = maybe_capture_squeue(runtime_dir)
    if queue_snapshot:
        append_event(runtime_dir, "squeue_snapshot", {"lines": queue_snapshot.splitlines()[:20]})
        sync_jobs_with_squeue(state, queue_snapshot)
    sacct_snapshot = maybe_capture_sacct(runtime_dir, list(state.get("jobs", {}).keys()))
    if sacct_snapshot:
        append_event(runtime_dir, "sacct_snapshot", {"lines": sacct_snapshot.splitlines()[:20]})
        sync_jobs_with_sacct(state, sacct_snapshot)
    maybe_clear_stale_waiting_job(runtime_dir, state)

    models = [state["supervisor"]["active_model"]]
    fallback = state["supervisor"].get("fallback_model", "")
    if fallback and fallback not in models:
        models.append(fallback)
    worker_policy = effective_worker_policy(args)
    state["supervisor"]["worker_policy"] = {
        "sandbox_mode": str(worker_policy.get("sandbox_mode", "")),
        "approval_policy": str(worker_policy.get("approval_policy", "")),
        "dangerous_bypass": bool(worker_policy.get("dangerous_bypass")),
        "source": str(worker_policy.get("source", "")),
        "session_source": str(worker_policy.get("session_source", "")),
    }
    state["supervisor"]["active_tick"] = {
        "running": True,
        "started_at": state["supervisor"]["last_tick_at"],
        "model": state["supervisor"]["active_model"],
        "worker_pid": 0,
        "response_path": "",
        "session_log_path": "",
    }
    append_event(
        runtime_dir,
        "tick_started",
        {
            "model": state["supervisor"]["active_model"],
            "worker_policy": deepcopy(state["supervisor"]["worker_policy"]),
        },
    )
    save_state(runtime_dir, state)

    worker_result: dict[str, Any] | None = None
    last_output = ""
    used_model = models[0]
    for model in models:
        used_model = model
        result, output = invoke_codex_once(
            runtime_dir=runtime_dir,
            state=state,
            model=model,
            search_enabled=args.search,
            extra_config=args.codex_config,
            worker_policy=worker_policy,
        )
        last_output = output
        if result is not None:
            worker_result = result
            if state["supervisor"]["active_model"] != model:
                state["supervisor"]["active_model"] = model
                append_event(runtime_dir, "model_switched", {"model": model, "reason": "successful_fallback"})
            break
        if not detect_model_limit(output):
            break

    if worker_result is None:
        state["supervisor"]["consecutive_failures"] += 1
        state["supervisor"]["active_tick"] = {
            "running": False,
            "started_at": "",
            "model": "",
            "worker_pid": 0,
            "response_path": "",
            "session_log_path": "",
        }
        state["progress"]["summary"] = "Codex tick failed."
        state["progress"]["next_sleep_seconds"] = DEFAULT_FAILURE_RETRY_SECONDS
        state["progress"]["sleep_reason"] = (
            "Worker tick failed or timed out; retry in five minutes. Reserve one-hour polling for real waiting-job loops."
        )
        state["progress"]["planned_wake_at"] = iso_after_seconds(DEFAULT_FAILURE_RETRY_SECONDS)
        merge_execution_packet(runtime_dir, state, packet={}, overwrite_missing_only=False)
        append_event(runtime_dir, "tick_failed", {"model": used_model, "output": last_output[-4000:]})
        save_state(runtime_dir, state)
        return 1

    merge_jobs(state, worker_result["jobs_submitted"])
    merge_plan(state, worker_result["plan_updates"])
    acknowledge_inputs(runtime_dir, state, worker_result["acknowledged_input_ids"], "Acknowledged by worker.")
    state["supervisor"]["consecutive_failures"] = 0
    state["supervisor"]["active_tick"] = {
        "running": False,
        "started_at": "",
        "model": "",
        "worker_pid": 0,
        "response_path": "",
        "session_log_path": "",
    }
    state["progress"]["summary"] = worker_result["summary"]
    state["progress"]["last_worker_status"] = worker_result["status"]
    state["progress"]["artifacts_updated"] = worker_result["artifacts_updated"]
    sleep_seconds, sleep_reason = choose_sleep_policy(runtime_dir, state, worker_result)
    state["progress"]["next_sleep_seconds"] = sleep_seconds
    state["progress"]["sleep_reason"] = sleep_reason
    state["progress"]["planned_wake_at"] = iso_after_seconds(sleep_seconds) if sleep_seconds > 0 else ""
    merge_execution_packet(
        runtime_dir,
        state,
        {
            "current_phase": worker_result.get("current_phase", {}),
            "phase_plan": worker_result.get("phase_plan", []),
            "next_action": worker_result.get("next_action", {}),
        },
        overwrite_missing_only=False,
    )
    state["history"]["ticks_completed"] += 1
    record_summary(runtime_dir, state, worker_result)
    append_event(runtime_dir, "tick_completed", {"model": used_model, "status": worker_result["status"]})

    if worker_result["lark_update_markdown"] and not args.disable_lark:
        update_lark_doc(runtime_dir, state, worker_result["lark_update_markdown"])

    worker_status = worker_result["status"]
    if worker_status == "done":
        state["lifecycle"]["status"] = "completed"
        state["lifecycle"]["completed_at"] = now_iso()
        if not args.disable_lark and not state["lark"].get("final_summary_written"):
            if update_lark_doc(runtime_dir, state, generate_final_summary_markdown(state, worker_result)):
                state["lark"]["final_summary_written"] = True
    elif worker_status == "waiting_job":
        state["lifecycle"]["status"] = "waiting_job"
    elif worker_status == "blocked":
        state["lifecycle"]["status"] = "blocked"
    elif worker_status == "failed":
        state["lifecycle"]["status"] = "failed"
    else:
        state["lifecycle"]["status"] = "running"

    if worker_result["stop_reason"]:
        state["lifecycle"]["stop_requested"] = True
        state["lifecycle"]["stop_reason"] = worker_result["stop_reason"]

    maybe_send_periodic_heartbeat(runtime_dir, state, args.disable_lark)
    save_state(runtime_dir, state)
    return 0


def install_signal_handlers(runtime_dir: Path | None = None) -> None:
    def handler(signum: int, _frame: Any) -> None:
        global GLOBAL_STOP_REQUESTED
        GLOBAL_STOP_REQUESTED = True
        if runtime_dir is not None and (runtime_dir / "state.json").exists():
            state = load_state(runtime_dir)
            state["lifecycle"]["stop_requested"] = True
            state["lifecycle"]["stop_reason"] = f"Received signal {signum}"
            save_state(runtime_dir, state)
            update_lark_doc(
                runtime_dir,
                state,
                f"## {SYSTEM_SECTION_PREFIX} Stopped\n\n- Time: {now_iso()}\n- Reason: received signal `{signum}`.\n",
            )

    for sig in STOP_SIGNALS:
        signal.signal(sig, handler)


def init_runtime(args: argparse.Namespace) -> int:
    mission_path = Path(args.mission).expanduser().resolve()
    if not mission_path.exists():
        raise SystemExit(f"Mission file not found: {mission_path}")

    mission_text = read_text(mission_path)
    title = extract_title(mission_text, mission_path.stem)
    runtime_dir = resolve_runtime_dir(args.runtime_dir)
    ensure_runtime_layout(runtime_dir)

    state = default_state(
        mission_path,
        runtime_dir,
        title,
        extract_doc_urls(mission_text),
        seeded_plan_steps(mission_text, mission_path=mission_path, doc_urls=extract_doc_urls(mission_text)),
    )
    state["lifecycle"]["status"] = "awaiting_plan_confirmation"
    if args.doc_url:
        urls = state["mission"]["doc_urls"]
        if args.doc_url not in urls:
            urls.append(args.doc_url)

    write_text(runtime_dir / "mission.md", mission_text)
    write_text(runtime_dir / "prompts" / "worker_prompt.md", render_worker_prompt(runtime_dir, state))
    write_text(runtime_dir / "runbook.md", render_runbook(state))
    write_text(runtime_dir / "notes" / "latest_summary.md", "# Latest Summary\n\nRuntime initialized.\n")
    merge_execution_packet(runtime_dir, state, packet={}, overwrite_missing_only=False)
    save_state(runtime_dir, state)
    append_event(runtime_dir, "runtime_initialized", {"mission_path": str(mission_path), "runtime_dir": str(runtime_dir)})
    print(str(runtime_dir))
    return 0


def start_runtime(args: argparse.Namespace) -> int:
    runtime_dir = resolve_runtime_dir(args.runtime_dir)
    ensure_runtime_layout(runtime_dir)
    if not (runtime_dir / "state.json").exists():
        raise SystemExit(f"Runtime not initialized: {runtime_dir}")
    state = load_state(runtime_dir)
    if state["lifecycle"].get("status") == "awaiting_plan_confirmation":
        raise SystemExit("Runtime is waiting for plan confirmation. Use mode-approve-plan or --auto-approve-plan flow before starting execution.")
    install_signal_handlers(runtime_dir)

    while True:
        exit_code = perform_tick(runtime_dir, args)
        state = load_state(runtime_dir)
        if args.once:
            return exit_code
        if GLOBAL_STOP_REQUESTED or state["lifecycle"].get("stop_requested"):
            return exit_code
        if state["lifecycle"]["status"] in {"completed", "failed", "blocked"}:
            return exit_code

        sleep_seconds = clamp_sleep_seconds(state["progress"].get("next_sleep_seconds", DEFAULT_INTERVAL_SECONDS))
        state["supervisor"]["last_sleep_seconds"] = sleep_seconds
        state["progress"]["next_sleep_seconds"] = sleep_seconds
        state["progress"]["planned_wake_at"] = iso_after_seconds(sleep_seconds) if sleep_seconds > 0 else ""
        if not state["progress"].get("sleep_reason", "").strip():
            state["progress"]["sleep_reason"] = (
                "Continue immediately with the next worker burst."
                if sleep_seconds <= 0
                else "Use the default one-hour polling interval unless the worker provides a shorter estimate."
            )
        save_state(runtime_dir, state)
        if sleep_seconds <= 0:
            append_event(
                runtime_dir,
                "supervisor_continue",
                {
                    "reason": state["progress"].get("sleep_reason", ""),
                },
            )
            continue
        append_event(
            runtime_dir,
            "supervisor_sleep",
            {
                "seconds": sleep_seconds,
                "reason": state["progress"].get("sleep_reason", ""),
                "planned_wake_at": state["progress"].get("planned_wake_at", ""),
            },
        )
        time.sleep(sleep_seconds)


def print_status(args: argparse.Namespace) -> int:
    runtime_dir = resolve_runtime_dir(args.runtime_dir)
    state = load_state(runtime_dir)
    payload = {
        "runtime_dir": str(runtime_dir),
        "title": state["mission"]["title"],
        "status": state["lifecycle"]["status"],
        "last_tick_at": state["supervisor"]["last_tick_at"],
        "active_model": state["supervisor"]["active_model"],
        "worker_policy": state["supervisor"].get("worker_policy", {}),
        "next_sleep_seconds": state["progress"]["next_sleep_seconds"],
        "sleep_reason": state["progress"].get("sleep_reason", ""),
        "planned_wake_at": state["progress"].get("planned_wake_at", ""),
        "summary": state["progress"]["summary"],
        "jobs": list(state["jobs"].keys()),
        "focused_job_ids": select_focused_job_ids(runtime_dir, state),
        "plan": current_plan_items(state),
        "plan_preview": preview_plan_items(state),
        "planning": state.get("planning", {}),
        "execution": state.get("execution", {}),
        "inputs": state.get("inputs", {}),
        "lark": state.get("lark", {}),
        "daemon": daemon_snapshot(runtime_dir),
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        for key, value in payload.items():
            print(f"{key}: {value}")
    return 0


def stop_runtime(args: argparse.Namespace) -> int:
    runtime_dir = resolve_runtime_dir(args.runtime_dir)
    state = load_state(runtime_dir)
    state["lifecycle"]["stop_requested"] = True
    state["lifecycle"]["stop_reason"] = args.reason
    state["lifecycle"]["status"] = "stopped"
    save_state(runtime_dir, state)
    append_event(runtime_dir, "stop_requested", {"reason": args.reason})
    if not args.disable_lark:
        update_lark_doc(
            runtime_dir,
            state,
            f"## {SYSTEM_SECTION_PREFIX} Stopped\n\n- Time: {now_iso()}\n- Reason: {args.reason}\n",
        )
    return 0


def mode_status(args: argparse.Namespace) -> int:
    runtime_dir = resolve_runtime_dir(args.runtime_dir)
    if not (runtime_dir / "state.json").exists():
        raise SystemExit(f"Runtime not initialized: {runtime_dir}")
    state = load_state(runtime_dir)
    if state["lifecycle"].get("status") == "awaiting_plan_confirmation":
        print(render_plan_preview(runtime_dir))
    else:
        print(render_mode_report(runtime_dir, flavor="status"))
    return 0


def mode_start(args: argparse.Namespace) -> int:
    runtime_dir = resolve_runtime_dir(args.runtime_dir)
    if not (runtime_dir / "state.json").exists():
        if not args.mission:
            raise SystemExit(f"Runtime not initialized: {runtime_dir}. Provide --mission to bootstrap it.")
        init_args = argparse.Namespace(
            mission=args.mission,
            runtime_dir=str(runtime_dir),
            doc_url=args.doc_url,
        )
        init_runtime(init_args)

    state = load_state(runtime_dir)
    if args.auto_approve_plan:
        approve_args = argparse.Namespace(
            runtime_dir=str(runtime_dir),
            daemon=args.daemon,
            search=args.search,
            disable_lark=args.disable_lark,
            codex_config=args.codex_config,
            worker_sandbox=args.worker_sandbox,
            worker_approval_policy=args.worker_approval_policy,
            worker_full_access=args.worker_full_access,
        )
        return mode_approve_plan(approve_args)

    if args.daemon:
        append_event(runtime_dir, "daemon_start_deferred", {"reason": "awaiting_plan_confirmation"})

    state["lifecycle"]["status"] = "awaiting_plan_confirmation"
    state["progress"]["summary"] = "Plan preview generated. Waiting for user confirmation before execution."
    state["progress"]["next_sleep_seconds"] = DEFAULT_INTERVAL_SECONDS
    state["progress"]["sleep_reason"] = "Plan is awaiting confirmation; keep the default one-hour background polling interval if needed."
    state["progress"]["planned_wake_at"] = iso_after_seconds(DEFAULT_INTERVAL_SECONDS)
    merge_execution_packet(runtime_dir, state, packet={}, overwrite_missing_only=False)
    save_state(runtime_dir, state)
    print(render_plan_preview(runtime_dir))
    return 0


def mode_approve_plan(args: argparse.Namespace) -> int:
    runtime_dir = resolve_runtime_dir(args.runtime_dir)
    if not (runtime_dir / "state.json").exists():
        raise SystemExit(f"Runtime not initialized: {runtime_dir}")
    state = load_state(runtime_dir)
    preview = state.get("planning", {}).get("preview", {})
    items = deepcopy(preview.get("items", []))
    if not items:
        raise SystemExit("Plan preview is empty. Revise or regenerate the preview before approval.")

    approved_at = now_iso()
    set_current_plan_items(state, items)
    preview["approved"] = True
    preview["approved_at"] = approved_at
    state["planning"]["current"]["version"] = max(int(preview.get("version", 1)), int(state["planning"]["current"].get("version", 0)))
    state["planning"]["current"]["approved_at"] = approved_at
    state["lifecycle"]["status"] = "running"
    state["progress"]["summary"] = "Plan approved. Ready to execute."
    state["progress"]["next_sleep_seconds"] = 0
    state["progress"]["sleep_reason"] = "Execution is approved; continue immediately once the daemon starts."
    state["progress"]["planned_wake_at"] = ""
    merge_execution_packet(runtime_dir, state, packet={}, overwrite_missing_only=False)
    append_event(runtime_dir, "plan_confirmed", {"version": preview.get("version", 1)})
    save_state(runtime_dir, state)

    if args.daemon:
        daemon_args = argparse.Namespace(
            runtime_dir=str(runtime_dir),
            search=args.search,
            disable_lark=args.disable_lark,
            codex_config=args.codex_config,
            worker_sandbox=args.worker_sandbox,
            worker_approval_policy=args.worker_approval_policy,
            worker_full_access=args.worker_full_access,
        )
        try:
            daemon_start(daemon_args)
        except SystemExit as exc:
            if "Supervisor already running" not in str(exc):
                raise

    print(render_mode_report(runtime_dir, flavor="status"))
    return 0


def mode_revise_plan(args: argparse.Namespace) -> int:
    runtime_dir = resolve_runtime_dir(args.runtime_dir)
    if not (runtime_dir / "state.json").exists():
        raise SystemExit(f"Runtime not initialized: {runtime_dir}")

    if args.file:
        revision_note = read_text(Path(args.file).expanduser().resolve()).strip()
    else:
        revision_note = args.message.strip()
    if not revision_note:
        raise SystemExit("Revision message is empty")

    forwarded = argparse.Namespace(
        runtime_dir=str(runtime_dir),
        message=revision_note,
        file="",
        source="chat",
        title=args.title or "Plan revision request",
        author=args.author,
        json=False,
        quiet=True,
    )
    add_input(forwarded)

    state = load_state(runtime_dir)
    base_items = preview_plan_items(state) or current_plan_items(state) or seeded_plan_steps(
        read_text(runtime_dir / "mission.md"),
        mission_path=Path(state["mission"].get("source_path", "")).expanduser() if state.get("mission", {}).get("source_path") else None,
        doc_urls=list(state.get("mission", {}).get("doc_urls", [])),
    )
    revised_items = [{"step": f"Apply the latest user steering before execution: {revision_note}", "status": "in_progress"}]
    for item in base_items:
        step = str(item.get("step", "")).strip()
        if not step:
            continue
        revised_items.append({"step": step, "status": "pending"})
    set_preview_plan_items(state, revised_items, source="user_revision", revision_note=revision_note)
    state["lifecycle"]["status"] = "awaiting_plan_confirmation"
    state["progress"]["summary"] = "Plan preview revised. Waiting for user confirmation before execution."
    state["progress"]["next_sleep_seconds"] = DEFAULT_INTERVAL_SECONDS
    state["progress"]["sleep_reason"] = "The plan was revised; keep the default one-hour polling interval until the next confirmation."
    state["progress"]["planned_wake_at"] = iso_after_seconds(DEFAULT_INTERVAL_SECONDS)
    merge_execution_packet(runtime_dir, state, packet={}, overwrite_missing_only=False)
    append_event(runtime_dir, "plan_revised", {"version": state["planning"]["preview"]["version"], "note": revision_note})
    save_state(runtime_dir, state)
    print(render_plan_preview(runtime_dir))
    return 0


def mode_sync(args: argparse.Namespace) -> int:
    runtime_dir = resolve_runtime_dir(args.runtime_dir)
    if not (runtime_dir / "state.json").exists():
        raise SystemExit(f"Runtime not initialized: {runtime_dir}")
    state = load_state(runtime_dir)
    if state["lifecycle"].get("status") == "awaiting_plan_confirmation":
        print(render_plan_preview(runtime_dir))
    else:
        print(render_mode_report(runtime_dir, flavor="sync"))
    return 0


def mode_update(args: argparse.Namespace) -> int:
    forwarded = argparse.Namespace(
        runtime_dir=args.runtime_dir,
        message=args.message,
        file=args.file,
        source="chat",
        title=args.title,
        author=args.author,
        json=False,
        quiet=True,
    )
    add_input(forwarded)
    runtime_dir = resolve_runtime_dir(args.runtime_dir)
    print(render_mode_report(runtime_dir, flavor="sync"))
    return 0


def mode_plan(args: argparse.Namespace) -> int:
    runtime_dir = resolve_runtime_dir(args.runtime_dir)
    state = load_state(runtime_dir)
    if state["lifecycle"].get("status") == "awaiting_plan_confirmation":
        print("**Plan Preview**\n")
        print("\n".join(summarize_preview_plan(state)))
    else:
        print("**Current Plan**\n")
        print("\n".join(summarize_plan(state)))
    return 0


def mode_jobs(args: argparse.Namespace) -> int:
    runtime_dir = resolve_runtime_dir(args.runtime_dir)
    state = load_state(runtime_dir)
    print("**Active Jobs**\n")
    print("\n".join(summarize_jobs(state)))
    return 0


def mode_pause(args: argparse.Namespace) -> int:
    runtime_dir = resolve_runtime_dir(args.runtime_dir)
    state = load_state(runtime_dir)
    state["supervisor"]["paused"] = True
    state["lifecycle"]["status"] = "paused"
    append_event(runtime_dir, "mode_paused", {})
    save_state(runtime_dir, state)
    print(render_mode_report(runtime_dir, flavor="status"))
    return 0


def mode_resume(args: argparse.Namespace) -> int:
    runtime_dir = resolve_runtime_dir(args.runtime_dir)
    state = load_state(runtime_dir)
    state["supervisor"]["paused"] = False
    if state["lifecycle"].get("status") in {"paused", "stopped", "blocked", "failed"}:
        state["lifecycle"]["status"] = "running"
    state["lifecycle"]["stop_requested"] = False
    state["lifecycle"]["stop_reason"] = ""
    state["progress"]["next_sleep_seconds"] = 0
    state["progress"]["sleep_reason"] = "Resumed; continue immediately with the next worker burst."
    state["progress"]["planned_wake_at"] = ""
    append_event(runtime_dir, "mode_resumed", {})
    save_state(runtime_dir, state)
    print(render_mode_report(runtime_dir, flavor="status"))
    return 0


def mode_stop(args: argparse.Namespace) -> int:
    runtime_dir = resolve_runtime_dir(args.runtime_dir)
    stop_args = argparse.Namespace(
        runtime_dir=str(runtime_dir),
        reason=args.reason,
        disable_lark=args.disable_lark,
    )
    if args.daemon:
        daemon_stop(stop_args)
    else:
        stop_runtime(stop_args)
    print(render_mode_report(runtime_dir, flavor="status"))
    return 0


def submit_job(args: argparse.Namespace) -> int:
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    ensure_runtime_layout(runtime_dir)
    if not (runtime_dir / "state.json").exists():
        raise SystemExit(f"Runtime not initialized: {runtime_dir}")
    script_path = Path(args.script).expanduser().resolve()
    if not script_path.exists():
        raise SystemExit(f"Script not found: {script_path}")

    cmd = ["sbatch"]
    for item in args.sbatch_arg:
        cmd.extend(shlex.split(item))
    cmd.append(str(script_path))
    result = capture_command(cmd, cwd=runtime_dir)
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise SystemExit(output.strip() or "sbatch failed")

    match = re.search(r"Submitted batch job\s+(\d+)", output)
    if not match:
        raise SystemExit(f"Could not parse job id from sbatch output: {output.strip()}")
    job_id = match.group(1)

    metadata = {
        "job_id": job_id,
        "status": "submitted",
        "script": str(script_path),
        "submitted_at": now_iso(),
        "notes": args.notes,
    }
    write_json(runtime_dir / "jobs" / f"{job_id}.json", metadata)

    state = load_state(runtime_dir)
    merge_jobs(state, [metadata])
    save_state(runtime_dir, state)
    append_event(runtime_dir, "job_submitted", {"job_id": job_id, "script": str(script_path)})

    payload = {"job_id": job_id, "script": str(script_path), "output": output.strip()}
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else output.strip())
    return 0


def sync_jobs_command(args: argparse.Namespace) -> int:
    runtime_dir = resolve_runtime_dir(args.runtime_dir)
    ensure_runtime_layout(runtime_dir)
    if not (runtime_dir / "state.json").exists():
        raise SystemExit(f"Runtime not initialized: {runtime_dir}")
    state = load_state(runtime_dir)
    queue_snapshot = maybe_capture_squeue(runtime_dir)
    if queue_snapshot:
        sync_jobs_with_squeue(state, queue_snapshot)
    sacct_snapshot = maybe_capture_sacct(runtime_dir, list(state.get("jobs", {}).keys()))
    if sacct_snapshot:
        sync_jobs_with_sacct(state, sacct_snapshot)
    maybe_clear_stale_waiting_job(runtime_dir, state)
    save_state(runtime_dir, state)
    payload = list(state.get("jobs", {}).values())
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else "\n".join(f"{item['job_id']}: {item.get('status', '')}" for item in payload))
    return 0


def list_jobs_command(args: argparse.Namespace) -> int:
    runtime_dir = resolve_runtime_dir(args.runtime_dir)
    ensure_runtime_layout(runtime_dir)
    state = load_state(runtime_dir)
    payload = list(state.get("jobs", {}).values())
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else "\n".join(f"{item['job_id']}: {item.get('status', '')}" for item in payload))
    return 0


def add_input(args: argparse.Namespace) -> int:
    runtime_dir = resolve_runtime_dir(args.runtime_dir)
    ensure_runtime_layout(runtime_dir)
    if not (runtime_dir / "state.json").exists():
        raise SystemExit(f"Runtime not initialized: {runtime_dir}")

    if args.file:
        content = read_text(Path(args.file).expanduser().resolve())
    else:
        content = args.message
    if not content or not content.strip():
        raise SystemExit("Input content is empty")

    item = make_input_item(
        source=args.source,
        content=content,
        title=args.title,
        author=args.author,
    )
    items = load_inputs(runtime_dir)
    items.append(item)
    write_inputs(runtime_dir, items)
    write_pending_inputs_markdown(runtime_dir, [entry for entry in items if entry.get("status") == "pending"])

    state = load_state(runtime_dir)
    refresh_input_counters(state, items)
    save_state(runtime_dir, state)
    append_event(runtime_dir, "input_added", {"id": item["id"], "source": item["source"], "title": item["title"]})

    if args.json:
        print(json.dumps(item, indent=2, ensure_ascii=False))
    elif not getattr(args, "quiet", False):
        print(f"added input {item['id']}")
    return 0


def list_inputs(args: argparse.Namespace) -> int:
    runtime_dir = resolve_runtime_dir(args.runtime_dir)
    ensure_runtime_layout(runtime_dir)
    items = load_inputs(runtime_dir)
    if args.pending_only:
        items = [item for item in items if item.get("status") == "pending"]

    if args.limit:
        items = items[-args.limit :]

    if args.json:
        print(json.dumps(items, indent=2, ensure_ascii=False))
        return 0

    for item in items:
        title = f" | {item['title']}" if item.get("title") else ""
        print(f"{item['id']} | {item['status']} | {item['source']} | {item['created_at']}{title}")
    return 0


def ack_input(args: argparse.Namespace) -> int:
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    ensure_runtime_layout(runtime_dir)
    if not (runtime_dir / "state.json").exists():
        raise SystemExit(f"Runtime not initialized: {runtime_dir}")

    items = load_inputs(runtime_dir)
    target = None
    for item in items:
        if item.get("id") == args.input_id:
            target = item
            break
    if target is None:
        raise SystemExit(f"Input not found: {args.input_id}")

    target["status"] = "acknowledged"
    target["acknowledged_at"] = now_iso()
    target["resolution"] = args.resolution.strip()
    write_inputs(runtime_dir, items)
    write_pending_inputs_markdown(runtime_dir, [entry for entry in items if entry.get("status") == "pending"])

    state = load_state(runtime_dir)
    refresh_input_counters(state, items)
    save_state(runtime_dir, state)
    append_event(runtime_dir, "input_acknowledged", {"id": target["id"], "resolution": target["resolution"]})

    if args.json:
        print(json.dumps(target, indent=2, ensure_ascii=False))
    else:
        print(f"acknowledged input {target['id']}")
    return 0


def daemon_start(args: argparse.Namespace) -> int:
    runtime_dir = resolve_runtime_dir(args.runtime_dir)
    ensure_runtime_layout(runtime_dir)
    if not (runtime_dir / "state.json").exists():
        raise SystemExit(f"Runtime not initialized: {runtime_dir}")
    state = load_state(runtime_dir)
    if state["lifecycle"].get("status") == "awaiting_plan_confirmation":
        raise SystemExit("Runtime is waiting for plan confirmation. Approve the plan before starting the daemon.")

    snapshot = daemon_snapshot(runtime_dir)
    if snapshot["running"]:
        raise SystemExit(f"Supervisor already running with pid {snapshot['pid']}")

    log_path = Path(snapshot["log_path"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("a", encoding="utf-8")
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "start",
        str(runtime_dir),
    ]
    if args.search:
        cmd.append("--search")
    if args.disable_lark:
        cmd.append("--disable-lark")
    for item in args.codex_config:
        cmd.extend(["--codex-config", item])
    if args.worker_sandbox != "inherit":
        cmd.extend(["--worker-sandbox", args.worker_sandbox])
    if args.worker_approval_policy != "inherit":
        cmd.extend(["--worker-approval-policy", args.worker_approval_policy])
    if args.worker_full_access:
        cmd.append("--worker-full-access")

    process = subprocess.Popen(  # noqa: S603
        cmd,
        cwd=str(runtime_dir),
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
    )
    log_handle.close()
    write_text(pid_file_path(runtime_dir), f"{process.pid}\n")
    append_event(runtime_dir, "daemon_started", {"pid": process.pid, "log_path": str(log_path)})
    print(json.dumps({"runtime_dir": str(runtime_dir), "pid": process.pid, "log_path": str(log_path)}, ensure_ascii=False))
    return 0


def daemon_stop(args: argparse.Namespace) -> int:
    runtime_dir = resolve_runtime_dir(args.runtime_dir)
    ensure_runtime_layout(runtime_dir)
    snapshot = daemon_snapshot(runtime_dir)
    pid = snapshot["pid"]
    if pid is None:
        stop_args = argparse.Namespace(runtime_dir=str(runtime_dir), reason=args.reason, disable_lark=args.disable_lark)
        stop_runtime(stop_args)
        print(json.dumps({"runtime_dir": str(runtime_dir), "running": False, "stopped": False}, ensure_ascii=False))
        return 0

    state = load_state(runtime_dir)
    state["lifecycle"]["stop_requested"] = True
    state["lifecycle"]["stop_reason"] = args.reason
    state["lifecycle"]["status"] = "stopped"
    save_state(runtime_dir, state)
    append_event(runtime_dir, "daemon_stop_requested", {"pid": pid, "reason": args.reason})
    os.kill(pid, signal.SIGTERM)
    print(json.dumps({"runtime_dir": str(runtime_dir), "running": True, "pid": pid, "signal": "SIGTERM"}, ensure_ascii=False))
    return 0


def daemon_status(args: argparse.Namespace) -> int:
    runtime_dir = resolve_runtime_dir(args.runtime_dir)
    snapshot = daemon_snapshot(runtime_dir)
    state = load_state(runtime_dir) if (runtime_dir / "state.json").exists() else None
    payload = {
        "runtime_dir": str(runtime_dir),
        "daemon": snapshot,
        "lifecycle_status": state["lifecycle"]["status"] if state else "missing",
        "summary": state["progress"]["summary"] if state else "",
        "inputs": state.get("inputs", {}) if state else {},
        "active_tick": collect_live_worker_snapshot(runtime_dir, state) if state else {},
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else "\n".join(f"{k}: {v}" for k, v in payload.items()))
    return 0


def add_worker_exec_args(parser: argparse.ArgumentParser, *, daemon_help: bool = False) -> None:
    parser.add_argument(
        "--worker-sandbox",
        choices=["inherit", "read-only", "workspace-write", "danger-full-access"],
        default="inherit",
        help="Worker sandbox mode. Defaults to inheriting the current Codex session, then falling back to workspace-write.",
    )
    parser.add_argument(
        "--worker-approval-policy",
        choices=["inherit", "untrusted", "on-failure", "on-request", "never"],
        default="inherit",
        help="Worker approval policy. Defaults to inheriting the current Codex session, then falling back to on-request.",
    )
    parser.add_argument(
        "--worker-full-access",
        action="store_true",
        help=(
            "Force worker bursts to run with danger-full-access and approval_policy=never. "
            "Use this when the worker must edit freely and submit jobs without approval prompts."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Single-file autoresearch runtime for Codex.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create a runtime from one autoresearch markdown file.")
    init_parser.add_argument("mission", help="Path to autoresearch.md")
    init_parser.add_argument(
        "--runtime-dir",
        default="",
        help=f"Where to create the runtime state. Defaults to ./{DEFAULT_RUNTIME_DIRNAME} in the current working directory",
    )
    init_parser.add_argument("--doc-url", default="", help="Optional Lark/Feishu doc URL to append updates to")
    init_parser.set_defaults(func=init_runtime)

    start_parser = subparsers.add_parser("start", help="Start or continue the supervisor loop.")
    start_parser.add_argument(
        "runtime_dir",
        nargs="?",
        default="",
        help=f"Runtime directory created by init. Defaults to ./{DEFAULT_RUNTIME_DIRNAME} in the current working directory",
    )
    start_parser.add_argument("--once", action="store_true", help="Execute a single Codex tick")
    start_parser.add_argument("--search", action="store_true", help="Enable web search inside Codex")
    start_parser.add_argument("--disable-lark", action="store_true", help="Skip Lark document updates")
    start_parser.add_argument(
        "--codex-config",
        action="append",
        default=[],
        help="Extra 'key=value' values passed through to codex exec via -c",
    )
    add_worker_exec_args(start_parser)
    start_parser.set_defaults(func=start_runtime)

    status_parser = subparsers.add_parser("status", help="Show runtime status.")
    status_parser.add_argument(
        "runtime_dir",
        nargs="?",
        default="",
        help=f"Runtime directory created by init. Defaults to ./{DEFAULT_RUNTIME_DIRNAME} in the current working directory",
    )
    status_parser.add_argument("--json", action="store_true", help="Print JSON output")
    status_parser.set_defaults(func=print_status)

    stop_parser = subparsers.add_parser("stop", help="Request a graceful stop.")
    stop_parser.add_argument(
        "runtime_dir",
        nargs="?",
        default="",
        help=f"Runtime directory created by init. Defaults to ./{DEFAULT_RUNTIME_DIRNAME} in the current working directory",
    )
    stop_parser.add_argument("--reason", default="manual stop", help="Reason to record in state and Lark")
    stop_parser.add_argument("--disable-lark", action="store_true", help="Skip Lark document updates")
    stop_parser.set_defaults(func=stop_runtime)

    mode_start_parser = subparsers.add_parser("mode-start", help="Enter Auto-Codex conversation mode for a runtime.")
    mode_start_parser.add_argument(
        "runtime_dir",
        nargs="?",
        default="",
        help=f"Runtime directory created by init. Defaults to ./{DEFAULT_RUNTIME_DIRNAME} in the current working directory",
    )
    mode_start_parser.add_argument("--mission", default="", help="Mission markdown used to bootstrap the runtime if needed")
    mode_start_parser.add_argument("--doc-url", default="", help="Optional Lark/Feishu doc URL to append updates to")
    mode_start_parser.add_argument("--daemon", action="store_true", help="Start the background supervisor if it is not running")
    mode_start_parser.add_argument("--search", action="store_true", help="Enable web search inside Codex when starting the daemon")
    mode_start_parser.add_argument("--disable-lark", action="store_true", help="Skip Lark document updates when starting the daemon")
    mode_start_parser.add_argument(
        "--auto-approve-plan",
        action="store_true",
        help="Approve the generated preview plan immediately and continue into execution.",
    )
    mode_start_parser.add_argument(
        "--codex-config",
        action="append",
        default=[],
        help="Extra 'key=value' values passed through to codex exec via -c when starting the daemon",
    )
    add_worker_exec_args(mode_start_parser)
    mode_start_parser.set_defaults(func=mode_start)

    mode_approve_parser = subparsers.add_parser("mode-approve-plan", help="Approve the current plan preview and enter execution mode.")
    mode_approve_parser.add_argument(
        "runtime_dir",
        nargs="?",
        default="",
        help=f"Runtime directory created by init. Defaults to ./{DEFAULT_RUNTIME_DIRNAME} in the current working directory",
    )
    mode_approve_parser.add_argument("--daemon", action="store_true", help="Start the background supervisor after approval")
    mode_approve_parser.add_argument("--search", action="store_true", help="Enable web search inside Codex when starting the daemon")
    mode_approve_parser.add_argument("--disable-lark", action="store_true", help="Skip Lark document updates when starting the daemon")
    mode_approve_parser.add_argument(
        "--codex-config",
        action="append",
        default=[],
        help="Extra 'key=value' values passed through to codex exec via -c when starting the daemon",
    )
    add_worker_exec_args(mode_approve_parser)
    mode_approve_parser.set_defaults(func=mode_approve_plan)

    mode_revise_parser = subparsers.add_parser("mode-revise-plan", help="Revise the current plan preview before execution starts.")
    mode_revise_parser.add_argument(
        "runtime_dir",
        nargs="?",
        default="",
        help=f"Runtime directory created by init. Defaults to ./{DEFAULT_RUNTIME_DIRNAME} in the current working directory",
    )
    mode_revise_parser.add_argument("--message", default="", help="Revision note as a direct string")
    mode_revise_parser.add_argument("--file", default="", help="Read the revision note from a file")
    mode_revise_parser.add_argument("--title", default="Plan revision request", help="Optional short title")
    mode_revise_parser.add_argument("--author", default="user", help="Author label")
    mode_revise_parser.set_defaults(func=mode_revise_plan)

    mode_status_parser = subparsers.add_parser("mode-status", help="Render a conversation-style runtime status report.")
    mode_status_parser.add_argument(
        "runtime_dir",
        nargs="?",
        default="",
        help=f"Runtime directory created by init. Defaults to ./{DEFAULT_RUNTIME_DIRNAME} in the current working directory",
    )
    mode_status_parser.set_defaults(func=mode_status)

    mode_sync_parser = subparsers.add_parser("mode-sync", help="Render a sync report with recent runtime events and pending inputs.")
    mode_sync_parser.add_argument(
        "runtime_dir",
        nargs="?",
        default="",
        help=f"Runtime directory created by init. Defaults to ./{DEFAULT_RUNTIME_DIRNAME} in the current working directory",
    )
    mode_sync_parser.set_defaults(func=mode_sync)

    mode_update_parser = subparsers.add_parser("mode-update", help="Add a chat-style input and render the updated sync report.")
    mode_update_parser.add_argument(
        "runtime_dir",
        nargs="?",
        default="",
        help=f"Runtime directory created by init. Defaults to ./{DEFAULT_RUNTIME_DIRNAME} in the current working directory",
    )
    mode_update_parser.add_argument("--message", default="", help="Input content as a direct string")
    mode_update_parser.add_argument("--file", default="", help="Read input content from a file")
    mode_update_parser.add_argument("--title", default="", help="Optional short title")
    mode_update_parser.add_argument("--author", default="user", help="Author label")
    mode_update_parser.set_defaults(func=mode_update)

    mode_plan_parser = subparsers.add_parser("mode-plan", help="Render the current plan in a conversation-friendly format.")
    mode_plan_parser.add_argument(
        "runtime_dir",
        nargs="?",
        default="",
        help=f"Runtime directory created by init. Defaults to ./{DEFAULT_RUNTIME_DIRNAME} in the current working directory",
    )
    mode_plan_parser.set_defaults(func=mode_plan)

    mode_jobs_parser = subparsers.add_parser("mode-jobs", help="Render active jobs in a conversation-friendly format.")
    mode_jobs_parser.add_argument(
        "runtime_dir",
        nargs="?",
        default="",
        help=f"Runtime directory created by init. Defaults to ./{DEFAULT_RUNTIME_DIRNAME} in the current working directory",
    )
    mode_jobs_parser.set_defaults(func=mode_jobs)

    mode_pause_parser = subparsers.add_parser("mode-pause", help="Pause the runtime and render the new status.")
    mode_pause_parser.add_argument(
        "runtime_dir",
        nargs="?",
        default="",
        help=f"Runtime directory created by init. Defaults to ./{DEFAULT_RUNTIME_DIRNAME} in the current working directory",
    )
    mode_pause_parser.set_defaults(func=mode_pause)

    mode_resume_parser = subparsers.add_parser("mode-resume", help="Resume a paused runtime and render the new status.")
    mode_resume_parser.add_argument(
        "runtime_dir",
        nargs="?",
        default="",
        help=f"Runtime directory created by init. Defaults to ./{DEFAULT_RUNTIME_DIRNAME} in the current working directory",
    )
    mode_resume_parser.set_defaults(func=mode_resume)

    mode_stop_parser = subparsers.add_parser("mode-stop", help="Stop the runtime and render the final status report.")
    mode_stop_parser.add_argument(
        "runtime_dir",
        nargs="?",
        default="",
        help=f"Runtime directory created by init. Defaults to ./{DEFAULT_RUNTIME_DIRNAME} in the current working directory",
    )
    mode_stop_parser.add_argument("--reason", default="manual stop", help="Reason to record in state and Lark")
    mode_stop_parser.add_argument("--disable-lark", action="store_true", help="Skip Lark document updates")
    mode_stop_parser.add_argument("--daemon", action="store_true", help="Also stop the background supervisor if it is running")
    mode_stop_parser.set_defaults(func=mode_stop)

    add_input_parser = subparsers.add_parser("add-input", help="Add a persisted user input to the runtime inbox.")
    add_input_parser.add_argument(
        "runtime_dir",
        nargs="?",
        default="",
        help=f"Runtime directory created by init. Defaults to ./{DEFAULT_RUNTIME_DIRNAME} in the current working directory",
    )
    add_input_parser.add_argument("--message", default="", help="Input content as a direct string")
    add_input_parser.add_argument("--file", default="", help="Read input content from a file")
    add_input_parser.add_argument("--source", default="manual", help="Input source label, such as manual or feishu")
    add_input_parser.add_argument("--title", default="", help="Optional short title")
    add_input_parser.add_argument("--author", default="user", help="Author label")
    add_input_parser.add_argument("--json", action="store_true", help="Print JSON output")
    add_input_parser.set_defaults(func=add_input)

    list_inputs_parser = subparsers.add_parser("list-inputs", help="List persisted runtime inputs.")
    list_inputs_parser.add_argument(
        "runtime_dir",
        nargs="?",
        default="",
        help=f"Runtime directory created by init. Defaults to ./{DEFAULT_RUNTIME_DIRNAME} in the current working directory",
    )
    list_inputs_parser.add_argument("--pending-only", action="store_true", help="Only show pending inputs")
    list_inputs_parser.add_argument("--limit", type=int, default=20, help="Show at most the most recent N inputs")
    list_inputs_parser.add_argument("--json", action="store_true", help="Print JSON output")
    list_inputs_parser.set_defaults(func=list_inputs)

    ack_input_parser = subparsers.add_parser("ack-input", help="Mark a persisted input as acknowledged.")
    ack_input_parser.add_argument("runtime_dir", help="Runtime directory created by init")
    ack_input_parser.add_argument("input_id", help="Input id to acknowledge")
    ack_input_parser.add_argument("--resolution", default="", help="Optional resolution note")
    ack_input_parser.add_argument("--json", action="store_true", help="Print JSON output")
    ack_input_parser.set_defaults(func=ack_input)

    submit_job_parser = subparsers.add_parser("submit-job", help="Submit a Slurm job and register its metadata.")
    submit_job_parser.add_argument("runtime_dir", help="Runtime directory created by init")
    submit_job_parser.add_argument("script", help="Path to an sbatch script")
    submit_job_parser.add_argument("--sbatch-arg", action="append", default=[], help="Extra argument passed to sbatch")
    submit_job_parser.add_argument("--notes", default="", help="Optional note stored with the job metadata")
    submit_job_parser.add_argument("--json", action="store_true", help="Print JSON output")
    submit_job_parser.set_defaults(func=submit_job)

    sync_jobs_parser = subparsers.add_parser("sync-jobs", help="Refresh known job statuses from squeue.")
    sync_jobs_parser.add_argument(
        "runtime_dir",
        nargs="?",
        default="",
        help=f"Runtime directory created by init. Defaults to ./{DEFAULT_RUNTIME_DIRNAME} in the current working directory",
    )
    sync_jobs_parser.add_argument("--json", action="store_true", help="Print JSON output")
    sync_jobs_parser.set_defaults(func=sync_jobs_command)

    list_jobs_parser = subparsers.add_parser("list-jobs", help="List registered job metadata.")
    list_jobs_parser.add_argument(
        "runtime_dir",
        nargs="?",
        default="",
        help=f"Runtime directory created by init. Defaults to ./{DEFAULT_RUNTIME_DIRNAME} in the current working directory",
    )
    list_jobs_parser.add_argument("--json", action="store_true", help="Print JSON output")
    list_jobs_parser.set_defaults(func=list_jobs_command)

    daemon_start_parser = subparsers.add_parser("daemon-start", help="Run the supervisor in the background.")
    daemon_start_parser.add_argument(
        "runtime_dir",
        nargs="?",
        default="",
        help=f"Runtime directory created by init. Defaults to ./{DEFAULT_RUNTIME_DIRNAME} in the current working directory",
    )
    daemon_start_parser.add_argument("--search", action="store_true", help="Enable web search inside Codex")
    daemon_start_parser.add_argument("--disable-lark", action="store_true", help="Skip Lark document updates")
    daemon_start_parser.add_argument(
        "--codex-config",
        action="append",
        default=[],
        help="Extra 'key=value' values passed through to codex exec via -c",
    )
    add_worker_exec_args(daemon_start_parser)
    daemon_start_parser.set_defaults(func=daemon_start)

    daemon_stop_parser = subparsers.add_parser("daemon-stop", help="Stop a background supervisor.")
    daemon_stop_parser.add_argument(
        "runtime_dir",
        nargs="?",
        default="",
        help=f"Runtime directory created by init. Defaults to ./{DEFAULT_RUNTIME_DIRNAME} in the current working directory",
    )
    daemon_stop_parser.add_argument("--reason", default="manual stop", help="Reason to record in state and Lark")
    daemon_stop_parser.add_argument("--disable-lark", action="store_true", help="Skip Lark document updates")
    daemon_stop_parser.set_defaults(func=daemon_stop)

    daemon_status_parser = subparsers.add_parser("daemon-status", help="Show background supervisor status.")
    daemon_status_parser.add_argument(
        "runtime_dir",
        nargs="?",
        default="",
        help=f"Runtime directory created by init. Defaults to ./{DEFAULT_RUNTIME_DIRNAME} in the current working directory",
    )
    daemon_status_parser.add_argument("--json", action="store_true", help="Print JSON output")
    daemon_status_parser.set_defaults(func=daemon_status)

    return parser


def main() -> int:
    install_signal_handlers()
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
