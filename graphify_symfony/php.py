"""Lightweight PHP scanner: namespaces, imports, class/method/property
declarations with their PHP 8 attributes (and Doctrine/Symfony docblock
annotations), template string references and getSubscribedEvents() keys.

Regex based on purpose: it only needs declarations and literal arguments,
must be fast on 30+ MB of PHP, and never executes anything."""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

# Files containing none of these only need the cheap FQCN index pass.
INTERESTING = re.compile(
    r"\.twig\b|#\[\s*\\?[\w\\]*(?:As\w+|Route|Template|Entity|ManyToOne|OneToMany|ManyToMany|OneToOne|Embedded|"
    r"Autowire|AsDecorator)|@(?:ORM\\|Route|Template)|getSubscribedEvents|MessageHandler|"
    r"ServiceEntityRepository"
)
NAMESPACE_RE = re.compile(r"^\s*namespace\s+([\w\\]+)\s*[;{]", re.M)
CLASS_RE = re.compile(
    r"^[ \t]*(?:(?:final|abstract|readonly)\s+)*(class|interface|trait|enum)\s+(\w+)([^{;]*)", re.M
)
USE_RE = re.compile(r"^\s*use\s+(?!function\b|const\b)([\w\\]+)(?:\s*\\\{([^}]*)\}|\s+as\s+(\w+))?\s*;", re.M)
USE_MULTI_RE = re.compile(r"^\s*use\s+(?!function\b|const\b)([\w\\]+(?:\s+as\s+\w+)?(?:\s*,\s*[\w\\]+(?:\s+as\s+\w+)?)+)\s*;", re.M)
DECL_RE = re.compile(
    r"(?P<cls>^[ \t]*(?:(?:final|abstract|readonly)\s+)*(?P<ckind>class|interface|trait|enum)\s+(?P<cname>\w+)(?P<chead>[^{;]*))"
    r"|(?P<fn>\bfunction\s+&?(?P<fname>\w+)\s*\()"
    r"|(?P<prop>^[ \t]*(?:#\[[^\n]*\]\s*)?(?:(?:public|protected|private|readonly|static|var)(?:\([^)]*\))?\s+)+"
    r"(?P<ptype>\??[\w\\|?&]+\s+)?\$(?P<pname>\w+))",
    re.M,
)
DOC_RE = re.compile(r"/\*\*.*?\*/", re.S)
TWIG_STR_RE = re.compile(r"""(['"])([^'"\n$]{1,200}?\.twig)\1""")
RENDER_CALL_RE = re.compile(
    r"(?:->|::)\s*(render|renderView|renderBlock|renderBlockView|stream|htmlTemplate|textTemplate|"
    r"renderForm|load|display)\s*\(\s*$"
)
SUBSCRIBED_KEY_RE = re.compile(r"""([\w\\]+::\w+|'[^'\n]+'|"[^"\n]+")\s*=>""")


@dataclass
class Attr:
    fqcn: str
    short: str
    args: list
    kwargs: dict
    line: int
    annotation: bool = False


@dataclass
class Decl:
    kind: str  # class | method | property
    name: str
    line: int
    attrs: list[Attr] = field(default_factory=list)
    type: str | None = None        # property type / first param type
    head: str = ""                  # class header (extends/implements) or params text
    pos: int = 0


@dataclass
class PhpClass:
    name: str
    fqcn: str
    kind: str
    line: int
    attrs: list[Attr] = field(default_factory=list)
    extends: str | None = None
    implements: list[str] = field(default_factory=list)
    methods: list[Decl] = field(default_factory=list)
    props: list[Decl] = field(default_factory=list)
    subscribed_events: list[tuple[int, str]] = field(default_factory=list)
    pos: int = 0

    def method(self, name: str) -> Decl | None:
        for m in self.methods:
            if m.name.lower() == name.lower():
                return m
        return None


@dataclass
class PhpFile:
    rel: str
    namespace: str = ""
    uses: dict[str, str] = field(default_factory=dict)
    classes: list[PhpClass] = field(default_factory=list)
    twig_refs: list[tuple[int, str, str, str | None]] = field(default_factory=list)  # line, name, kind, extra
    parsed: bool = False

    def resolve(self, name: str) -> str:
        return resolve_name(name, self.namespace, self.uses)

    def class_at(self, pos: int) -> PhpClass | None:
        best = None
        for c in self.classes:
            if c.pos <= pos:
                best = c
        return best


def resolve_name(name: str, namespace: str, uses: dict[str, str]) -> str:
    name = name.strip()
    if name.startswith("\\"):
        return name[1:]
    if name.lower() in ("self", "static", "parent"):
        return name
    head, _, rest = name.partition("\\")
    if head in uses:
        return uses[head] + ("\\" + rest if rest else "")
    if name in ("string", "int", "float", "bool", "array", "mixed", "object", "callable", "iterable", "void", "null", "never", "false", "true"):
        return name
    return f"{namespace}\\{name}" if namespace else name


