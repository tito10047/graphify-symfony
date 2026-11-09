"""End-to-end against the real graphify: extract -> post-pass -> graphify update.

Skipped when graphify (PyPI: graphifyy) is not importable."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from graphify_symfony.cli import main
from graphify_symfony.graph import ORIGIN

from .conftest import write_app

graphify = pytest.importorskip("graphify")

ENV = {**os.environ, "GRAPHIFY_BACKEND": "none", "GRAPHIFY_QUERY_LOG_DISABLE": "1", "PYTHONHASHSEED": "0"}


def _gfy(cwd, *args):
    return subprocess.run([sys.executable, "-m", "graphify", *args], cwd=cwd, env=ENV,
                          capture_output=True, text=True, timeout=300)


def _ours(root):
    g = json.loads((root / "graphify-out" / "graph.json").read_text())
    return ({n["id"] for n in g["nodes"] if n.get("_origin") == ORIGIN},
            {(e["source"], e["target"], e["sf_kind"]) for e in g["links"] if e.get("_origin") == ORIGIN})


def test_survives_graphify_update(tmp_path, monkeypatch):
    root = write_app(tmp_path / "acme")
    r = _gfy(root, "extract", ".", "--code-only")
    assert (root / "graphify-out" / "graph.json").is_file(), r.stdout + r.stderr
    monkeypatch.setenv("GRAPHIFY_BACKEND", "none")
    assert main(["run", "--root", str(root)]) == 0
    nodes, edges = _ours(root)
    assert ("src_controller_productcontroller_productcontroller_show", "templates_product_show_html",
            "render") in edges

    r = _gfy(root, "update", ".")
    assert r.returncode == 0, r.stdout + r.stderr
    nodes2, edges2 = _ours(root)
    assert nodes2 == nodes
    # a plain rebuild may collapse a parallel edge between the same two nodes;
    # everything else is preserved because our _origin is not "ast"
    assert len(edges2) >= len(edges) * 0.9

    r = _gfy(root, "explain", "product/show.html.twig")
    assert "base.html.twig [extends]" in r.stdout
    r = _gfy(root, "affected", "PriceCalculator")
    assert "ProductController" in r.stdout

    # re-applying restores the full set
    assert main(["run", "--root", str(root)]) == 0
    assert _ours(root) == (nodes, edges)
