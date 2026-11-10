from __future__ import annotations

import json

import pytest

from graphify_symfony.cli import main
from graphify_symfony.lookup import _normalize_url, _pattern_regex, find_routes


@pytest.mark.parametrize("pattern,url,params", [
    ("/products/{id}", "/products/5", {"id": "5"}),
    ("/products/{id}", "/products/5/", {"id": "5"}),
    ("/products/{id<\\d+>}", "/products/5", {"id": "5"}),
    ("/products/{id<\\d+>}", "/products/abc", None),
    ("/blog/{page?1}", "/blog", {}),
    ("/blog/{page<\\d+>?1}", "/blog/3", {"page": "3"}),
    ("/{_locale}/a.{_format}", "/sk/a.json", {"_locale": "sk", "_format": "json"}),
    ("/products/{id}", "/products/5/edit", None),
    ("/legacy", "/legacy", {}),
])
def test_pattern_regex(pattern, url, params):
    m = _pattern_regex(pattern).fullmatch(url)
    assert (None if m is None else {k: v for k, v in m.groupdict().items() if v}) == params


@pytest.mark.parametrize("q,url", [
    ("https://example.com/en/products/5?x=1#top", "/en/products/5"),
    ("/index.php/products", "/products"),
    ("products", "/products"),
])
def test_normalize_url(q, url):
    assert _normalize_url(q) == url


def _route(name, path, src="src/Controller/A.php", methods=None):
    return {"id": name, "label": f"route {name} {path}", "sf_type": "route", "sf_route_name": name,
            "sf_path": path, "sf_methods": methods, "source_file": src}


def test_find_routes_prefers_literal_and_filters():
    g = {"nodes": [_route("catch_all", "/{slug}", "apps/front/src/C.php"),
                   _route("about", "/about", "apps/front/src/C.php", ["GET"]),
                   _route("admin_about", "/about", "apps/admin/src/C.php", ["POST"])], "links": []}
    assert [h.node["id"] for h in find_routes(g, "/about")] == ["about", "admin_about", "catch_all"]
    assert [h.node["id"] for h in find_routes(g, "/about", method="post")] == ["admin_about", "catch_all"]
    assert [h.node["id"] for h in find_routes(g, "/about", app="apps/admin")] == ["admin_about"]
    assert [h.how for h in find_routes(g, "about")] == ["name"]
    assert {h.node["id"] for h in find_routes(g, "abou")} == {"about", "admin_about"}


def test_route_command(app, capsys):
    assert main(["run", "--root", str(app)]) == 0
    capsys.readouterr()

    assert main(["route", "/en/products/42", "--root", str(app), "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    r = res["routes"][0]
    assert (r["name"], r["params"], r["matched_by"]) == ("product_show", {"id": "42"}, "url")
    assert r["controller"]["label"] == "ProductController::show()"
    assert "product/show.html.twig" in [t["label"] for t in r["templates"]]

    assert main(["route", "/products/42", "--root", str(app)]) == 0  # attribute path, no locale prefix
    out = capsys.readouterr().out
    assert "product_show" in out and "ProductController::show()" in out

    assert main(["route", "/products/42", "--method", "DELETE", "--root", str(app)]) == 4
    assert main(["route", "legacy_home", "--root", str(app)]) == 0
    assert "ProductController::legacy()" in capsys.readouterr().out


def test_route_command_without_graph(tmp_path):
    assert main(["route", "/x", "--root", str(tmp_path)]) == 2