class _Lines:
    def __init__(self, text: str):
        self.offsets = [m.end() for m in re.finditer(r"\n", text)]

    def line(self, pos: int) -> int:
        return bisect.bisect_right(self.offsets, pos - 1) + 1 if pos else 1


# ------------------------------------------------------------ literal parser
class _ValueParser:
    """Parses PHP attribute / Doctrine annotation argument lists."""

    def __init__(self, text: str, resolve):
        self.s = text
        self.i = 0
        self.resolve = resolve

    def ws(self):
        while self.i < len(self.s) and self.s[self.i] in " \t\r\n*":
            self.i += 1

    def args(self, closer: str) -> tuple[list, dict]:
        pos, kw = [], {}
        while True:
            self.ws()
            if self.i >= len(self.s) or self.s[self.i] == closer:
                self.i += 1
                return pos, kw
            m = re.compile(r"(\w+)\s*(?::(?!:)|=(?![=>]))").match(self.s, self.i)
            key = None
            if m:
                key = m.group(1)
                self.i = m.end()
            val = self.value()
            if key is None:
                pos.append(val)
            else:
                kw[key] = val
            self.ws()
            if self.i < len(self.s) and self.s[self.i] == ",":
                self.i += 1
            elif self.i < len(self.s) and self.s[self.i] != closer:
                self.skip_to(",", closer)

    def skip_to(self, *chars):
        depth = 0
        while self.i < len(self.s):
            c = self.s[self.i]
            if c in "'\"":
                self.string()
                continue
            if c in "([{":
                depth += 1
            elif c in ")]}":
                if depth == 0:
                    return
                depth -= 1
            elif depth == 0 and c in chars:
                return
            self.i += 1

    def string(self) -> str:
        q = self.s[self.i]
        self.i += 1
        out = []
        while self.i < len(self.s) and self.s[self.i] != q:
            if self.s[self.i] == "\\" and self.i + 1 < len(self.s):
                nxt = self.s[self.i + 1]
                out.append(nxt if nxt in (q, "\\") else "\\" + nxt)
                self.i += 2
                continue
            out.append(self.s[self.i])
            self.i += 1
        self.i += 1
        return "".join(out)

    def value(self):
        self.ws()
        if self.i >= len(self.s):
            return None
        c = self.s[self.i]
        if c in "'\"":
            v = self.string()
            self.ws()
            if self.s.startswith("::class", self.i):  # "Foo"::class does not exist, ignore
                pass
            return v
        if c in "[{":
            closer = "]" if c == "[" else "}"
            self.i += 1
            items: list = []
            mapping: dict = {}
            while True:
                self.ws()
                if self.i >= len(self.s):
                    break
                if self.s[self.i] == closer:
                    self.i += 1
                    break
                k = self.value()
                self.ws()
                if self.s.startswith("=>", self.i) or (closer == "}" and self.s[self.i:self.i + 1] in ("=", ":")):
                    self.i += 2 if self.s.startswith("=>", self.i) else 1
                    v = self.value()
                    mapping[str(k)] = v
                else:
                    items.append(k)
                self.ws()
                if self.i < len(self.s) and self.s[self.i] == ",":
                    self.i += 1
                elif self.i < len(self.s) and self.s[self.i] != closer:
                    self.skip_to(",", closer)
            return mapping if mapping else items
        m = re.compile(r"(new\s+)?\\?[\w\\]+(?:::\w+)?").match(self.s, self.i)
        if m:
            self.i = m.end()
            tok = m.group(0)
            if tok.startswith("new"):
                self.ws()
                if self.i < len(self.s) and self.s[self.i] == "(":
                    self.i += 1
                    self.skip_to(")")
                    self.i += 1
                return ("new", self.resolve(tok[3:].strip()))
            if tok.endswith("::class"):
                return ("class", self.resolve(tok[:-7]))
            if "::" in tok:
                cls, const = tok.split("::", 1)
                return ("const", self.resolve(cls), const)
            low = tok.lower()
            if low in ("true", "false"):
                return low == "true"
            if low == "null":
                return None
            if re.fullmatch(r"-?\d+", tok):
                return int(tok)
            return ("const", "", tok)
        # unknown expression: skip
        self.skip_to(",", ")", "]", "}")
        return None


