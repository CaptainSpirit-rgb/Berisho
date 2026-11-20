import json
import subprocess

import pytest
from scenario import NOW, WATCH, build

from market_intel import config
from market_intel.delivery import analysis, digest, export


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    work = tmp_path / "work"
    git(tmp_path, "init", "-q", "-b", "main", str(work))
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "Test")
    (work / "README.md").write_text("hi\n")
    git(work, "add", "README.md")
    git(work, "commit", "-q", "-m", "init")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-q", "origin", "main")
    (work / "dirty.txt").write_text("uncommitted\n")  # must survive untouched
    return work, remote


def test_build_export_contents(con):
    build(con)
    settings = config.load_settings()
    d = digest.build_digest(con, WATCH, settings, now=NOW)
    data = export.build_export(con, d, analysis.rule_based(d, settings), settings)
    assert data["schema_version"] == 1 and data["data_as_of"] == "2026-11-19"
    assert data["regime"]["regime"] == "bear" and "news_headlines" in data
    assert "TODAY IN BRIEF" in data["digest_text"]
    files = export.export_files(data)
    assert set(files) == {"daily/2026-11-19.json", "latest.json"}
    assert json.loads(files["latest.json"])["watchlist"][1]["symbol"] == "NVDA"


def test_push_creates_branch_then_appends_without_touching_worktree(repo):
    work, remote = repo
    c1 = export.push_to_branch(work, {"daily/a.json": b"1", "latest.json": b"1"})
    assert git(remote, "rev-parse", "digest-data") == c1
    assert git(remote, "show", "digest-data:latest.json") == "1"
    c2 = export.push_to_branch(work, {"daily/b.json": b"2", "latest.json": b"2"})
    assert git(remote, "rev-parse", "digest-data^") == c1
    assert git(remote, "ls-tree", "-r", "--name-only", "digest-data").split() == [
        "daily/a.json",
        "daily/b.json",
        "latest.json",
    ]
    assert git(remote, "rev-parse", "main") == git(work, "rev-parse", "main")
    assert git(work, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert (work / "dirty.txt").read_text() == "uncommitted\n"
    assert "dirty.txt" in git(work, "status", "--porcelain")
    # same content again: no new commit
    assert export.push_to_branch(work, {"latest.json": b"2"}) == c2


def test_push_failure_raises(tmp_path):
    work = tmp_path / "w"
    git(tmp_path, "init", "-q", str(work))
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "Test")
    with pytest.raises(export.GitPushError):
        export.push_to_branch(work, {"latest.json": b"x"})  # no remote
