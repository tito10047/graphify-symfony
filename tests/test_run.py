from __future__ import annotations

import json
from pathlib import Path

from graphify_symfony.cli import main
from graphify_symfony.graph import ORIGIN


def _run(root: Path, *extra) -> dict:
    return main(["run", "--root", str(root), *extra])


def _load(root: Path) -> dict:
    return json.loads((root / "graphify-out" / "graph.json").read_text(encoding="utf-8"))


def _edges(g: dict) -> set[tuple[str, str, str]]:
    return {(e["source"], e["sf_kind"], e["target"]) for e in g["links"] if e.get("_origin") == ORIGIN}


def _nodes(g: dict) -> dict[str, dict]:
    return {n["id"]: n for n in g["nodes"] if n.get("_origin") == ORIGIN}


CTRL = "src_controller_productcontroller_productcontroller"
SHOW_TPL = "templates_product_show_html"


def test_run_adds_expected_edges(app):
    assert _run(app) == 0
    g = _load(app)
    edges = _edges(g)
    nodes = _nodes(g)

    # templates
    assert nodes[SHOW_TPL]["label"] == "product/show.html.twig"
    assert nodes[SHOW_TPL]["sf_type"] == "template"
    assert (SHOW_TPL, "extends", "templates_base_html") in edges
    assert (SHOW_TPL, "include", "templates_product_card_html") in edges
    assert (SHOW_TPL, "import", "templates_macros_forms_html") in edges
    assert ("templates_product_list_html", "use", "templates_product_blocks_html") in edges
    assert not any("commented_out" in t or "not_a_template" in t for _, _, t in edges)
    assert (SHOW_TPL, "defines_block", "templates_product_show_html_sf_block_body") in edges
    assert ("templates_product_show_html_sf_block_body", "overrides_block",
            "templates_base_html_sf_block_body") in edges
    assert ("config_packages_twig", "form_theme", "templates_form_theme_html") not in edges  # no AST node for yaml here

    # PHP -> Twig, attached to the method node
    assert (f"{CTRL}_show", "render", SHOW_TPL) in edges
    assert (f"{CTRL}_list", "render", "templates_product_list_html") in edges
    assert (f"{CTRL}_list", "render_block", "templates_product_show_html_sf_block_price") in edges
    assert (f"{CTRL}_legacy", "render", "templates_mail_welcome_html") in edges  # @mail namespace

    # components: class <-> template, usages (class, anonymous, by custom name)
    alert = "src_twig_components_alert_alert"
    assert (alert, "component_template", "templates_components_alert_html") in edges
    assert (SHOW_TPL, "component", alert) in edges
    assert (SHOW_TPL, "component", "templates_components_button_html") in edges
    summary = "src_twig_components_cart_summary_summary"
    assert (summary, "component_template", "templates_cart_summary_html") in edges
    assert (SHOW_TPL, "component", summary) in edges

    # stimulus
    assert ("templates_base_html", "stimulus", "assets_controllers_hello_controller") in edges
    assert (SHOW_TPL, "stimulus", "assets_controllers_cart_drawer_controller") in edges

    # routes: class prefix + method, full path from the route cache, yaml route
    routes = {n["sf_route_name"]: n for n in nodes.values() if n["sf_type"] == "route"}
    assert routes["product_show"]["sf_path"] == "/products/{id}"
    assert routes["product_show"]["sf_full_path"] == "/en/products/{id}"
    assert routes["product_list"]["sf_methods"] == ["GET", "HEAD"]
    assert "legacy_home" in routes
    assert not any("::" in k for k in routes)  # FQCN route aliases skipped
    show_route = next(i for i, n in nodes.items() if n.get("sf_route_name") == "product_show")
    assert (show_route, "route", f"{CTRL}_show") in edges
    assert "/not-a-route" not in json.dumps(routes)

    # doctrine
    product = "src_entity_product_product"
    assert (product, "doctrine_repository", "src_repository_productrepository_productrepository") in edges
    assert (product, "doctrine_manytoone", "src_entity_category_category") in edges
    assert (product, "doctrine_manytoone", "src_entity_brand_brand") in edges  # inferred from type
    assert ("src_entity_category_category", "doctrine_repository",
            "src_repository_categoryrepository_categoryrepository") in edges  # annotation
    assert ("src_entity_category_category", "doctrine_onetomany", product) in edges

    # messenger, events, commands
    assert ("src_messagehandler_reindexproducthandler_reindexproducthandler_invoke", "handles_message",
            "src_message_reindexproduct_reindexproduct") in edges
    assert ("src_eventsubscriber_localesubscriber_localesubscriber_onrequest", "subscribes_to",
            "sf_event_kernel_request") in edges
    assert any(t == "sf_event_acme_custom" for _, k, t in edges if k == "subscribes_to")
    cmd = next(i for i, n in nodes.items() if n["sf_type"] == "console_command")
    assert nodes[cmd]["label"] == "console acme:import"

    # DI container (app services only)
    assert (CTRL, "injects", "src_service_pricecalculator_pricecalculator") in edges
    assert ("src_service_pricecalculator_pricecalculator", "injects", "src_service_taxtable_taxtable") in edges
    assert ("src_service_pricecalculatorinterface_pricecalculatorinterface", "di_alias",
            "src_service_pricecalculator_pricecalculator") in edges
    svc = [n for n in nodes.values() if n["sf_type"] == "service"]
    assert [n["sf_service_id"] for n in svc] == ["acme.tax_table.cached"]
    assert not any("logger" in t for _, _, t in edges)  # vendor service, no graph node

    # schema
    for n in nodes.values():
        assert {"id", "label", "file_type", "source_file", "community", "norm_label", "_origin"} <= set(n)
        if n["source_file"]:
            assert n["id"].startswith(n["id"].split("_sf_")[0])
    for e in g["links"]:
        if e.get("_origin") == ORIGIN:
            assert e["confidence"] in ("EXTRACTED", "INFERRED", "AMBIGUOUS")
            assert e["source_file"]
            assert e["relation"] in ("extends", "embeds", "imports", "mixes_in", "uses", "references", "defines")


def test_idempotent_and_strip(app):
    gpath = app / "graphify-out" / "graph.json"
    original = gpath.read_bytes()
    _run(app)
    first = gpath.read_bytes()
    _run(app)
    assert gpath.read_bytes() == first
    assert main(["strip", "--root", str(app)]) == 0
    assert gpath.read_bytes() == original


def test_missing_graph_and_non_symfony(tmp_path, capsys):
    assert _run(tmp_path) == 0
    assert _run(tmp_path, "--strict") == 2
    (tmp_path / "graphify-out").mkdir()
    (tmp_path / "graphify-out" / "graph.json").write_text('{"nodes":[],"links":[]}')
    assert _run(tmp_path) == 0
    assert "not a Symfony project" in capsys.readouterr().out


def test_unrelated_graph_nodes_untouched(app):
    before = {n["id"]: n for n in _load(app)["nodes"]}
    _run(app)
    after = {n["id"]: n for n in _load(app)["nodes"] if n.get("_origin") != ORIGIN}
    assert before == after
