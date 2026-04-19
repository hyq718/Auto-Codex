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
from datetime import datetime
from pathlib import Path
from string import Template
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = PROJECT_ROOT / "templates"
SCHEMA_PATH = PROJECT_ROOT / "schemas" / "agent_response.schema.json"
DEFAULT_MODEL = "gpt-5.4"
DEFAULT_FALLBACK_MODEL = "gpt-5.3-codex-spark"
DEFAULT_INTERVAL_SECONDS = 600
DEFAULT_LARK_POLL_INTERVAL_SECONDS = 7200
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 7200
MAX_INPUT_EXCERPT_CHARS = 6000
SYSTEM_SECTION_PREFIX = "Autoresearch System:"
STOP_SIGNALS = {signal.SIGINT, signal.SIGTERM}
GLOBAL_STOP_REQUESTED = False


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


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


def seeded_plan_steps(markdown: str) -> list[dict[str, str]]:
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

    if not plan_steps:
        fallback_steps = [
            "Read the mission carefully and extract constraints, targets, and reporting sinks.",
            "Set up the runtime workspace and identify the next executable unit of work.",
            "Run the highest-priority experiment, coding task, or research action.",
            "Record evidence, update progress, and queue follow-up work based on results.",
            "Repeat until the mission is complete or a real blocker requires escalation.",
        ]
        plan_steps = fallback_steps

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
        },
        "progress": {
            "summary": "Runtime initialized.",
            "last_worker_status": "",
            "artifacts_updated": [],
            "next_sleep_seconds": DEFAULT_INTERVAL_SECONDS,
        },
        "plan": {
            "items": plan_steps,
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

    template = load_template("worker_prompt.md.tmpl")
    return template.substitute(
        runtime_dir=str(runtime_dir),
        mission_path=str(runtime_dir / "mission.md"),
        state_path=str(runtime_dir / "state.json"),
        plan_path=str(runtime_dir / "plan.json"),
        pending_inputs_path=str(pending_inputs_path),
        pending_inputs_summary=pending_inputs_summary,
        jobs_dir=str(runtime_dir / "jobs"),
        notes_dir=str(runtime_dir / "notes"),
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
    state.setdefault("plan", {"items": []})
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

    state["supervisor"].setdefault("active_model", DEFAULT_MODEL)
    state["supervisor"].setdefault("fallback_model", DEFAULT_FALLBACK_MODEL)
    state["supervisor"].setdefault("last_tick_at", "")
    state["supervisor"].setdefault("last_sleep_seconds", 0)
    state["supervisor"].setdefault("consecutive_failures", 0)

    state["progress"].setdefault("summary", "")
    state["progress"].setdefault("last_worker_status", "")
    state["progress"].setdefault("artifacts_updated", [])
    state["progress"].setdefault("next_sleep_seconds", DEFAULT_INTERVAL_SECONDS)

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
    for item in state.get("plan", {}).get("items", []):
        status = item.get("status", "pending")
        step = item.get("step", "")
        lines.append(f"- [{status}] {step}")
    lines.append("")
    write_text(runtime_dir / "notes" / "plan.md", "\n".join(lines))


def save_state(runtime_dir: Path, state: dict[str, Any]) -> None:
    state = ensure_state_defaults(state)
    state["lifecycle"]["updated_at"] = now_iso()
    write_json(runtime_dir / "state.json", state)
    write_json(runtime_dir / "plan.json", state.get("plan", {"items": []}))
    write_plan_markdown(runtime_dir, state)


def load_state(runtime_dir: Path) -> dict[str, Any]:
    return ensure_state_defaults(read_json(runtime_dir / "state.json"))


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


def refresh_input_counters(state: dict[str, Any], items: list[dict[str, Any]]) -> None:
    pending = [item for item in items if item.get("status") == "pending"]
    acknowledged = [item for item in items if item.get("status") == "acknowledged"]
    state["inputs"] = {
        "total": len(items),
        "pending": len(pending),
        "last_added_at": items[-1]["created_at"] if items else "",
        "last_acknowledged_at": acknowledged[-1]["acknowledged_at"] if acknowledged else "",
    }


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


def record_summary(runtime_dir: Path, result: dict[str, Any]) -> None:
    summary = textwrap.dedent(
        f"""\
        # Latest Summary

        Updated: {now_iso()}
        Worker status: {result.get("status", "unknown")}

        {result.get("summary", "").strip()}
        """
    )
    write_text(runtime_dir / "notes" / "latest_summary.md", summary)


def capture_command(
    cmd: list[str],
    cwd: Path | None = None,
    stdin_text: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    if env:
        command_env.update(env)
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        input=stdin_text,
        text=True,
        capture_output=True,
        env=command_env,
        check=False,
    )


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
    result.setdefault("jobs_submitted", [])
    result.setdefault("artifacts_updated", [])
    result.setdefault("lark_update_markdown", "")
    result.setdefault("model_switch_recommended", "")
    result.setdefault("stop_reason", "")
    result.setdefault("plan_updates", [])
    result.setdefault("acknowledged_input_ids", [])
    result.setdefault("final_summary_markdown", "")
    return result


def write_supervisor_log(runtime_dir: Path, name: str, content: str) -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    write_text(runtime_dir / "logs" / "codex" / f"{stamp}-{name}.log", content)


def detect_model_limit(output: str) -> bool:
    lowered = output.lower()
    needles = ("rate limit", "limit reached", "quota", "too many requests")
    return any(needle in lowered for needle in needles)


def codex_command(
    runtime_dir: Path,
    model: str,
    search_enabled: bool,
    response_path: Path,
    extra_config: list[str],
) -> list[str]:
    cmd = [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--full-auto",
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
    if search_enabled:
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
        state["plan"]["items"] = cleaned


def sync_jobs_with_squeue(state: dict[str, Any], queue_snapshot: str) -> None:
    if not state.get("jobs"):
        return
    parsed = parse_squeue_output(queue_snapshot)
    for job_id, metadata in state["jobs"].items():
        if job_id in parsed:
            metadata["status"] = parsed[job_id].get("state", metadata.get("status", ""))
            metadata["queue_time"] = parsed[job_id].get("time", "")
            metadata["last_seen_at"] = now_iso()
        elif metadata.get("status") in {"PD", "R", "submitted", "queued", "running", "pending"}:
            metadata["status"] = "not_in_queue"
            metadata["last_seen_at"] = now_iso()


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
    for item in state.get("plan", {}).get("items", [])[:5]:
        plan_lines.append(f"- [{item.get('status', 'pending')}] {item.get('step', '')}")
    if not plan_lines:
        plan_lines.append("- none")

    job_lines = []
    for job_id, metadata in list(state.get("jobs", {}).items())[:10]:
        job_lines.append(f"- `{job_id}`: {metadata.get('status', 'unknown')}")
    if not job_lines:
        job_lines.append("- none")

    return "\n".join(
        [
            f"## {SYSTEM_SECTION_PREFIX} Heartbeat",
            "",
            f"- Time: {now_iso()}",
            f"- Lifecycle status: {state['lifecycle'].get('status', '')}",
            f"- Worker status: {state['progress'].get('last_worker_status', '')}",
            f"- Pending inputs: {state.get('inputs', {}).get('pending', 0)}",
            f"- Active model: {state['supervisor'].get('active_model', '')}",
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
) -> tuple[dict[str, Any] | None, str]:
    response_path = runtime_dir / "outbox" / f"codex-response-{int(time.time())}.json"
    prompt = render_worker_prompt(runtime_dir, state)
    cmd = codex_command(runtime_dir, model, search_enabled, response_path, extra_config)
    result = capture_command(cmd, cwd=runtime_dir, stdin_text=prompt)
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
    if state["lifecycle"].get("stop_requested"):
        append_event(runtime_dir, "tick_skipped", {"reason": "stop_requested"})
        return 0

    state["supervisor"]["last_tick_at"] = now_iso()
    maybe_poll_lark_inputs(runtime_dir, state, args.disable_lark)
    queue_snapshot = maybe_capture_squeue(runtime_dir)
    if queue_snapshot:
        append_event(runtime_dir, "squeue_snapshot", {"lines": queue_snapshot.splitlines()[:20]})
        sync_jobs_with_squeue(state, queue_snapshot)

    models = [state["supervisor"]["active_model"]]
    fallback = state["supervisor"].get("fallback_model", "")
    if fallback and fallback not in models:
        models.append(fallback)

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
        state["progress"]["summary"] = "Codex tick failed."
        append_event(runtime_dir, "tick_failed", {"model": used_model, "output": last_output[-4000:]})
        save_state(runtime_dir, state)
        return 1

    merge_jobs(state, worker_result["jobs_submitted"])
    merge_plan(state, worker_result["plan_updates"])
    acknowledge_inputs(runtime_dir, state, worker_result["acknowledged_input_ids"], "Acknowledged by worker.")
    state["supervisor"]["consecutive_failures"] = 0
    state["progress"]["summary"] = worker_result["summary"]
    state["progress"]["last_worker_status"] = worker_result["status"]
    state["progress"]["artifacts_updated"] = worker_result["artifacts_updated"]
    state["progress"]["next_sleep_seconds"] = int(worker_result["next_sleep_seconds"])
    state["history"]["ticks_completed"] += 1
    record_summary(runtime_dir, worker_result)
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
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    ensure_runtime_layout(runtime_dir)

    state = default_state(
        mission_path,
        runtime_dir,
        title,
        extract_doc_urls(mission_text),
        seeded_plan_steps(mission_text),
    )
    if args.doc_url:
        urls = state["mission"]["doc_urls"]
        if args.doc_url not in urls:
            urls.append(args.doc_url)

    write_text(runtime_dir / "mission.md", mission_text)
    write_text(runtime_dir / "prompts" / "worker_prompt.md", render_worker_prompt(runtime_dir, state))
    write_text(runtime_dir / "runbook.md", render_runbook(state))
    write_text(runtime_dir / "notes" / "latest_summary.md", "# Latest Summary\n\nRuntime initialized.\n")
    save_state(runtime_dir, state)
    append_event(runtime_dir, "runtime_initialized", {"mission_path": str(mission_path), "runtime_dir": str(runtime_dir)})
    print(str(runtime_dir))
    return 0


def start_runtime(args: argparse.Namespace) -> int:
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    ensure_runtime_layout(runtime_dir)
    if not (runtime_dir / "state.json").exists():
        raise SystemExit(f"Runtime not initialized: {runtime_dir}")
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

        sleep_seconds = max(30, int(state["progress"].get("next_sleep_seconds", DEFAULT_INTERVAL_SECONDS)))
        state["supervisor"]["last_sleep_seconds"] = sleep_seconds
        save_state(runtime_dir, state)
        append_event(runtime_dir, "supervisor_sleep", {"seconds": sleep_seconds})
        time.sleep(sleep_seconds)


def print_status(args: argparse.Namespace) -> int:
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    state = load_state(runtime_dir)
    payload = {
        "runtime_dir": str(runtime_dir),
        "title": state["mission"]["title"],
        "status": state["lifecycle"]["status"],
        "last_tick_at": state["supervisor"]["last_tick_at"],
        "active_model": state["supervisor"]["active_model"],
        "next_sleep_seconds": state["progress"]["next_sleep_seconds"],
        "summary": state["progress"]["summary"],
        "jobs": list(state["jobs"].keys()),
        "plan": state.get("plan", {}).get("items", []),
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
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    state = load_state(runtime_dir)
    state["lifecycle"]["stop_requested"] = True
    state["lifecycle"]["stop_reason"] = args.reason
    save_state(runtime_dir, state)
    append_event(runtime_dir, "stop_requested", {"reason": args.reason})
    if not args.disable_lark:
        update_lark_doc(
            runtime_dir,
            state,
            f"## {SYSTEM_SECTION_PREFIX} Stopped\n\n- Time: {now_iso()}\n- Reason: {args.reason}\n",
        )
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
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    ensure_runtime_layout(runtime_dir)
    if not (runtime_dir / "state.json").exists():
        raise SystemExit(f"Runtime not initialized: {runtime_dir}")
    state = load_state(runtime_dir)
    queue_snapshot = maybe_capture_squeue(runtime_dir)
    if queue_snapshot:
        sync_jobs_with_squeue(state, queue_snapshot)
    save_state(runtime_dir, state)
    payload = list(state.get("jobs", {}).values())
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else "\n".join(f"{item['job_id']}: {item.get('status', '')}" for item in payload))
    return 0


def list_jobs_command(args: argparse.Namespace) -> int:
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    ensure_runtime_layout(runtime_dir)
    state = load_state(runtime_dir)
    payload = list(state.get("jobs", {}).values())
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else "\n".join(f"{item['job_id']}: {item.get('status', '')}" for item in payload))
    return 0


def add_input(args: argparse.Namespace) -> int:
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
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
    else:
        print(f"added input {item['id']}")
    return 0


def list_inputs(args: argparse.Namespace) -> int:
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
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
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    ensure_runtime_layout(runtime_dir)
    if not (runtime_dir / "state.json").exists():
        raise SystemExit(f"Runtime not initialized: {runtime_dir}")

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
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
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
    save_state(runtime_dir, state)
    append_event(runtime_dir, "daemon_stop_requested", {"pid": pid, "reason": args.reason})
    os.kill(pid, signal.SIGTERM)
    print(json.dumps({"runtime_dir": str(runtime_dir), "running": True, "pid": pid, "signal": "SIGTERM"}, ensure_ascii=False))
    return 0


def daemon_status(args: argparse.Namespace) -> int:
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    snapshot = daemon_snapshot(runtime_dir)
    state = load_state(runtime_dir) if (runtime_dir / "state.json").exists() else None
    payload = {
        "runtime_dir": str(runtime_dir),
        "daemon": snapshot,
        "lifecycle_status": state["lifecycle"]["status"] if state else "missing",
        "summary": state["progress"]["summary"] if state else "",
        "inputs": state.get("inputs", {}) if state else {},
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False) if args.json else "\n".join(f"{k}: {v}" for k, v in payload.items()))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Single-file autoresearch runtime for Codex.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create a runtime from one autoresearch markdown file.")
    init_parser.add_argument("mission", help="Path to autoresearch.md")
    init_parser.add_argument("--runtime-dir", required=True, help="Where to create the runtime state")
    init_parser.add_argument("--doc-url", default="", help="Optional Lark/Feishu doc URL to append updates to")
    init_parser.set_defaults(func=init_runtime)

    start_parser = subparsers.add_parser("start", help="Start or continue the supervisor loop.")
    start_parser.add_argument("runtime_dir", help="Runtime directory created by init")
    start_parser.add_argument("--once", action="store_true", help="Execute a single Codex tick")
    start_parser.add_argument("--search", action="store_true", help="Enable web search inside Codex")
    start_parser.add_argument("--disable-lark", action="store_true", help="Skip Lark document updates")
    start_parser.add_argument(
        "--codex-config",
        action="append",
        default=[],
        help="Extra 'key=value' values passed through to codex exec via -c",
    )
    start_parser.set_defaults(func=start_runtime)

    status_parser = subparsers.add_parser("status", help="Show runtime status.")
    status_parser.add_argument("runtime_dir", help="Runtime directory created by init")
    status_parser.add_argument("--json", action="store_true", help="Print JSON output")
    status_parser.set_defaults(func=print_status)

    stop_parser = subparsers.add_parser("stop", help="Request a graceful stop.")
    stop_parser.add_argument("runtime_dir", help="Runtime directory created by init")
    stop_parser.add_argument("--reason", default="manual stop", help="Reason to record in state and Lark")
    stop_parser.add_argument("--disable-lark", action="store_true", help="Skip Lark document updates")
    stop_parser.set_defaults(func=stop_runtime)

    add_input_parser = subparsers.add_parser("add-input", help="Add a persisted user input to the runtime inbox.")
    add_input_parser.add_argument("runtime_dir", help="Runtime directory created by init")
    add_input_parser.add_argument("--message", default="", help="Input content as a direct string")
    add_input_parser.add_argument("--file", default="", help="Read input content from a file")
    add_input_parser.add_argument("--source", default="manual", help="Input source label, such as manual or feishu")
    add_input_parser.add_argument("--title", default="", help="Optional short title")
    add_input_parser.add_argument("--author", default="user", help="Author label")
    add_input_parser.add_argument("--json", action="store_true", help="Print JSON output")
    add_input_parser.set_defaults(func=add_input)

    list_inputs_parser = subparsers.add_parser("list-inputs", help="List persisted runtime inputs.")
    list_inputs_parser.add_argument("runtime_dir", help="Runtime directory created by init")
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
    sync_jobs_parser.add_argument("runtime_dir", help="Runtime directory created by init")
    sync_jobs_parser.add_argument("--json", action="store_true", help="Print JSON output")
    sync_jobs_parser.set_defaults(func=sync_jobs_command)

    list_jobs_parser = subparsers.add_parser("list-jobs", help="List registered job metadata.")
    list_jobs_parser.add_argument("runtime_dir", help="Runtime directory created by init")
    list_jobs_parser.add_argument("--json", action="store_true", help="Print JSON output")
    list_jobs_parser.set_defaults(func=list_jobs_command)

    daemon_start_parser = subparsers.add_parser("daemon-start", help="Run the supervisor in the background.")
    daemon_start_parser.add_argument("runtime_dir", help="Runtime directory created by init")
    daemon_start_parser.add_argument("--search", action="store_true", help="Enable web search inside Codex")
    daemon_start_parser.add_argument("--disable-lark", action="store_true", help="Skip Lark document updates")
    daemon_start_parser.add_argument(
        "--codex-config",
        action="append",
        default=[],
        help="Extra 'key=value' values passed through to codex exec via -c",
    )
    daemon_start_parser.set_defaults(func=daemon_start)

    daemon_stop_parser = subparsers.add_parser("daemon-stop", help="Stop a background supervisor.")
    daemon_stop_parser.add_argument("runtime_dir", help="Runtime directory created by init")
    daemon_stop_parser.add_argument("--reason", default="manual stop", help="Reason to record in state and Lark")
    daemon_stop_parser.add_argument("--disable-lark", action="store_true", help="Skip Lark document updates")
    daemon_stop_parser.set_defaults(func=daemon_stop)

    daemon_status_parser = subparsers.add_parser("daemon-status", help="Show background supervisor status.")
    daemon_status_parser.add_argument("runtime_dir", help="Runtime directory created by init")
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
