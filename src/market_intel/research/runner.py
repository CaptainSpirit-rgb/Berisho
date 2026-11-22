"""Run the research agents with Claude Code, headless, under the Claude subscription.

``claude -p`` starts a Claude Code session without a window. Its main session is the
Chief Intelligence Officer (instructions in ``.claude/commands/research.md``) and it
calls the specialist subagents defined in ``.claude/agents/``. Nothing here calls the
Claude API, so with Claude Code logged in to Luka's subscription the run is free (it
counts toward the plan's usage limits).

Safety: ``ANTHROPIC_API_KEY`` is removed from the session's environment, because Claude
Code bills the API instead of the subscription when that variable is set. Tools are
limited to reading files, calling subagents and writing inside the run's ``reports/``
folder.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PAID_ENV_VARS = ("ANTHROPIC_API_KEY",)


@dataclass
class AgentRun:
    ok: bool
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def allowed_tools(rel_run_dir: str) -> list[str]:
    reports = f"./{rel_run_dir}/reports/**"
    return ["Read", "Glob", "Grep", "Task", "Agent", f"Write({reports})", f"Edit({reports})"]


def build_command(exe: str, rel_run_dir: str, cfg: dict[str, Any]) -> list[str]:
    prompt = (
        "You are the Chief Intelligence Officer of the Berisho research desk. Read "
        ".claude/commands/research.md and follow it exactly for the run directory "
        f"{rel_run_dir}."
    )
    cmd = [
        exe,
        "-p",
        prompt,
        "--allowedTools",
        ",".join(allowed_tools(rel_run_dir)),
        "--output-format",
        "json",
        "--max-turns",
        str(int(cfg.get("max_turns", 80))),
    ]
    if cfg.get("model"):
        cmd += ["--model", str(cfg["model"])]
    return cmd


def clean_env(env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if env is None else env)
    for var in PAID_ENV_VARS:
        if env.pop(var, None):
            log.info(
                "Removed %s from the agents' environment (keeps the run on the "
                "subscription, not the paid API)",
                var,
            )
    return env


def run_agents(run_dir: Path, repo_root: Path, cfg: dict[str, Any]) -> AgentRun:
    exe = shutil.which(str(cfg.get("claude_command", "claude")))
    if not exe:
        return AgentRun(False, "Claude Code (the `claude` command) isn't installed or not on PATH")
    try:
        rel = run_dir.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return AgentRun(False, f"run directory {run_dir} must be inside the repo")
    cmd = build_command(exe, rel, cfg)
    timeout = int(float(cfg.get("timeout_minutes", 20)) * 60)
    log.info("Starting the research agents (Claude Code, up to %d min)", timeout // 60)
    try:
        proc = subprocess.run(
            cmd,
            cwd=repo_root,
            env=clean_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return AgentRun(False, f"the agents took longer than {timeout // 60} minutes")
    (run_dir / "claude_output.json").write_text(proc.stdout or "", encoding="utf-8")
    if proc.stderr:
        (run_dir / "claude_stderr.txt").write_text(proc.stderr, encoding="utf-8")
    meta: dict[str, Any] = {}
    try:
        out = json.loads(proc.stdout or "{}")
        meta = {
            k: out.get(k)
            for k in ("num_turns", "duration_ms", "is_error", "subtype", "total_cost_usd")
        }
    except ValueError:
        out = {}
    if meta.get("total_cost_usd") is not None:
        log.info(
            "Agents finished: %s turns, %.1f min. Claude Code reports an API-equivalent "
            "cost of $%.2f; on the subscription this isn't billed.",
            meta.get("num_turns"),
            (meta.get("duration_ms") or 0) / 60000,
            meta["total_cost_usd"],
        )
    if proc.returncode != 0 or out.get("is_error"):
        text = (out.get("result") or proc.stderr or "").strip()[:300]
        if "limit" in text.lower():
            return AgentRun(False, f"Claude usage limit reached: {text}", meta)
        return AgentRun(False, f"Claude Code exited with an error: {text or proc.returncode}", meta)
    return AgentRun(True, "", meta)
