"""`graphify-symfony setup` / `teardown`: one-shot, idempotent per-project setup.

Every file we touch is recorded in ``graphify-out/.graphify-symfony-setup.json``
(original bytes + hash of what we wrote). Teardown restores the original bytes
exactly when the file is still what we wrote; if somebody edited it since, only
our managed part is removed structurally."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import hooks as hookmod

STATE_NAME = ".graphify-symfony-setup.json"
BLOCK_START = "# >>> graphify-symfony (managed block; `graphify-symfony teardown` removes it) >>>"
BLOCK_END = "# <<< graphify-symfony <<<"

GRAPHIFYIGNORE = [
    "vendor/",
    "var/",
    "node_modules/",
    "public/build/",
    "public/bundles/",
    "assets/vendor/",
    "translations/",
    "migrations/",
    "*.min.js",
    "composer.lock",
    "symfony.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "importmap.lock",
    ".claude/",
]
GITIGNORE_OUT = "/graphify-out/"
GITIGNORE_LOCAL = "/.claude/settings.local.json"
CLAUDE_MARKER = "## graphify"
GFY_ENV = {"GRAPHIFY_BACKEND": "none", "GRAPHIFY_QUERY_LOG_DISABLE": "1", "PYTHONHASHSEED": "0"}


# ------------------------------------------------------------------ helpers
def _sha(b: bytes | None) -> str | None:
    return None if b is None else hashlib.sha256(b).hexdigest()


def _decode(b: bytes) -> str:
    return b.decode("utf-8", errors="surrogateescape")


def _encode(s: str) -> bytes:
    return s.encode("utf-8", errors="surrogateescape")


def _nl(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _write_bytes(p: Path, data: bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".gfs-tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
    try:
        mode = p.stat().st_mode & 0o7777
        os.chmod(tmp, mode)
    except OSError:
        pass
    try:
        os.replace(tmp, p)
    except PermissionError:  # Windows: destination briefly open elsewhere
        time.sleep(0.2)
        os.replace(tmp, p)


class State:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / os.environ.get("GRAPHIFY_OUT", "graphify-out") / STATE_NAME
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("files", {})

    def save(self) -> None:
        data = (json.dumps(self.data, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if self.path.is_file() and self.path.read_bytes() == data:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _write_bytes(self.path, data)

    # -- file tracking -------------------------------------------------------
    def apply(self, rel: str, transform) -> str:
        """Run ``transform(text_or_None) -> text_or_None`` on ``rel`` and record it."""
        p = self.root / rel
        cur = p.read_bytes() if p.is_file() else None
        entry = self.data["files"].get(rel)
        if entry is None:
            entry = {"existed": cur is not None,
                     "original": base64.b64encode(cur).decode() if cur is not None else None,
                     "written": _sha(cur), "dirty": False}
        elif _sha(cur) != entry.get("written"):
            entry["dirty"] = True  # edited since our last write: never restore blindly
        new = transform(_decode(cur) if cur is not None else None)
        new_b = _encode(new) if new is not None else None
        if new_b == cur:
            status = "unchanged"
        elif new_b is None:
            p.unlink()
            status = "removed"
        else:
            _write_bytes(p, new_b)
            status = "created" if cur is None else "updated"
        entry["written"] = _sha(new_b)
        if status != "unchanged" or rel in self.data["files"]:
            self.data["files"][rel] = entry
        return status

    def restore(self, rel: str, structural) -> str:
        """Undo our changes to ``rel``: exact restore when untouched since,
        else ``structural(text) -> text_or_None``."""
        entry = self.data["files"].get(rel)
        p = self.root / rel
        cur = p.read_bytes() if p.is_file() else None
        if entry and not entry.get("dirty") and _sha(cur) == entry.get("written"):
            if entry.get("existed"):
                orig = base64.b64decode(entry["original"])
                if orig != cur:
                    _write_bytes(p, orig)
                    return "restored"
                return "unchanged"
            if cur is not None:
                p.unlink()
                return "deleted (was created by setup)"
            return "unchanged"
        if cur is None:
            return "absent"
        new = structural(_decode(cur))
        if new is None or (not new.strip() and entry is not None and not entry.get("existed")):
            p.unlink()
            return "deleted"
        nb = _encode(new)
        if nb != cur:
            _write_bytes(p, nb)
            return "cleaned"
        return "unchanged"


# ------------------------------------------------------------ managed blocks
def _strip_block(text: str) -> str:
    lines = text.splitlines(keepends=True)
    out, inside = [], False
    for ln in lines:
        s = ln.strip()
        if s == BLOCK_START:
            inside = True
            continue
        if inside and s == BLOCK_END:
            inside = False
            continue
        if not inside:
            out.append(ln)
    return "".join(out)


def _with_block(text: str | None, entries: list[str]) -> str | None:
    base = _strip_block(text or "")
    nl = _nl(text or "")
    if not entries:
        return base if text is not None else None
    block = nl.join([BLOCK_START, *entries, BLOCK_END]) + nl
    if base and not base.endswith(("\n", "\r")):
        base += nl
    return base + block


def _present(text: str | None, variants: set[str]) -> bool:
    if not text:
        return False
    body = _strip_block(text)
    return any(ln.strip() in variants for ln in body.splitlines())


def _gitignore_entries(text: str | None, claude_local: bool) -> list[str]:
    out = []
    if not _present(text, {"graphify-out", "graphify-out/", "/graphify-out", "/graphify-out/",
                           "graphify-out/*", "/graphify-out/*", "graphify-out/**"}):
        out.append(GITIGNORE_OUT)
    if claude_local and not _present(text, {".claude/settings.local.json", "/.claude/settings.local.json",
                                            ".claude/", "/.claude/", ".claude", "/.claude", "*.local.json"}):
        out.append(GITIGNORE_LOCAL)
    return out


# ---------------------------------------------------------------- CLAUDE.md
def _gfy_install():
    try:
        from graphify import install as gi  # type: ignore
        return gi
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(f"graphify is not importable from this Python ({exc}); "
                           "install graphify-symfony into graphify's uv tool env") from exc


def _claude_md(text: str | None) -> str:
    gi = _gfy_install()
    section = gi._always_on("claude-md")
    if text is None:
        return section
    nl = _nl(text)
    norm = text.replace("\r\n", "\n")
    new = gi._replace_or_append_section(norm, CLAUDE_MARKER, section)
    return new.replace("\n", nl) if nl != "\n" else new


def _claude_md_remove(text: str) -> str | None:
    gi = _gfy_install()
    nl = _nl(text)
    new = gi._remove_marker_section(text.replace("\r\n", "\n"), CLAUDE_MARKER)
    if new is None:
        return text
    return new.replace("\n", nl) if nl != "\n" else new


# ------------------------------------------------------- Claude Code hooks
def _load_settings(text: str | None) -> dict:
    if not text or not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("settings file is not a JSON object")
    return data


def _dump_settings(data: dict) -> str:
    return json.dumps(data, indent=2) + "\n"


def _strip_gfy_hooks(data: dict) -> tuple[dict, bool]:
    gi = _gfy_install()
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return data, False
    changed = False
    for ev in ("PreToolUse", "SessionStart"):
        groups = hooks.get(ev)
        if isinstance(groups, list):
            kept = gi._strip_graphify_hooks(groups)
            if kept != groups:
                changed = True
                if kept:
                    hooks[ev] = kept
                else:
                    del hooks[ev]
    if changed and not hooks:
        del data["hooks"]
    return data, changed


def _add_gfy_hooks(data: dict, project: bool) -> dict:
    gi = _gfy_install()
    data, _ = _strip_gfy_hooks(data)
    hooks = data.setdefault("hooks", {})
    hooks.setdefault("PreToolUse", []).extend(gi._claude_pretooluse_hooks(project=project))
    hooks.setdefault("SessionStart", []).extend(gi._claude_sessionstart_hooks(project=project))
    return data


def _settings_add(project: bool):
    def tf(text):
        new = _add_gfy_hooks(_load_settings(text), project)
        out = _dump_settings(new)
        if text is not None and _load_settings(text) == new:
            return text  # semantically identical: keep the user's formatting
        return out
    return tf


def _settings_strip(text):
    if text is None:
        return None
    data, changed = _strip_gfy_hooks(_load_settings(text))
    return _dump_settings(data) if changed else text


# --------------------------------------------------------------- git bits
def _git(root: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def _is_git(root: Path) -> bool:
    r = _git(root, "rev-parse", "--is-inside-work-tree")
    return r.returncode == 0 and r.stdout.strip() == "true"


def _head(root: Path) -> str | None:
    r = _git(root, "rev-parse", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else None


def _graphify(root: Path, *args, timeout=3600) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "graphify", *args], cwd=root,
                          env={**os.environ, **GFY_ENV}, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def _hook_rel(root: Path, name: str) -> str:
    hd = hookmod.hooks_dir(root)
    try:
        return hd.resolve().relative_to(root.resolve()).joinpath(name).as_posix()
    except ValueError:
        return (hd / name).as_posix()  # outside the work tree (core.hooksPath)


# -------------------------------------------------------------- commands
def setup(root: Path, claude_hooks: str = "local", git_hooks: bool = True, build: bool = True,
          log=print) -> dict:
    root = root.resolve()
    st = State(root)
    st.data["claude_hooks"] = claude_hooks
    res: dict = {"root": str(root), "files": {}}

    res["files"][".graphifyignore"] = st.apply(".graphifyignore", lambda t: _with_block(t, GRAPHIFYIGNORE))
    res["files"][".gitignore"] = st.apply(
        ".gitignore", lambda t: _with_block(t, _gitignore_entries(t, claude_hooks == "local")))
    res["files"]["CLAUDE.md"] = st.apply("CLAUDE.md", _claude_md)
    if claude_hooks == "local":
        res["files"][".claude/settings.local.json"] = st.apply(".claude/settings.local.json", _settings_add(False))
        if (root / ".claude" / "settings.json").is_file():
            res["files"][".claude/settings.json"] = st.apply(".claude/settings.json", _settings_strip)
    elif claude_hooks == "shared":
        res["files"][".claude/settings.json"] = st.apply(".claude/settings.json", _settings_add(True))
        if (root / ".claude" / "settings.local.json").is_file():
            res["files"][".claude/settings.local.json"] = st.apply(".claude/settings.local.json", _settings_strip)
    st.save()

    if git_hooks:
        if not _is_git(root):
            res["git_hooks"] = "skipped (not a git repository)"
        else:
            gh = st.data.setdefault("git_hooks", {})
            if "graphify_before" not in gh:
                text = ""
                p = hookmod.hooks_dir(root) / "post-commit"
                if p.is_file():
                    text = p.read_text(encoding="utf-8", errors="replace")
                gh["graphify_before"] = hookmod.GRAPHIFY_START["post-commit"] in text
                gh["merge_config_before"] = {k: _git(root, "config", "--get", k).stdout.strip() or None
                                             for k in ("merge.graphify.name", "merge.graphify.driver")}
            rels = [_hook_rel(root, h) for h in hookmod.HOOKS]
            for rel in rels:  # snapshot originals before anything writes them
                if rel not in st.data["files"]:
                    p = root / rel
                    cur = p.read_bytes() if p.is_file() else None
                    st.data["files"][rel] = {"existed": cur is not None,
                                             "original": base64.b64encode(cur).decode() if cur is not None else None,
                                             "written": _sha(cur), "dirty": False}
            _status_text, hooks_ok = hookmod.status(root)
        if _is_git(root) and hooks_ok:
            res["git_hooks"] = "already installed (unchanged)"
        elif _is_git(root):
            gh = st.data["git_hooks"]
            rels = [_hook_rel(root, h) for h in hookmod.HOOKS]
            attrs_before = (root / ".gitattributes").read_bytes() if (root / ".gitattributes").is_file() else None
            out = hookmod.install(root)
            # graph.json is never committed, so graphify's merge driver cannot apply:
            # keep .gitattributes / git config exactly as they were
            if attrs_before is None:
                if (root / ".gitattributes").is_file():
                    (root / ".gitattributes").unlink()
            elif (root / ".gitattributes").read_bytes() != attrs_before:
                _write_bytes(root / ".gitattributes", attrs_before)
            for k, v in gh["merge_config_before"].items():
                if v is None:
                    _git(root, "config", "--unset", k)
            for rel in rels:
                p = root / rel
                entry = st.data["files"][rel]
                entry["written"] = _sha(p.read_bytes() if p.is_file() else None)
            res["git_hooks"] = out
            st.save()

    if build:
        gpath = root / os.environ.get("GRAPHIFY_OUT", "graphify-out") / "graph.json"
        built_at = None
        if gpath.is_file():
            try:
                built_at = json.loads(gpath.read_text(encoding="utf-8")).get("built_at_commit")
            except (OSError, ValueError):
                built_at = None
        head = _head(root)
        if gpath.is_file() and head and built_at == head:
            res["build"] = "graph already at HEAD"
        else:
            t = time.perf_counter()
            log("[graphify-symfony] building the code graph (graphify extract --code-only) ...")
            r = _graphify(root, "extract", ".", "--code-only")
            if not gpath.is_file():
                res["build"] = f"graphify extract failed (exit {r.returncode}): {r.stderr.strip()[-500:]}"
                res["ok"] = False
                return res
            r2 = _graphify(root, "cluster-only", ".", "--no-label")
            res["build"] = f"built in {time.perf_counter() - t:.1f}s" + (
                "" if r2.returncode == 0 else f" (cluster-only exit {r2.returncode})")
        import contextlib

        from .cli import main as cli_main

        with contextlib.redirect_stdout(sys.stderr):
            res["post_pass_exit"] = cli_main(["run", "--root", str(root)])
    res["ok"] = res.get("ok", True)
    return res


def teardown(root: Path, purge: bool = False, log=print) -> dict:
    root = root.resolve()
    st = State(root)
    res: dict = {"root": str(root), "files": {}}
    if not st.data["files"]:
        # no setup state (graphify-out was deleted?): remove only what is clearly ours
        for rel in (".graphifyignore", ".gitignore"):
            st.data["files"][rel] = {"existed": True, "dirty": True}
        st.data["files"][".claude/settings.local.json"] = {"existed": True, "dirty": True}
        st.data["claude_hooks"] = "local"
        if _is_git(root):
            for h in hookmod.HOOKS:
                st.data["files"][_hook_rel(root, h)] = {"existed": True, "dirty": True}
            st.data["git_hooks"] = {"graphify_before": True}
    gh = st.data.get("git_hooks")
    if gh is not None and _is_git(root):
        rels = [_hook_rel(root, h) for h in hookmod.HOOKS]

        def strip_hooks(text: str) -> str | None:
            text = hookmod._strip_block(text)
            if not gh.get("graphify_before"):
                try:
                    from graphify.hooks import (_CHECKOUT_MARKER, _CHECKOUT_MARKER_END, _HOOK_MARKER,  # type: ignore
                                                _HOOK_MARKER_END, _MERGE_HOOK_MARKER, _MERGE_HOOK_MARKER_END,
                                                _REWRITE_HOOK_MARKER, _REWRITE_HOOK_MARKER_END)
                    import re

                    for a, b in ((_HOOK_MARKER, _HOOK_MARKER_END), (_CHECKOUT_MARKER, _CHECKOUT_MARKER_END),
                                 (_MERGE_HOOK_MARKER, _MERGE_HOOK_MARKER_END),
                                 (_REWRITE_HOOK_MARKER, _REWRITE_HOOK_MARKER_END)):
                        text = re.sub(rf"{re.escape(a)}.*?{re.escape(b)}\n?", "", text, flags=re.S)
                except Exception:
                    pass
            return None if text.strip() in ("", "#!/bin/sh", "#!/usr/bin/env sh", "#!/bin/bash") else text

        for rel in rels:
            if rel in st.data["files"]:
                res["files"][rel] = st.restore(rel, strip_hooks)
        if not gh.get("graphify_before"):
            for k, v in (gh.get("merge_config_before") or {}).items():
                if v is None:
                    _git(root, "config", "--unset", k)
    if (st.data.get("files") or {}).get(".claude/settings.local.json") is not None:
        res["files"][".claude/settings.local.json"] = st.restore(".claude/settings.local.json", _settings_strip)
    if (st.data.get("files") or {}).get(".claude/settings.json") is not None:
        mode = st.data.get("claude_hooks")
        res["files"][".claude/settings.json"] = st.restore(
            ".claude/settings.json", _settings_strip if mode == "shared" else (lambda t: t))
    for rel, structural in (("CLAUDE.md", _claude_md_remove), (".gitignore", _strip_block),
                            (".graphifyignore", _strip_block)):
        if rel in st.data.get("files", {}):
            entry = st.data["files"][rel]
            if rel == "CLAUDE.md" and entry.get("existed") and entry.get("original") is not None and \
                    CLAUDE_MARKER in _decode(base64.b64decode(entry["original"])):
                structural = (lambda t: t)  # the section predates setup: leave it
            res["files"][rel] = st.restore(rel, structural)
    claude_dir = root / ".claude"
    if claude_dir.is_dir() and not any(claude_dir.iterdir()):
        claude_dir.rmdir()
    out = st.path.parent
    if purge:
        if out.is_dir() and out.name and out.resolve() != root:
            shutil.rmtree(out)
            res["purged"] = str(out)
    else:
        from .cli import main as cli_main

        if (out / "graph.json").is_file():
            import contextlib

            with contextlib.redirect_stdout(sys.stderr):
                cli_main(["strip", "--root", str(root)])
        if st.path.is_file():
            st.path.unlink()
    return res
