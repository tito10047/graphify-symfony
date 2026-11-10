"""Read-only lookups over the Symfony layer of graph.json.

`graphify explain "/products/5"` cannot answer "which controller serves this URL":
graphify resolves the query against file paths first (``templates/products/...``
wins over the route node) and knows nothing about ``{id}`` placeholders.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from .graph import links_key

# {name}, {name<requirement>}, {name?default}, {name<req>?default}
_PLACEHOLDER = re.compile(r"\{!?(\w+)(?:<((?:[^<>]|<[^<>]*>)*)>)?(\?[^}]*)?\}")

# edges that lead from a controller method to what it renders
TEMPLATE_KINDS = ("render", "render_block", "template_ref")


@dataclass
class RouteHit:
    node: dict
    how: str  # "name", "url", "fragment"
    params: dict = field(default_factory=dict)
    controller: dict | None = None
    templates: list[dict] = field(default_factory=list)


def _pattern_regex(path: str) -> re.Pattern:
    out, pos = [], 0
    for m in _PLACEHOLDER.finditer(path):
        lit = path[pos:m.start()]
        name, req, opt = m.group(1), m.group(2) or "[^/]+", m.group(3)
        group = f"(?P<{name}>{req})"
        if opt is not None:
            # optional placeholder swallows the preceding slash too (/blog/{page?1} matches /blog)
            if lit.endswith("/"):
                out.append(re.escape(lit[:-1]) + f"(?:/{group})?")
            else:
                out.append(re.escape(lit) + f"{group}?")
        else:
            out.append(re.escape(lit) + group)
        pos = m.end()
    out.append(re.escape(path[pos:]))
    return re.compile("".join(out) + "/?")


def _normalize_url(q: str) -> str:
    if "://" in q:
        q = urlsplit(q).path or "/"
    q = q.split("?", 1)[0].split("#", 1)[0]
    # front controllers: /index.php/foo -> /foo
    q = re.sub(r"^/[\w.-]+\.php(?=/|$)", "", q) or "/"
    return q if q.startswith("/") else "/" + q


def _paths(n: dict) -> list[str]:
    return [p for p in dict.fromkeys((n.get("sf_full_path"), n.get("sf_path"))) if p]


def _static_len(path: str) -> int:
    return len(_PLACEHOLDER.sub("", path))


def find_routes(graph: dict, query: str, method: str | None = None, app: str | None = None) -> list[RouteHit]:
    routes = [n for n in graph["nodes"] if n.get("sf_type") == "route"]
    if app:
        prefix = app.strip("/") + "/"
        routes = [n for n in routes if (n.get("source_file") or "").startswith(prefix)]
    if method:
        method = method.upper()
        routes = [n for n in routes if not n.get("sf_methods") or method in n["sf_methods"]]

    by_name = [RouteHit(n, "name") for n in routes if n.get("sf_route_name") == query]
    if by_name:
        return by_name

    hits: list[tuple[int, RouteHit]] = []
    if query.startswith("/") or "://" in query:
        url = _normalize_url(query)
        for n in routes:
            for p in _paths(n):
                m = _pattern_regex(p).fullmatch(url)
                if m:
                    # literal routes before parametrised ones, like Symfony's matcher prefers them
                    hits.append((-_static_len(p), RouteHit(n, "url", {k: v for k, v in m.groupdict().items() if v})))
                    break
    if hits:
        hits.sort(key=lambda t: t[0])
        return [h for _, h in hits]

    q = query.lower()
    return [RouteHit(n, "fragment") for n in routes
            if q in (n.get("sf_route_name") or "").lower() or any(q in p.lower() for p in _paths(n))]


def resolve(graph: dict, hits: list[RouteHit]) -> None:
    """Fill in the controller method and the templates it renders."""
    nodes = {n["id"]: n for n in graph["nodes"]}
    out: dict[str, list[dict]] = {}
    for e in graph[links_key(graph)]:
        out.setdefault(e["source"], []).append(e)
    for h in hits:
        ctrl = next((e["target"] for e in out.get(h.node["id"], []) if e.get("sf_kind") == "route"), None)
        h.controller = nodes.get(ctrl)
        if ctrl:
            seen = set()
            for e in out.get(ctrl, []):
                if e.get("sf_kind") in TEMPLATE_KINDS and e["target"] not in seen and e["target"] in nodes:
                    seen.add(e["target"])
                    h.templates.append(nodes[e["target"]])


def _loc(n: dict) -> str:
    src = n.get("source_file") or "?"
    loc = n.get("source_location") or ""
    return f"{src}:{loc}" if loc else src


def _owner(ctrl: dict) -> str:
    """``.show()`` in src/Controller/ProductController.php -> ``ProductController::show()``."""
    label = ctrl.get("label", "?")
    src = ctrl.get("source_file")
    if label.startswith(".") and src and src.endswith(".php"):
        return f"{PurePosixPath(src).stem}::{label[1:]}"
    return label


def to_json(h: RouteHit) -> dict:
    n = h.node
    return {
        "name": n.get("sf_route_name"),
        "path": n.get("sf_full_path") or n.get("sf_path"),
        "methods": n.get("sf_methods") or [],
        "matched_by": h.how,
        "params": h.params,
        "label": n.get("label"),
        "controller": None if not h.controller else {
            "label": _owner(h.controller), "source_file": h.controller.get("source_file"),
            "source_location": h.controller.get("source_location"),
        },
        "templates": [{"label": t.get("label"), "source_file": t.get("source_file")} for t in h.templates],
    }


def format_hit(h: RouteHit) -> str:
    n = h.node
    methods = ",".join(n.get("sf_methods") or []) or "ANY"
    lines = [f"{n.get('sf_route_name') or '(unnamed)'}  {methods} {n.get('sf_full_path') or n.get('sf_path')}"]
    if h.params:
        lines.append("  params:     " + ", ".join(f"{k}={v}" for k, v in h.params.items()))
    if h.controller:
        lines.append(f"  controller: {_owner(h.controller)}  {_loc(h.controller)}")
    else:
        lines.append(f"  controller: ?  (route declared in {_loc(n)})")
    for t in h.templates:
        lines.append(f"  renders:    {t.get('label')}  {t.get('source_file')}")
    lines.append(f"  node:       {n.get('label')}")
    return "\n".join(lines)
