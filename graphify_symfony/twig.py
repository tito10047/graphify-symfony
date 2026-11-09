"""Twig template lexing (regex) and template-name resolution."""

from __future__ import annotations

import bisect
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

COMMENT_RE = re.compile(r"\{#.*?#\}", re.S)
VERBATIM_RE = re.compile(r"\{%-?\s*verbatim\s*-?%\}.*?\{%-?\s*endverbatim\s*-?%\}", re.S)
TAG_RE = re.compile(r"\{%[-~]?\s*(\w+)\b(.*?)[-~]?%\}", re.S)
STR_RE = re.compile(r"""'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)\"""")
FUNC_RE = re.compile(
    r"\b(include|source|component|block|stimulus_controller|stimulus_action|stimulus_target)\s*\(\s*"
    r"(\[[^\]]*\]|'[^']*'|\"[^\"]*\"|[\w.]+)"
    r"(?:\s*,\s*('[^']*'|\"[^\"]*\"))?"
)
TWIG_COMPONENT_RE = re.compile(r"<twig:([A-Za-z_][\w:.\-]*)")
DATA_CONTROLLER_RE = re.compile(r"""data-controller\s*=\s*(["'])(.*?)\1""", re.S)
CUT_RE = {
    "include": re.compile(r"\s(?:with|only|ignore\s+missing)\b"),
    "embed": re.compile(r"\s(?:with|only|ignore\s+missing)\b"),
    "import": re.compile(r"\sas\b"),
    "from": re.compile(r"\simport\b"),
    "use": re.compile(r"\swith\b"),
    "extends": None,
    "form_theme": re.compile(r"\sonly\b"),
}


def _blank(m: re.Match) -> str:
    return re.sub(r"[^\n]", " ", m.group(0))


def _strings(expr: str) -> list[str]:
    return [a if a is not None else b for a, b in STR_RE.findall(expr)]


def _is_template_name(s: str) -> bool:
    return bool(s) and (s.endswith(".twig") or s.startswith("@")) and not s.endswith("/") and "{" not in s and "~" not in s


@dataclass
class TwigRef:
    kind: str          # extends|include|embed|import|use|form_theme|source|block_ref
    names: list[str]
    line: int
    dynamic: bool = False
    block: str | None = None


@dataclass
class TwigFile:
    refs: list[TwigRef] = field(default_factory=list)
    blocks: list[tuple[str, int]] = field(default_factory=list)
    components: list[tuple[str, int, str]] = field(default_factory=list)  # name, line, syntax
    stimulus: list[tuple[str, int]] = field(default_factory=list)  # controller name ("{{controller}}" = own)
    dynamic_refs: int = 0


