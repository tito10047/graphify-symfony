from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

from graphify_symfony import hooks
from graphify_symfony.detect import detect_project
from graphify_symfony.php import full_scan
from graphify_symfony.twig import TemplateResolver, parse_twig


def test_parse_twig_tags_functions_and_html():
    tf = parse_twig(textwrap.dedent("""\
        {% extends cond ? 'a.html.twig' : 'b.html.twig' %}
        {%- include ['x.html.twig', 'y.html.twig'] ignore missing -%}
        {% embed 'card.html.twig' with {title: 'z.html.twig'} %}{% block inner %}{% endblock %}{% endembed %}
        {% from 'macros.html.twig' import input %}
        {% form_theme form 'theme_a.html.twig' 'theme_b.html.twig' %}
        {% import _self as me %}
        {# {% include 'hidden.html.twig' %} #}
        {% verbatim %}{% include 'raw.html.twig' %}{% endverbatim %}
        {{ source('@icons/logo.svg.twig') }}
        {{ block('title', 'layout.html.twig') }}
        {% component 'Card' with {a: 1} %}{% endcomponent %}
        {% component Table %}{% endcomponent %}
        <twig:Shop:ProductTile :product="p"/>
        <twig:block name="footer">x</twig:block>
        <div data-controller="search  modal--dialog {{ extra }}" {{ stimulus_controller(controller) }}></div>
        {{ stimulus_action('cart', 'add') }}
        """))
    kinds = {(r.kind, tuple(r.names), r.dynamic) for r in tf.refs}
    assert ("extends", ("a.html.twig", "b.html.twig"), True) in kinds
    assert ("include", ("x.html.twig", "y.html.twig"), False) in kinds
    assert ("embed", ("card.html.twig",), False) in kinds
    assert ("import", ("macros.html.twig",), False) in kinds
    assert ("form_theme", ("theme_a.html.twig", "theme_b.html.twig"), False) in kinds
    assert ("source", ("@icons/logo.svg.twig",), False) in kinds
    assert any(r.kind == "block_ref" and r.block == "title" for r in tf.refs)
    flat = json.dumps([r.names for r in tf.refs])
    assert "hidden" not in flat and "raw.html" not in flat and "z.html" not in flat
    assert [c[0] for c in tf.components] == ["Card", "Table", "Shop:ProductTile"]
    assert {s[0] for s in tf.stimulus} == {"search", "modal--dialog", "{{controller}}", "cart"}
    assert ("inner", 3) in tf.blocks


def test_php_scan_attributes_and_refs():
    pf = full_scan("src/X.php", textwrap.dedent("""\
        <?php
        namespace App\\Web;

        use Symfony\\Component\\Routing\\Attribute\\Route;
        use App\\Domain\\{Order, Customer as Client};

        // $this->render('commented.html.twig') is still a string literal; kept as template_ref
        #[Route(path: ['en' => '/orders', 'sk' => '/objednavky'], name: 'orders')]
        class OrderController
        {
            #[Route('/{id}', name: '_show', methods: ['GET'])]
            public function show(
                #[MapEntity] Order $order,
            ) {
                return $this->render("order/show.html.twig");
            }
        }
        """))
    assert pf.uses["Client"] == "App\\Domain\\Customer"
    assert pf.uses["Order"] == "App\\Domain\\Order"
    c = pf.classes[0]
    assert c.fqcn == "App\\Web\\OrderController"
    assert [a.short for a in c.attrs] == ["Route"]
    assert c.attrs[0].kwargs["path"] == {"en": "/orders", "sk": "/objednavky"}
    show = c.method("show")
    assert [a.short for a in show.attrs] == ["Route"]  # #[MapEntity] is a parameter attribute
    assert show.attrs[0].kwargs["methods"] == ["GET"]
    assert show.type == "Order"
    assert (15, "order/show.html.twig", "render", "render") in pf.twig_refs