def _balanced_end(text: str, i: int, open_c: str, close_c: str) -> int:
    """Index just after the bracket matching text[i] (== open_c)."""
    depth = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in "'\"":
            q = c
            i += 1
            while i < n and text[i] != q:
                i += 2 if text[i] == "\\" else 1
        elif c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _split_attr_group(body: str) -> list[tuple[str, str | None]]:
    """'A(x), B' -> [('A', 'x'), ('B', None)]"""
    out = []
    i = 0
    while i < len(body):
        m = re.compile(r"\s*,?\s*(\\?[\w\\]+)\s*").match(body, i)
        if not m or not m.group(1):
            break
        name = m.group(1)
        i = m.end()
        args = None
        if i < len(body) and body[i] == "(":
            end = _balanced_end(body, i, "(", ")")
            args = body[i + 1:end - 1]
            i = end
        out.append((name, args))
        while i < len(body) and body[i] in " \t\r\n,":
            i += 1
    return out


def _parse_attrs(text: str, start: int, end: int, pf: PhpFile, lines: _Lines, annotation=False) -> list[Attr]:
    res: list[Attr] = []
    seg = text[start:end]
    if annotation:
        for m in re.finditer(r"@(\\?[A-Z][\w\\]*)\s*(\()?", seg):
            name = m.group(1)
            args = ""
            if m.group(2):
                a0 = m.end() - 1
                e = _balanced_end(seg, a0, "(", ")")
                args = seg[a0 + 1:e - 1]
            res.append(_mk_attr(name, args, pf, lines.line(start + m.start()), True))
        return res
    body = seg[2:-1]  # strip '#[' and ']'
    for name, args in _split_attr_group(body):
        res.append(_mk_attr(name, args or "", pf, lines.line(start), False))
    return res


def _mk_attr(name: str, args: str, pf: PhpFile, line: int, annotation: bool) -> Attr:
    fq = pf.resolve(name)
    try:
        pos, kw = _ValueParser(args + ")", pf.resolve).args(")") if args else ([], {})
    except Exception:
        pos, kw = [], {}
    return Attr(fqcn=fq, short=fq.rsplit("\\", 1)[-1], args=pos, kwargs=kw, line=line, annotation=annotation)


# ------------------------------------------------------------------ scanning
def quick_scan(rel: str, text: str) -> PhpFile:
    """Namespace + class declarations only (for the FQCN index)."""
    pf = PhpFile(rel=rel)
    m = NAMESPACE_RE.search(text)
    if m:
        pf.namespace = m.group(1)
    for cm in CLASS_RE.finditer(text):
        pf.classes.append(PhpClass(name=cm.group(2), fqcn=f"{pf.namespace}\\{cm.group(2)}" if pf.namespace else cm.group(2),
                                   kind=cm.group(1), line=text.count("\n", 0, cm.start()) + 1, pos=cm.start()))
    return pf


def _parse_uses(text: str, pf: PhpFile) -> None:
    for m in USE_RE.finditer(text):
        base, group, alias = m.group(1), m.group(2), m.group(3)
        if group is not None:
            for part in group.split(","):
                part = part.strip()
                if not part:
                    continue
                p, _, a = part.partition(" as ")
                p = p.strip()
                pf.uses[(a.strip() or p.rsplit("\\", 1)[-1])] = base.rstrip("\\") + "\\" + p
        else:
            pf.uses[alias or base.rsplit("\\", 1)[-1]] = base
    for m in USE_MULTI_RE.finditer(text):
        for part in m.group(1).split(","):
            p, _, a = part.strip().partition(" as ")
            p = p.strip()
            pf.uses[(a.strip() or p.rsplit("\\", 1)[-1])] = p