def parse_twig(text: str) -> TwigFile:
    text = VERBATIM_RE.sub(_blank, COMMENT_RE.sub(_blank, text))
    nl = [m.start() for m in re.finditer(r"\n", text)]

    def line(pos: int) -> int:
        return bisect.bisect_right(nl, pos - 1) + 1 if pos else 1

    tf = TwigFile()
    for m in TAG_RE.finditer(text):
        tag, body = m.group(1), m.group(2)
        ln = line(m.start())
        if tag == "block":
            bm = re.match(r"\s*(\w+)", body)
            if bm:
                tf.blocks.append((bm.group(1), ln))
            continue
        if tag == "component":
            sm = re.match(r"""\s*(?:'([^']+)'|"([^"]+)"|([\w:]+))""", body)
            if sm:
                tf.components.append((sm.group(1) or sm.group(2) or sm.group(3), ln, "tag"))
            continue
        if tag not in CUT_RE:
            continue
        cut = CUT_RE[tag]
        expr = body
        if cut is not None:
            cm = cut.search(" " + body)
            if cm:
                expr = body[: max(0, cm.start() - 1)]
        if tag == "form_theme":
            # first token is the form variable
            expr = re.sub(r"^\s*[\w.]+", "", expr, count=1)
        names = [s for s in _strings(expr) if _is_template_name(s)]
        if "_self" in expr and not names:
            continue
        dynamic = not names or bool(re.search(r"[?~]|\bor\b|\band\b|\(", re.sub(STR_RE, "", expr)))
        if not names:
            tf.dynamic_refs += 1
            continue
        tf.refs.append(TwigRef("import" if tag == "from" else tag, names, ln, dynamic=dynamic and len(names) > 1))

    for m in FUNC_RE.finditer(text):
        fn, arg, arg2 = m.group(1), m.group(2), m.group(3)
        ln = line(m.start())
        strs = _strings(arg)
        if fn == "include" or fn == "source":
            names = [s for s in strs if _is_template_name(s)]
            if names:
                tf.refs.append(TwigRef(fn, names, ln, dynamic=len(names) > 1))
            else:
                tf.dynamic_refs += 1
        elif fn == "block":
            if arg2:
                tpl = _strings(arg2)
                if tpl and _is_template_name(tpl[0]):
                    tf.refs.append(TwigRef("block_ref", tpl, ln, block=(strs[0] if strs else None)))
        elif fn == "component":
            if strs:
                tf.components.append((strs[0], ln, "function"))
        else:  # stimulus_*
            if strs:
                tf.stimulus.append((strs[0], ln))
            elif arg == "controller":
                tf.stimulus.append(("{{controller}}", ln))
    for m in TWIG_COMPONENT_RE.finditer(text):
        name = m.group(1)
        if name.split(":")[0] in ("block",):
            continue
        tf.components.append((name, line(m.start()), "html"))
    for m in DATA_CONTROLLER_RE.finditer(text):
        val = m.group(2)
        ln = line(m.start())
        if "{{" in val and re.search(r"\{\{\s*controller\s*\}\}", val):
            tf.stimulus.append(("{{controller}}", ln))
        val = re.sub(r"\{\{.*?\}\}|\{%.*?%\}", " ", val, flags=re.S)
        for name in val.split():
            if re.fullmatch(r"[\w\-]+", name):
                tf.stimulus.append((name, ln))
    return tf


# ------------------------------------------------------------ resolution
class TemplateResolver:
    """Maps Twig logical names to repo-relative files for one app."""

    def __init__(self, project_root: Path, dirs: list[tuple[str | None, str]], exists):
        self.root = project_root
        self.dirs = dirs
        self.exists = exists  # callable(repo_rel) -> bool
        self.main = [d for ns, d in dirs if ns is None]

    def resolve(self, name: str) -> str | None:
        name = name.strip()
        if not name:
            return None
        if name.startswith("@"):
            no_override = name.startswith("@!")
            body = name[2:] if no_override else name[1:]
            ns, _, path = body.partition("/")
            if not path:
                return None
            if not no_override:
                bundle = ns if ns.endswith("Bundle") else ns + "Bundle"
                for d in self.main[:1]:
                    cand = f"{d}/bundles/{bundle}/{path}"
                    if self.exists(cand):
                        return cand
            for dns, d in self.dirs:
                if dns == ns:
                    cand = f"{d}/{path}"
                    if self.exists(cand):
                        return cand
            if ns in ("App", "__main__"):
                return self._main(path)
            return None
        return self._main(name)

    def _main(self, path: str) -> str | None:
        path = os.path.normpath(path.lstrip("/")).replace(os.sep, "/")
        if path.startswith(".."):
            return None
        for d in self.main:
            cand = f"{d}/{path}" if d else path
            if self.exists(cand):
                return cand
        return None

    def logical_name(self, rel_file: str) -> str | None:
        for ns, d in self.dirs:
            prefix = d + "/" if d else ""
            if rel_file.startswith(prefix):
                sub = rel_file[len(prefix):]
                if ns is None and sub.startswith("bundles/") and sub.count("/") >= 2:
                    _, bundle, rest = sub.split("/", 2)
                    return f"@{bundle[:-6] if bundle.endswith('Bundle') else bundle}/{rest}"
                return sub if ns is None else f"@{ns}/{sub}"
        return None
