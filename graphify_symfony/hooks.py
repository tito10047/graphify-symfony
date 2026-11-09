"""Git hook chaining: graphify's own hook block rebuilds graph.json in the
background; our marked block, placed right after it, launches a detached
`graphify-symfony run --after-graphify` that waits for that rebuild (via
graphify's .rebuild.lock) and then re-applies the Symfony post-pass.

The block lives between its own markers, so re-running `graphify hook install`
(which rewrites only the text between *its* markers) leaves it alone, and
pre-existing user hook content is never touched."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

MARK = "# graphify-symfony-hook-start"
MARK_END = "# graphify-symfony-hook-end"
HOOKS = ("post-commit", "post-checkout", "post-merge", "post-rewrite")
GRAPHIFY_END = {
    "post-commit": "# graphify-hook-end",
    "post-checkout": "# graphify-checkout-hook-end",
    "post-merge": "# graphify-merge-hook-end",
    "post-rewrite": "# graphify-rewrite-hook-end",
}
GRAPHIFY_START = {
    "post-commit": "# graphify-hook-start",
    "post-checkout": "# graphify-checkout-hook-start",
    "post-merge": "# graphify-merge-hook-start",
    "post-rewrite": "# graphify-rewrite-hook-start",
}

_GUARDS = {
    "post-commit": """\
GIT_DIR=${GIT_DIR:-$(git rev-parse --git-dir 2>/dev/null)}
[ -d "$GIT_DIR/rebase-merge" ] && exit 0
[ -d "$GIT_DIR/rebase-apply" ] && exit 0
[ -f "$GIT_DIR/MERGE_HEAD" ] && exit 0
""",
    "post-checkout": """\
[ "${3:-0}" = "1" ] || exit 0
[ "${1:-}" = "${2:-}" ] && exit 0
GIT_DIR=${GIT_DIR:-$(git rev-parse --git-dir 2>/dev/null)}
[ -d "$GIT_DIR/rebase-merge" ] && exit 0
[ -d "$GIT_DIR/rebase-apply" ] && exit 0
[ -f "$GIT_DIR/MERGE_HEAD" ] && exit 0
[ -f "$GIT_DIR/CHERRY_PICK_HEAD" ] && exit 0
""",
    "post-merge": "",
    "post-rewrite": """\
