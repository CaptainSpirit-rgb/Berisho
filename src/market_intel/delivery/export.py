"""Daily data file for the scheduled Claude analysis.

The cloud can't reach Yahoo or SEC, so the PC run does all the collecting and then
publishes one JSON file per day: the numbers, filings (with text excerpts), headlines,
insider trades and the digest text that went to Telegram. The file is committed to a
separate branch (``digest-data`` by default) of the repo and pushed, without touching
the working tree or the checked-out branch: git plumbing with a temporary index.

The scheduled Claude run reads ``latest.json`` (and ``daily/<date>.json``) from that
branch and writes the analysis.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import duckdb

from market_intel.delivery.analysis import Analysis
from market_intel.delivery.digest import Digest
from market_intel.delivery.render import render_text

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def build_export(
    con: duckdb.DuckDBPyConnection, d: Digest, a: Analysis, settings: dict[str, Any]
) -> dict[str, Any]:
    from market_intel.llm.briefs import build_payload

    data = build_payload(con, d, settings)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": d.generated_at.astimezone(dt.UTC).isoformat(timespec="seconds"),
        "display_timezone": d.display_tz.key,
        **data,
        # FinBERT sentiment vs each stock's own baseline (thesis model), when scored.
        "sentiment": [{"symbol": sym, "note": line} for sym, line in d.sentiment],
        "digest_text": render_text(d, a),
    }


def export_files(data: dict[str, Any]) -> dict[str, bytes]:
    """Paths (inside the branch) -> file contents."""
    blob = (json.dumps(data, indent=1, default=str, ensure_ascii=False) + "\n").encode()
    day = data.get("data_as_of") or data["generated_at_utc"][:10]
    return {f"daily/{day}.json": blob, "latest.json": blob}


class GitPushError(RuntimeError):
    pass


def _git(repo: Path, *args: str, env: dict | None = None, input_: bytes | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        input=input_,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip()
        raise GitPushError(f"git {args[0]} failed: {err}")
    return proc.stdout.decode().strip()


def push_to_branch(
    repo: Path,
    files: dict[str, bytes],
    branch: str = "digest-data",
    message: str = "Daily digest data",
    remote: str = "origin",
) -> str:
    """Commit ``files`` on top of ``remote/branch`` (or as its first commit) and push.
    The working tree, the index and the current branch are left untouched. Returns the
    new commit id."""
    remote_ref = f"refs/remotes/{remote}/{branch}"
    try:
        _git(repo, "fetch", "-q", remote, f"+refs/heads/{branch}:{remote_ref}")
        parent = _git(repo, "rev-parse", "--verify", "-q", remote_ref)
    except GitPushError:
        parent = ""  # branch doesn't exist yet: first commit
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if parent:
            _git(repo, "read-tree", parent, env=env)
        else:
            _git(repo, "read-tree", "--empty", env=env)
        for path, content in files.items():
            sha = _git(repo, "hash-object", "-w", "--stdin", input_=content)
            _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{sha},{path}", env=env)
        tree = _git(repo, "write-tree", env=env)
    if parent and _git(repo, "rev-parse", f"{parent}^{{tree}}") == tree:
        log.info("Digest data unchanged; nothing to push")
        return parent
    commit_args = ["commit-tree", tree, "-m", message] + (["-p", parent] if parent else [])
    commit = _git(repo, *commit_args)
    _git(repo, "push", "-q", remote, f"{commit}:refs/heads/{branch}")
    return commit
