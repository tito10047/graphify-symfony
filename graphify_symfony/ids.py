"""Node ids. Graphify's own recipe is imported when available (it must be: we
run inside the graphifyy uv-tool venv); the fallback is a verbatim copy used
only by unit tests outside that venv."""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath

try:  # pragma: no cover - exercised in the real venv
    from graphify.ids import make_id as _make_id
    from graphify.extractors.base import _file_stem as _gfy_file_stem

    HAVE_GRAPHIFY = True
except Exception:  # pragma: no cover
    HAVE_GRAPHIFY = False

    def _normalize_id(s: str) -> str:
        cur = s
        for _ in range(6):
            nxt = unicodedata.normalize("NFKC", cur.casefold())
            if nxt == cur:
                break
            cur = nxt
        cur = re.sub(r"[^\w]+", "_", cur, flags=re.UNICODE)
        cur = re.sub(r"_+", "_", cur)
        return cur.strip("_")

    def _make_id(*parts: str) -> str:
        return _normalize_id("_".join(p.strip("_.") for p in parts if p))

    def _gfy_file_stem(path) -> str:
        if not path.name:
            return ""
        return path.with_suffix("").as_posix()


def make_id(*parts: str) -> str:
    return _make_id(*parts)


def file_stem(rel_path: str) -> str:
    return _gfy_file_stem(PurePosixPath(rel_path))


def file_node_id(rel_path: str) -> str:
    """Id graphify gives (or would give) to the file node of ``rel_path``."""
    return make_id(file_stem(rel_path))


def scoped_id(rel_path: str, *parts: str) -> str:
    """Id for a node owned by ``rel_path``.

    Starts with the canonical file stem so graphify's ``_semantic_id_remap``
    (which re-derives non-AST ids from ``source_file``) leaves it untouched.
    The ``sf`` segment keeps it clear of AST ids minted for the same file.
    """
    return make_id(file_stem(rel_path), "sf", *parts)


def global_id(*parts: str) -> str:
    """Id for a sourceless concept node (event names, ...)."""
    return make_id("sf", *parts)