[ -t 0 ] || cat >/dev/null
[ "${1:-}" = "rebase" ] || exit 0
""",
}

_BLOCK = """\
# graphify-symfony-hook-start
# Re-applies the Symfony post-pass after graphify's background rebuild.
# Installed by: graphify-symfony hook install
(
[ "${GRAPHIFY_SKIP_HOOK:-0}" = "1" ] && exit 0
[ "${GRAPHIFY_SYMFONY_SKIP_HOOK:-0}" = "1" ] && exit 0
__GUARD___GFS_GITDIR=$(CDPATH= cd "$(git rev-parse --git-dir 2>/dev/null)" 2>/dev/null && pwd)
_GFS_COMMONDIR=$(CDPATH= cd "$(git rev-parse --git-common-dir 2>/dev/null)" 2>/dev/null && pwd)
if [ -n "$_GFS_COMMONDIR" ] && [ "$_GFS_GITDIR" != "$_GFS_COMMONDIR" ]; then
    exit 0
fi
_GFS_TOP=$(git rev-parse --show-toplevel 2>/dev/null)
[ -n "$_GFS_TOP" ] && cd "$_GFS_TOP" || exit 0
_GFS_OUT="${GRAPHIFY_OUT:-graphify-out}"
[ -f "${_GFS_OUT%/}/graph.json" ] || exit 0
_GFS_LOG="${GRAPHIFY_SYMFONY_LOG:-$HOME/.cache/graphify-symfony.log}"
mkdir -p "$(dirname "$_GFS_LOG")" 2>/dev/null
_GFS_PY='__PINNED_PYTHON__'
if [ -x "$_GFS_PY" ] && "$_GFS_PY" -c "import graphify_symfony" 2>/dev/null; then
    "$_GFS_PY" -c 'import os, subprocess, sys
log = open(sys.argv[1], "a", buffering=1, encoding="utf-8", errors="replace")
cmd = [sys.executable, "-m", "graphify_symfony", "run", "--root", os.getcwd(), "--after-graphify"]
kw = dict(stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, close_fds=True)
if os.name == "nt":
    flags = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(cmd, creationflags=flags | 0x01000000, **kw)  # + CREATE_BREAKAWAY_FROM_JOB
    except OSError:
        subprocess.Popen(cmd, creationflags=flags, **kw)
else:
    subprocess.Popen(cmd, start_new_session=True, **kw)' "$_GFS_LOG"
elif command -v graphify-symfony >/dev/null 2>&1; then
    if command -v nohup >/dev/null 2>&1; then
        nohup graphify-symfony run --root "$PWD" --after-graphify </dev/null >>"$_GFS_LOG" 2>&1 &
    else
        graphify-symfony run --root "$PWD" --after-graphify </dev/null >>"$_GFS_LOG" 2>&1 &
    fi
else
    echo "[graphify-symfony hook] graphify-symfony not found; re-run 'graphify-symfony hook install'" >&2
fi
)
# graphify-symfony-hook-end
"""


def _write_lf(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _read(path: Path) -> str:
    with open(path, encoding="utf-8", errors="replace", newline="") as fh:
        return fh.read().replace("\r\n", "\n")


def render_block(hook: str, python: str | None = None) -> str:
    py = (python or sys.executable).replace("\\", "/")  # C:/... works in Git for Windows' sh and in Windows
    if not re.fullmatch(r"[\w/.@:+ \-]+", py):
        py = ""
    return _BLOCK.replace("__GUARD__", _GUARDS[hook]).replace("__PINNED_PYTHON__", py)


def hooks_dir(root: Path) -> Path:
    try:
        from graphify.hooks import _hooks_dir  # type: ignore

        return Path(_hooks_dir(root))
    except Exception:
        out = subprocess.run(["git", "rev-parse", "--git-path", "hooks"], cwd=root, capture_output=True, text=True)
        if out.returncode != 0:
            raise RuntimeError(f"{root} is not inside a git repository")
        p = Path(out.stdout.strip())
        return p if p.is_absolute() else (root / p)


def _strip_block(text: str) -> str:
    return re.sub(rf"\n?{re.escape(MARK)}.*?{re.escape(MARK_END)}\n?", "\n", text, flags=re.S)


def install_block(path: Path, hook: str, python: str | None = None) -> str:
    block = render_block(hook, python)
    text = _read(path) if path.exists() else ""
    had = MARK in text
    if had:
        text = _strip_block(text)
    if not text.strip():
        text = "#!/bin/sh\n"
    elif not text.startswith("#!"):
        text = "#!/bin/sh\n" + text
    end = GRAPHIFY_END[hook]
    idx = text.find(end)
    if idx != -1:
        cut = idx + len(end)
        if cut < len(text) and text[cut] == "\n":
            cut += 1
        text = text[:cut] + block + text[cut:]
    else:
        if not text.endswith("\n"):
            text += "\n"
        text += block
    text = re.sub(r"\n{3,}", "\n\n", text)
    _write_lf(path, text)
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return "updated" if had else "installed"


def uninstall_block(path: Path) -> str:
    if not path.exists():
        return "not installed"
    text = _read(path)
    if MARK not in text:
        return "not installed"
    text = _strip_block(text)
    if text.strip() in ("", "#!/bin/sh", "#!/bin/bash", "#!/usr/bin/env sh"):
        path.unlink()
        return "removed (hook file deleted)"
    _write_lf(path, re.sub(r"\n{3,}", "\n\n", text))
    return "removed"


def install(root: Path, with_graphify: bool = True) -> str:
    root = root.resolve()
    lines = []
    if with_graphify:
        try:
            from graphify.hooks import install as gfy_install  # type: ignore

            lines.append("graphify: " + gfy_install(root).replace("\n", "\n  "))
        except Exception as exc:
            r = subprocess.run(["graphify", "hook", "install"], cwd=root, capture_output=True, text=True)
            if r.returncode != 0:
                lines.append(f"graphify hook install failed ({exc}); {r.stderr.strip()}")
            else:
                lines.append("graphify: " + r.stdout.strip())
    hd = hooks_dir(root)
    hd.mkdir(parents=True, exist_ok=True)
    for hook in HOOKS:
        lines.append(f"{hook}: {install_block(hd / hook, hook)}")
    return "\n".join(lines)


def uninstall(root: Path) -> str:
    hd = hooks_dir(root.resolve())
    return "\n".join(f"{h}: {uninstall_block(hd / h)}" for h in HOOKS) + \
        "\n(graphify's own hooks were left alone; remove them with `graphify hook uninstall`)"


def status(root: Path) -> tuple[str, bool]:
    hd = hooks_dir(root.resolve())
    ok = True
    lines = []
    for hook in HOOKS:
        p = hd / hook
        text = _read(p) if p.exists() else ""
        ours = MARK in text
        gfy = GRAPHIFY_START[hook] in text
        order = ""
        if ours and gfy:
            order = ", after graphify" if text.find(MARK) > text.find(GRAPHIFY_START[hook]) else ", BEFORE graphify"
        stale = ""
        if ours:
            cur = re.search(rf"{re.escape(MARK)}.*?{re.escape(MARK_END)}", text, re.S).group(0)
            pinned = re.search(r"_GFS_PY='([^']*)'", cur)
            if cur.strip() != render_block(hook, pinned.group(1) if pinned else None).strip():
                stale = " (outdated block, re-run hook install)"
        exe = p.exists() and os.access(p, os.X_OK)
        lines.append(f"{hook}: graphify-symfony {'installed' if ours else 'missing'}{order}{stale}; "
                     f"graphify {'installed' if gfy else 'missing'}{'' if exe or not p.exists() else '; NOT executable'}")
        ok = ok and ours and gfy and not stale
    return "\n".join(lines), ok