def full_scan(rel: str, text: str) -> PhpFile:
    pf = quick_scan(rel, text)
    pf.parsed = True
    pf.classes = []
    _parse_uses(text, pf)
    lines = _Lines(text)

    # attribute groups and docblocks, attached to the next declaration
    pending: list[tuple[int, list[Attr]]] = []
    excluded = [(m.start(), m.end()) for m in re.finditer(r"/\*.*?\*/|(?<![:'\"])//[^\n]*", text, re.S)]
    for m in re.finditer(r"\bfunction\s+&?\w+\s*\(", text):
        excluded.append((m.end() - 1, _balanced_end(text, m.end() - 1, "(", ")")))
    excluded.sort()
    merged: list[list[int]] = []
    for a_, b_ in excluded:
        if merged and a_ <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b_)
        else:
            merged.append([a_, b_])
    ex_starts = [x[0] for x in merged]

    def _in_excluded(pos: int) -> bool:
        i = bisect.bisect_right(ex_starts, pos) - 1
        return i >= 0 and merged[i][0] <= pos < merged[i][1]

    for m in re.finditer(r"#\[", text):
        if _in_excluded(m.start()):
            continue
        e = _balanced_end(text, m.start() + 1, "[", "]")
        pending.append((e, _parse_attrs(text, m.start(), e, pf, lines)))
    for m in DOC_RE.finditer(text):
        if "@" in m.group(0) and re.search(r"@(?:ORM\\|Route|Template|Doctrine|IsGranted|Security)", m.group(0)):
            pending.append((m.end(), _parse_attrs(text, m.start(), m.end(), pf, lines, annotation=True)))
    pending.sort(key=lambda x: x[0])

    decls: list[tuple[int, str, Decl, re.Match]] = []
    for m in DECL_RE.finditer(text):
        if m.group("cls"):
            d = Decl("class", m.group("cname"), lines.line(m.start("ckind")), head=m.group("chead") or "", pos=m.start())
            d.type = m.group("ckind")
        elif m.group("fn"):
            pe = _balanced_end(text, m.end() - 1, "(", ")")
            d = Decl("method", m.group("fname"), lines.line(m.start("fn")), head=text[m.end():pe - 1], pos=m.start("fn"))
            pm = re.match(r"\s*(?:#\[[^\]]*\]\s*)*(?:(?:public|protected|private|readonly)\s+)*(\??[\w\\|]+)\s+&?(?:\.\.\.)?\$", d.head)
            if pm:
                d.type = pm.group(1)
        else:
            d = Decl("property", m.group("pname"), lines.line(m.start("pname")), pos=m.start("prop"))
            d.type = (m.group("ptype") or "").strip() or None
        key = m.start("ckind") if m.group("cls") else (m.start("fn") if m.group("fn") else m.start("pname"))
        decls.append((key, d.kind, d, m))
    # attach attributes: each pending group goes to the first decl starting after it
    starts = [x[0] for x in decls]
    for end, attrs in pending:
        i = bisect.bisect_left(starts, end)
        # attribute inline on the same line as a promoted property is caught by DECL prop regex
        if i < len(decls):
            decls[i][2].attrs.extend(attrs)

    cur: PhpClass | None = None
    for _pos, kind, d, _m in decls:
        if kind == "class":
            head = d.head
            ext = re.search(r"\bextends\s+([\w\\]+(?:\s*,\s*[\w\\]+)*)", head)
            imp = re.search(r"\bimplements\s+([\w\\,\s]+)", head)
            cur = PhpClass(name=d.name, fqcn=pf.resolve(d.name) if not pf.namespace else f"{pf.namespace}\\{d.name}",
                           kind=d.type or "class", line=d.line, attrs=d.attrs, pos=d.pos)
            if not pf.namespace:
                cur.fqcn = d.name
            if ext:
                cur.extends = pf.resolve(ext.group(1).split(",")[0].strip())
            if imp:
                cur.implements = [pf.resolve(x.strip()) for x in imp.group(1).split(",") if x.strip()]
            pf.classes.append(cur)
        elif cur is not None:
            if kind == "method":
                cur.methods.append(d)
            else:
                cur.props.append(d)

    # getSubscribedEvents keys
    for c in pf.classes:
        m = c.method("getSubscribedEvents")
        if not m:
            continue
        nxt = [x.pos for x in c.methods if x.pos > m.pos]
        body = text[m.pos: (min(nxt) if nxt else len(text))]
        base_line = m.line
        for km in SUBSCRIBED_KEY_RE.finditer(body):
            key = km.group(1)
            line = base_line + body.count("\n", 0, km.start())
            if key[0] in "'\"":
                c.subscribed_events.append((line, key[1:-1]))
            else:
                cls, const = key.split("::", 1)
                fq = pf.resolve(cls)
                c.subscribed_events.append((line, fq + "::" + const if const != "class" else fq))

    # template references (string literals ending in .twig)
    for m in TWIG_STR_RE.finditer(text):
        before = text[max(0, m.start() - 80):m.start()]
        kind = "template_ref"
        extra = None
        cm = RENDER_CALL_RE.search(before)
        if cm:
            kind = "render"
            extra = cm.group(1)
            if extra in ("renderBlock", "renderBlockView"):
                bn = render_block_name(text[m.end():m.end() + 120])
                if bn:
                    extra = f"{extra}:{bn}"
        elif re.search(r"(?:#\[\s*\\?[\w\\]*Template\s*\(|@Template\s*\()\s*(?:template\s*[:=]\s*)?$", before):
            kind = "template_attr"
        elif re.search(r"template\s*:\s*$", before) and re.search(r"As(?:Twig|Live|Sdc)Component", before):
            kind = "component_template"
        pf.twig_refs.append((lines.line(m.start()), m.group(2), kind, extra))
    # renderBlock('tpl', 'block') second arg
    return pf


def render_block_name(text_after: str) -> str | None:
    m = re.match(r"\s*,\s*(['\"])(\w+)\1", text_after)
    return m.group(2) if m else None
