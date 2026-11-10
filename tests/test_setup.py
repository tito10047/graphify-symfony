from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("graphify")

from graphify_symfony import hooks  # noqa: E402
from graphify_symfony.cli import main  # noqa: E402
from graphify_symfony.graph import ORIGIN  # noqa: E402

from .conftest import write_app  # noqa: E402

USER_SETTINGS = {
    "permissions": {"allow": ["Bash(ls:*)"]},
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo user-hook"}]},
            {"matcher": "Read|Glob", "hooks": [{"type": "command", "command": "graphify hook-guard read"}]},
        ]
    },
}


def _git(cwd: Path, *args) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                           "-c", "core.autocrlf=false", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout


def _snapshot(root: Path) -> dict[str, bytes]:
    out = {}
    for p in root.rglob("*"):
        rel = p.relative_to(root).as_posix()
        if p.is_file() and not rel.startswith((".git/objects", ".git/logs", "graphify-out/", "var/")) \
                and rel not in (".git/index", ".git/COMMIT_EDITMSG", ".git/ORIG_HEAD", ".git/config"):
            out[rel] = p.read_bytes()
    return out


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = write_app(tmp_path / "acme")
    (root / ".gitignore").write_bytes(b"/vendor/\r\n/var/")          # CRLF, no trailing newline
    (root / "CLAUDE.md").write_bytes(b"# Acme\r\n\r\nProject notes.\r\n")
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text(json.dumps(USER_SETTINGS, indent=4), encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def test_setup_is_idempotent_and_teardown_restores(repo: Path):
    before = _snapshot(repo)
    assert main(["setup", "--root", str(repo), "--no-build"]) == 0
    first = _snapshot(repo)

    gi = first[".gitignore"].decode()
    assert gi.startswith("/vendor/\r\n/var/\r\n# >>> graphify-symfony")
    assert "/graphify-out/\r\n/.claude/settings.local.json\r\n" in gi
    assert b"vendor/" in first[".graphifyignore"] and b"translations/" in first[".graphifyignore"]
    claude = first["CLAUDE.md"].decode()
    assert claude.startswith("# Acme\r\n") and "## graphify" in claude and "\n" not in claude.replace("\r\n", "")
    local = json.loads(first[".claude/settings.local.json"])
    cmds = [h["command"] for g in local["hooks"]["PreToolUse"] for h in g["hooks"]]
    assert any("hook-guard search" in c for c in cmds) and any("hook-guard read" in c for c in cmds)
    assert "SessionStart" in local["hooks"]
    shared = json.loads(first[".claude/settings.json"])
    shared_cmds = [h["command"] for g in shared["hooks"]["PreToolUse"] for h in g["hooks"]]
    assert shared_cmds == ["echo user-hook"]  # graphify's hook moved out, user's kept
    assert shared["permissions"] == USER_SETTINGS["permissions"]
    hook = repo / ".git" / "hooks" / "post-commit"
    assert hooks.MARK in hook.read_text(encoding="utf-8")
    assert b"\r\n" not in hook.read_bytes()
    assert ".gitattributes" not in first  # merge driver side effects undone
    r = subprocess.run(["git", "config", "--get", "merge.graphify.driver"], cwd=repo, capture_output=True)
    assert r.returncode != 0

    # what a teammate would see: only the three shareable files
    status = {ln[3:] for ln in _git(repo, "status", "--porcelain").splitlines()}
    assert status == {".gitignore", "CLAUDE.md", ".graphifyignore", ".claude/settings.json"}

    assert main(["setup", "--root", str(repo), "--no-build"]) == 0
    assert _snapshot(repo) == first

    assert main(["teardown", "--root", str(repo)]) == 0
    assert _snapshot(repo) == before


def test_teardown_after_user_edit_is_structural(repo: Path):
    assert main(["setup", "--root", str(repo), "--no-build", "--no-git-hooks", "--claude-hooks", "none"]) == 0
    gi = repo / ".gitignore"
    gi.write_bytes(gi.read_bytes() + b"/extra/\r\n")
    assert main(["teardown", "--root", str(repo)]) == 0
    assert gi.read_bytes() == b"/vendor/\r\n/var/\r\n/extra/\r\n"
    assert not (repo / ".graphifyignore").exists()
    assert (repo / "CLAUDE.md").read_bytes() == b"# Acme\r\n\r\nProject notes.\r\n"


def test_setup_builds_graph(repo: Path, monkeypatch):
    monkeypatch.setenv("GRAPHIFY_BACKEND", "none")
    assert main(["setup", "--root", str(repo), "--no-git-hooks", "--json"]) == 0
    g = json.loads((repo / "graphify-out" / "graph.json").read_text(encoding="utf-8"))
    assert any(n.get("_origin") == ORIGIN and n.get("sf_type") == "template" for n in g["nodes"])
    assert main(["teardown", "--root", str(repo), "--purge"]) == 0
    assert not (repo / "graphify-out").exists()


def _mtimes(root: Path) -> dict[str, int]:
    return {p.relative_to(root).as_posix(): p.stat().st_mtime_ns for p in root.rglob("*")
            if p.is_file() and not p.relative_to(root).as_posix().startswith(".git/objects")}


ROLLOUT = "# >>> graphify-rollout (managed) >>>\n/graphify-out/\n*.generated.php\n# <<< graphify-rollout <<<\n"


def test_rerun_touches_nothing_and_foreign_blocks_survive(repo: Path, monkeypatch):
    monkeypatch.setenv("GRAPHIFY_BACKEND", "none")
    for rel in (".gitignore", ".graphifyignore"):
        p = repo / rel
        p.write_bytes((p.read_bytes() + b"\r\n" if p.exists() else b"") + ROLLOUT.encode())
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "rollout block")
    before = _snapshot(repo)
    assert main(["setup", "--root", str(repo)]) == 0
    gi = (repo / ".gitignore").read_text(encoding="utf-8")
    assert ROLLOUT in gi
    assert "/graphify-out/" not in gi.split("# <<< graphify-rollout <<<")[1]  # not duplicated
    assert ROLLOUT in (repo / ".graphifyignore").read_text(encoding="utf-8")

    m1 = _mtimes(repo)
    assert main(["setup", "--root", str(repo)]) == 0
    assert _mtimes(repo) == m1

    # the rollout tool edits its own block afterwards; teardown removes only ours
    for rel in (".gitignore", ".graphifyignore"):
        p = repo / rel
        p.write_bytes(p.read_bytes().replace(b"*.generated.php", b"*.gen.php"))
    assert main(["teardown", "--root", str(repo), "--purge"]) == 0
    for rel in (".gitignore", ".graphifyignore"):
        expected = before[rel].decode().replace("*.generated.php", "*.gen.php")
        assert (repo / rel).read_bytes().decode() == expected


def test_setup_keeps_custom_graphify_section(repo: Path):
    custom = b"# Acme\r\n\r\n## graphify - code graph (use it first)\r\n\r\nOur own rules.\r\n"
    (repo / "CLAUDE.md").write_bytes(custom)
    assert main(["setup", "--root", str(repo), "--no-build", "--no-git-hooks", "--claude-hooks", "none"]) == 0
    assert (repo / "CLAUDE.md").read_bytes() == custom
    assert main(["teardown", "--root", str(repo)]) == 0
    assert (repo / "CLAUDE.md").read_bytes() == custom


def test_setup_no_claude_md(repo: Path):
    before = (repo / "CLAUDE.md").read_bytes()
    assert main(["setup", "--root", str(repo), "--no-build", "--no-git-hooks", "--claude-hooks", "none",
                 "--no-claude-md"]) == 0
    assert (repo / "CLAUDE.md").read_bytes() == before


def test_graphifyfoo_heading_is_not_a_custom_section():
    from graphify_symfony.setup import _has_custom_section

    assert _has_custom_section("## graphify — znalostný graf\n")
    assert not _has_custom_section("## graphify\n")
    assert not _has_custom_section("## graphifyish\n")
