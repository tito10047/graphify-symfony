"""graph.json loading, indexing of graphify's own nodes, idempotent merge."""

from __future__ import annotations

import bisect
import contextlib
import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .ids import file_node_id, make_id

ORIGIN = "graphify-symfony"
COMMUNITY_NAME = "Symfony (graphify-symfony)"
CONFIDENCE_SCORE = {"EXTRACTED": 1.0, "INFERRED": 0.55, "AMBIGUOUS": 0.2}
_LOC_RE = re.compile(r"^L(\d+)")


def _line_of(node: dict) -> int:
    m = _LOC_RE.match(str(node.get("source_location") or ""))
    return int(m.group(1)) if m else 0


def load_graph(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def links_key(graph: dict) -> str:
    return "links" if "links" in graph else "edges"


def strip_ours(graph: dict) -> tuple[int, int]:
    lk = links_key(graph)
    n0, e0 = len(graph.get("nodes", [])), len(graph.get(lk, []))
    graph["nodes"] = [n for n in graph.get("nodes", []) if n.get("_origin") != ORIGIN]
    graph[lk] = [e for e in graph.get(lk, []) if e.get("_origin") != ORIGIN]
    return n0 - len(graph["nodes"]), e0 - len(graph[lk])


def write_graph_atomic(path: Path, graph: dict) -> None:
    # Same encoding graphify uses for graph.json (write_json_atomic(compact=True)).
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".graph.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(graph, fh, ensure_ascii=True, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def graphify_lock(out_dir: Path, blocking: bool = True):
    """Take graphify's own per-repo rebuild lock so we never interleave a write
    with a hook/watch rebuild. Falls back to a no-op outside the graphify venv."""
    try:
        from graphify.watch import _rebuild_lock  # type: ignore
    except Exception:
        yield True
        return
    with _rebuild_lock(out_dir, blocking=blocking) as got:
        yield got


@dataclass
class FileSymbols:
    file_id: str | None = None
    classes: list[tuple[int, str, str]] = field(default_factory=list)  # (line, id, label)
    methods: list[tuple[int, str, str]] = field(default_factory=list)  # (line, id, name)


class GraphIndex:
    """Read-only index over graphify's (non graphify-symfony) nodes."""

    def __init__(self, graph: dict):
        self.graph = graph
        self.ids: set[str] = set()
        self.nodes: dict[str, dict] = {}
        self.files: dict[str, FileSymbols] = defaultdict(FileSymbols)
        self.class_methods: dict[str, dict[str, str]] = defaultdict(dict)
        lk = links_key(graph)
        for n in graph.get("nodes", []):
            nid = n.get("id")
            if not isinstance(nid, str):
                continue
            self.ids.add(nid)
            self.nodes[nid] = n
        method_owner: dict[str, str] = {}
        self.imports: dict[str, set[str]] = defaultdict(set)
        for e in graph.get(lk, []):
            rel_ = e.get("relation")
            if rel_ == "method":
                method_owner[e.get("target")] = e.get("source")
            elif rel_ in ("imports", "imports_from") and e.get("_origin") != ORIGIN:
                self.imports[e.get("source")].add(e.get("target"))
        owners = set(method_owner.values())
        for nid, n in self.nodes.items():
            sf = n.get("source_file")
            if not sf or n.get("_origin") == ORIGIN:
                continue
            label = str(n.get("label", ""))
            fs = self.files[sf]
            if nid == file_node_id(sf):
                fs.file_id = nid
                continue
            if label.startswith(".") and label.endswith("()"):
                name = label[1:-2]
                fs.methods.append((_line_of(n), nid, name))
                owner = method_owner.get(nid)
                if owner:
                    self.class_methods[owner][name.lower()] = nid
            elif n.get("_callable_class") or (nid in owners and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", label)):
                fs.classes.append((_line_of(n), nid, label))
        for fs in self.files.values():
            fs.classes.sort()
            fs.methods.sort()
        self._max_community = max(
            (n.get("community") for n in self.nodes.values() if isinstance(n.get("community"), int)),
            default=0,
        )

    # -- lookups ---------------------------------------------------------
    def has(self, nid: str | None) -> bool:
        return bool(nid) and nid in self.ids

    def file_id(self, rel: str) -> str | None:
        fs = self.files.get(rel)
        if fs and fs.file_id:
            return fs.file_id
        nid = file_node_id(rel)
        return nid if nid in self.ids else None

    def class_id(self, rel: str, short_name: str) -> str | None:
        fs = self.files.get(rel)
        if fs:
            for _line, nid, label in fs.classes:
                if label == short_name:
                    return nid
        nid = make_id(file_node_id(rel), short_name)
        return nid if nid in self.ids else None

    def method_id(self, class_id: str | None, method: str) -> str | None:
        if not class_id:
            return None
        return self.class_methods.get(class_id, {}).get(method.lower())

    def symbol_at(self, rel: str, line: int) -> str | None:
        """Innermost graphify node (method > class > file) enclosing ``line``."""
        fs = self.files.get(rel)
        if not fs:
            return None
        cls = None
        if fs.classes:
            i = bisect.bisect_right([c[0] for c in fs.classes], line) - 1
            if i >= 0:
                cls = fs.classes[i][1]
        if cls and fs.methods:
            members = self.class_methods.get(cls, {})
            member_ids = set(members.values())
            best = None
            for mline, mid, _name in fs.methods:
                if mline > line:
                    break
                if mid in member_ids:
                    best = mid
            if best:
                return best
        return cls or fs.file_id

    def external_class(self, fqcn: str) -> str | None:
        """Node graphify minted for a vendor FQCN (import stub), if any."""
        nid = make_id(fqcn.lstrip("\\"))
        return nid if nid in self.ids else None

    @property
    def max_community(self) -> int:
        return self._max_community


class Emitter:
    """Collects nodes/edges in graphify's schema, then merges them."""

    def __init__(self, index: GraphIndex):
        self.index = index
        self.nodes: dict[str, dict] = {}
        self.edges: dict[tuple, dict] = {}
        self.dropped_edges = 0

    def node(self, nid: str, label: str, *, sf_type: str, source_file: str | None = None,
             line: int | None = None, file_type: str = "code", **extra) -> str:
        if nid in self.index.ids:  # never shadow a graphify node
            return nid
        if nid in self.nodes:
            cur = self.nodes[nid]
            for k, v in extra.items():
                if v is not None and k not in cur:
                    cur[k] = v
            return nid
        n = {
            "id": nid,
            "label": label,
            "_origin": ORIGIN,
            "file_type": file_type,
            "norm_label": label.lower(),
            "source_file": source_file or "",
            "sf_type": sf_type,
        }
        if source_file:
            n["source_location"] = f"L{line or 1}"
        for k, v in extra.items():
            if v is not None:
                n[k] = v
        self.nodes[nid] = n
        return nid

    def exists(self, nid: str | None) -> bool:
        return bool(nid) and (nid in self.index.ids or nid in self.nodes)

    def edge(self, source: str | None, target: str | None, relation: str, *, kind: str,
             confidence: str = "EXTRACTED", source_file: str | None = None,
             line: int | None = None, **extra) -> bool:
        if not source or not target or source == target:
            return False
        if not (self.exists(source) and self.exists(target)):
            self.dropped_edges += 1
            return False
        key = (source, target, relation, kind)
        if key in self.edges:
            return False
        e = {
            "source": source,
            "target": target,
            "relation": relation,
            "_origin": ORIGIN,
            "confidence": confidence,
            "confidence_score": CONFIDENCE_SCORE.get(confidence, 1.0),
            "sf_kind": kind,
            "weight": 1.0,
        }
        if not source_file:
            for end in (source, target):
                n = self.nodes.get(end) or self.index.nodes.get(end) or {}
                if n.get("source_file"):
                    source_file = n["source_file"]
                    m = _LOC_RE.match(str(n.get("source_location") or ""))
                    line = int(m.group(1)) if m else 1
                    break
        if source_file:
            e["source_file"] = source_file
            e["source_location"] = f"L{line or 1}"
        for k, v in extra.items():
            if v is not None:
                e[k] = v
        self.edges[key] = e
        return True

    # -- merge -----------------------------------------------------------
    def merge_into(self, graph: dict) -> tuple[int, int]:
        """Append collected items to ``graph`` (already stripped of ours).

        Every added node gets the community of its most common graphify
        neighbour so query/report grouping stays meaningful; isolated ones
        share one extra community.
        """
        lk = links_key(graph)
        # drop our nodes nobody connects to, except template files (they are
        # useful on their own: "which templates exist / are never rendered")
        connected: set[str] = set()
        for e in self.edges.values():
            connected.add(e["source"])
            connected.add(e["target"])
        nodes = [n for nid, n in self.nodes.items()
                 if nid in connected or n.get("sf_type") == "template"]
        keep = {n["id"] for n in nodes}
        edges = [e for e in self.edges.values()
                 if (e["source"] in keep or e["source"] in self.index.ids)
                 and (e["target"] in keep or e["target"] in self.index.ids)]

        neigh: dict[str, Counter] = defaultdict(Counter)
        for e in edges:
            for a, b in ((e["source"], e["target"]), (e["target"], e["source"])):
                if a in keep and b in self.index.nodes:
                    c = self.index.nodes[b].get("community")
                    if isinstance(c, int):
                        neigh[a][c] += 1
        # second pass: inherit from our own neighbours (template chains)
        for _ in range(2):
            for e in edges:
                for a, b in ((e["source"], e["target"]), (e["target"], e["source"])):
                    if a in keep and not neigh.get(a) and b in keep and neigh.get(b):
                        neigh[a][neigh[b].most_common(1)[0][0]] += 1
        fallback = self.index.max_community + 1
        names = {}
        for n in self.index.nodes.values():
            c = n.get("community")
            if isinstance(c, int) and c not in names and n.get("community_name"):
                names[c] = n["community_name"]
        for n in sorted(nodes, key=lambda x: x["id"]):
            c = neigh[n["id"]].most_common(1)[0][0] if neigh.get(n["id"]) else fallback
            n["community"] = c
            n["community_name"] = names.get(c, COMMUNITY_NAME if c == fallback else f"Community {c}")
        nodes.sort(key=lambda x: x["id"])
        edges.sort(key=lambda e: (e["source"], e["target"], e["relation"], e["sf_kind"]))
        graph["nodes"].extend(nodes)
        graph[lk].extend(edges)
        return len(nodes), len(edges)