def test_resolver(tmp_path):
    files = {"templates/a.html.twig", "templates/bundles/AcmeBundle/x.html.twig",
             "lib/acme/views/x.html.twig", "lib/acme/views/y.html.twig", "mail/m.html.twig"}
    r = TemplateResolver(tmp_path, [(None, "templates"), ("Acme", "lib/acme/views"), ("mail", "mail")],
                         lambda p: p in files)
    assert r.resolve("a.html.twig") == "templates/a.html.twig"
    assert r.resolve("/a.html.twig") == "templates/a.html.twig"
    assert r.resolve("@Acme/x.html.twig") == "templates/bundles/AcmeBundle/x.html.twig"  # override
    assert r.resolve("@!Acme/x.html.twig") == "lib/acme/views/x.html.twig"
    assert r.resolve("@Acme/y.html.twig") == "lib/acme/views/y.html.twig"
    assert r.resolve("@mail/m.html.twig") == "mail/m.html.twig"
    assert r.resolve("@Other/a.html.twig") is None
    assert r.logical_name("lib/acme/views/y.html.twig") == "@Acme/y.html.twig"


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def test_detect_monorepo(tmp_path):
    app = '{"require": {"symfony/framework-bundle": "^7"}}'
    _write(tmp_path, {
        "composer.json": '{"name": "acme/mono"}',
        "apps/shop/composer.json": app,
        "apps/shop/bin/console": "",
        "apps/shop/templates/x.html.twig": "",
        "apps/shop/config/packages/twig.yaml":
            "twig:\n  paths:\n    '%kernel.project_dir%/vendor/acme/ui-kit/templates': ui\n",
        "apps/admin/composer.json": app,
        "apps/admin/config/bundles.php": "",
        "libs/ui-kit/composer.json": '{"name": "acme/ui-kit", "type": "symfony-bundle", '
                                     '"require": {"symfony/framework-bundle": "^7"}}',
        "libs/ui-kit/templates/button.html.twig": "",
    })
    p = detect_project(tmp_path)
    assert [a.rel for a in p.apps] == ["apps/admin", "apps/shop"]
    shop = p.apps[1]
    assert ("ui", "libs/ui-kit/templates") in shop.template_dirs  # vendor path -> in-repo package
    assert p.app_for("apps/shop/src/X.php") is shop
    assert detect_project(tmp_path / "libs").apps == []


def _git(cwd: Path, *args) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


GRAPHIFY_LIKE = "#!/bin/sh\n# user line\n# graphify-hook-start\n( echo graphify )\n# graphify-hook-end\n"


def test_hook_block_chaining(tmp_path):
    _git(tmp_path, "init", "-q")
    hd = tmp_path / ".git" / "hooks"
    hook = hd / "post-commit"
    hook.write_text(GRAPHIFY_LIKE)
    assert hooks.install_block(hook, "post-commit", "/usr/bin/python3") == "installed"
    text = hook.read_text()
    assert text.index("# graphify-hook-end") < text.index(hooks.MARK)
    assert "# user line" in text
    # graphify rewrites only its own block on re-install
    text2 = text.replace("( echo graphify )", "( echo graphify v2 )")
    hook.write_text(text2 + "# user tail\n")
    assert hooks.install_block(hook, "post-commit", "/usr/bin/python3") == "updated"
    text3 = hook.read_text()
    assert text3.count(hooks.MARK) == 1 and "graphify v2" in text3 and "# user tail" in text3
    status, ok = hooks.status(tmp_path)
    assert "post-commit: graphify-symfony installed, after graphify" in status
    assert hooks.uninstall_block(hook) == "removed"
    assert hooks.MARK not in hook.read_text() and "graphify v2" in hook.read_text()
    # sh syntax check of the rendered blocks
    for h in hooks.HOOKS:
        sh = tmp_path / f"{h}.sh"
        sh.write_text("#!/bin/sh\n" + hooks.render_block(h, "/usr/bin/python3"))
        subprocess.run(["sh", "-n", str(sh)], check=True)
