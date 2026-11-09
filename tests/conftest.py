"""Synthetic Symfony app ("acme") + a graph.json shaped like graphify's output.

Everything here is made up for the tests; nothing is copied from a real project."""

from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path

import pytest

from graphify_symfony.ids import file_node_id, make_id

FILES = {
    "composer.json": json.dumps({
        "name": "acme/shop",
        "type": "project",
        "require": {"symfony/framework-bundle": "^7.0", "symfony/ux-twig-component": "^2.20"},
        "autoload": {"psr-4": {"App\\": "src/"}},
    }),
    "bin/console": "#!/usr/bin/env php\n<?php\n",
    "config/bundles.php": "<?php return [];\n",
    "config/packages/twig.yaml": """\
        twig:
            default_path: '%kernel.project_dir%/templates'
            paths:
                '%kernel.project_dir%/templates/mail': mail
            form_themes:
                - 'form/theme.html.twig'
        """,
    "config/packages/twig_component.yaml": """\
        twig_component:
            anonymous_template_directory: 'components/'
            defaults:
                App\\Twig\\Components\\: 'components/'
        """,
    "config/routes.yaml": """\
        legacy_home:
            path: /legacy
            controller: App\\Controller\\ProductController::legacy
        controllers:
            resource: routing.controllers
        """,
    "config/services.yaml": "services:\n  _defaults:\n    autowire: true\n",
    "src/Controller/ProductController.php": """\
        <?php
        namespace App\\Controller;

        use App\\Entity\\Product;
        use App\\Service\\PriceCalculator;
        use Symfony\\Bundle\\FrameworkBundle\\Controller\\AbstractController;
        use Symfony\\Component\\Routing\\Attribute\\Route;

        /**
         * Docblock mentioning #[Route('/not-a-route')] must be ignored.
         */
        #[Route('/products', name: 'product_')]
        final class ProductController extends AbstractController
        {
            public function __construct(private PriceCalculator $calc) {}

            #[Route('/{id}', name: 'show', methods: ['GET'])]
            public function show(Product $product): Response
            {
                return $this->render('product/show.html.twig', ['product' => $product]);
            }

            #[Route('', name: 'list', methods: 'GET|HEAD')]
            public function list(): Response
            {
                $html = $this->renderView('product/list.html.twig');
                return $this->renderBlock('product/show.html.twig', 'price', []);
            }

            public function legacy(): Response
            {
                return $this->render('@mail/welcome.html.twig');
            }
        }
        """,
    "src/Entity/Product.php": """\
        <?php
        namespace App\\Entity;

        use App\\Repository\\ProductRepository;
        use Doctrine\\ORM\\Mapping as ORM;

        #[ORM\\Entity(repositoryClass: ProductRepository::class)]
        class Product
        {
            #[ORM\\Id, ORM\\Column]
            private ?int $id = null;

            #[ORM\\ManyToOne(targetEntity: Category::class, inversedBy: 'products')]
            private ?Category $category = null;

            #[ORM\\ManyToOne]
            private ?Brand $brand = null;
        }
        """,
    "src/Entity/Category.php": """\
        <?php
        namespace App\\Entity;

        use Doctrine\\ORM\\Mapping as ORM;

        /**
         * @ORM\\Entity(repositoryClass="App\\Repository\\CategoryRepository")
         */
        class Category
        {
            /** @ORM\\OneToMany(targetEntity="Product", mappedBy="category") */
            private $products;
        }
        """,
    "src/Entity/Brand.php": "<?php\nnamespace App\\Entity;\n\nclass Brand\n{\n}\n",
    "src/Repository/ProductRepository.php": "<?php\nnamespace App\\Repository;\n\nclass ProductRepository\n{\n}\n",
    "src/Repository/CategoryRepository.php": "<?php\nnamespace App\\Repository;\n\nclass CategoryRepository\n{\n}\n",
    "src/Service/PriceCalculator.php": """\
        <?php
        namespace App\\Service;

        class PriceCalculator implements PriceCalculatorInterface
        {
            public function __construct(private TaxTable $taxes) {}
        }
        """,
    "src/Service/PriceCalculatorInterface.php": "<?php\nnamespace App\\Service;\n\ninterface PriceCalculatorInterface\n{\n}\n",
    "src/Service/TaxTable.php": "<?php\nnamespace App\\Service;\n\nclass TaxTable\n{\n}\n",
    "src/Twig/Components/Alert.php": """\
        <?php
        namespace App\\Twig\\Components;

        use Symfony\\UX\\TwigComponent\\Attribute\\AsTwigComponent;

        #[AsTwigComponent]
        final class Alert
        {
            public string $type = 'info';
        }
        """,
    "src/Twig/Components/Cart/Summary.php": """\
        <?php
        namespace App\\Twig\\Components\\Cart;

        use Symfony\\UX\\LiveComponent\\Attribute\\AsLiveComponent;

        #[AsLiveComponent('cart_summary', template: 'cart/summary.html.twig')]
        final class Summary
        {
        }
        """,
    "src/Message/ReindexProduct.php": "<?php\nnamespace App\\Message;\n\nfinal class ReindexProduct\n{\n}\n",
    "src/MessageHandler/ReindexProductHandler.php": """\
        <?php
        namespace App\\MessageHandler;

        use App\\Message\\ReindexProduct;
        use Symfony\\Component\\Messenger\\Attribute\\AsMessageHandler;

        #[AsMessageHandler]
        final class ReindexProductHandler
        {
            public function __invoke(ReindexProduct $message): void
            {
            }
        }
        """,
    "src/EventSubscriber/LocaleSubscriber.php": """\
        <?php
        namespace App\\EventSubscriber;

        use Symfony\\Component\\EventDispatcher\\EventSubscriberInterface;
        use Symfony\\Component\\HttpKernel\\KernelEvents;

        final class LocaleSubscriber implements EventSubscriberInterface
        {
            public static function getSubscribedEvents(): array
            {
                return [KernelEvents::REQUEST => 'onRequest', 'acme.custom' => 'onCustom'];
            }

            public function onRequest(): void {}
        }
        """,
    "src/Command/ImportCommand.php": """\
        <?php
        namespace App\\Command;

        use Symfony\\Component\\Console\\Attribute\\AsCommand;

        #[AsCommand(name: 'acme:import', description: 'Import')]
        final class ImportCommand
        {
        }
        """,
    "templates/base.html.twig": """\
        <html><body data-controller="hello">
        {% block body %}{% endblock %}
        {% block footer %}(c){% endblock %}
        </body></html>
        """,
    "templates/product/show.html.twig": """\
        {% extends 'base.html.twig' %}
        {# {% include 'product/commented_out.html.twig' %} #}
        {% import 'macros/forms.html.twig' as forms %}
        {% block body %}
          {% include 'product/_card.html.twig' with {title: 'not/a/template.html.twig'} %}
          {{ include('product/_card.html.twig') }}
          {% include some_variable %}
          <twig:Alert type="ok" />
          <twig:Button>Buy</twig:Button>
          {{ component('cart_summary') }}
          <twig:ux:icon name="x" />
          <div {{ stimulus_controller('cart/drawer') }}></div>
        {% endblock %}
        {% block price %}9.99{% endblock %}
        """,
    "templates/product/list.html.twig": "{% extends 'base.html.twig' %}\n{% use 'product/blocks.html.twig' %}\n",
    "templates/product/_card.html.twig": "<div>{{ title }}</div>\n",
    "templates/product/blocks.html.twig": "{% block extra %}{% endblock %}\n",
    "templates/macros/forms.html.twig": "{% macro input(n) %}<input name=\"{{ n }}\">{% endmacro %}\n",
    "templates/form/theme.html.twig": "{% block form_row %}{% endblock %}\n",
    "templates/components/Alert.html.twig": "<div class=\"alert\">{% block content %}{% endblock %}</div>\n",
    "templates/components/Button.html.twig": "<button>{% block content %}{% endblock %}</button>\n",
    "templates/cart/summary.html.twig": "<aside>{{ include('product/_card.html.twig') }}</aside>\n",
    "templates/mail/welcome.html.twig": "Hello\n",
    "assets/controllers/hello_controller.js": "export default class {}\n",
    "assets/controllers/cart/drawer_controller.js": "export default class {}\n",
    "var/cache/dev/App_KernelDevDebugContainer.xml": """\
        <?xml version="1.0" encoding="utf-8"?>
        <container xmlns="http://symfony.com/schema/dic/services">
          <services>
            <service id="App\\Controller\\ProductController" class="App\\Controller\\ProductController" public="true">
              <tag name="controller.service_arguments"/>
              <argument type="service" id="App\\Service\\PriceCalculator"/>
              <call method="setContainer"><argument type="service" id=".service_locator.abc"/></call>
            </service>
            <service id="App\\Service\\PriceCalculator" class="App\\Service\\PriceCalculator">
              <argument type="service" id="App\\Service\\TaxTable"/>
              <argument type="service" id="logger"/>
            </service>
            <service id="App\\Service\\TaxTable" class="App\\Service\\TaxTable"/>
            <service id="acme.tax_table.cached" class="App\\Service\\TaxTable"/>
            <service id="App\\Service\\PriceCalculatorInterface" alias="App\\Service\\PriceCalculator"/>
            <service id="App\\EventSubscriber\\LocaleSubscriber" class="App\\EventSubscriber\\LocaleSubscriber">
              <tag name="kernel.event_subscriber"/>
            </service>
            <service id="logger" class="Psr\\Log\\NullLogger"/>
          </services>
        </container>
        """,
}

ROUTE_CACHE = """<?php

// This file has been auto-generated by the Symfony Routing Component.

return [
    'product_show' => [['id'], ['_controller' => 'App\\\\Controller\\\\ProductController::show'], [], [['variable', '/', '[^/]++', 'id', true], ['text', '/en/products']], [], [], []],
    'App\\\\Controller\\\\ProductController::show' => [['id'], ['_controller' => 'App\\\\Controller\\\\ProductController::show'], [], [['variable', '/', '[^/]++', 'id', true], ['text', '/en/products']], [], [], []],
];
"""

CLASS_RE = re.compile(r"^(?:final\s+|abstract\s+)*(class|interface)\s+(\w+)", re.M)
METHOD_RE = re.compile(r"function\s+(\w+)\s*\(")


def write_app(root: Path) -> Path:
    for rel, content in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(content), encoding="utf-8")
    (root / "var/cache/dev/url_generating_routes.php").write_text(ROUTE_CACHE, encoding="utf-8")
    return root


def fake_graph(root: Path) -> dict:
    """Minimal graph.json in graphify's schema for the PHP/JS files above."""
    nodes, links = [], []

    def node(nid, label, sf, line, **kw):
        nodes.append({"id": nid, "label": label, "_origin": "ast", "community": len(nodes) % 3,
                      "community_name": f"Community {len(nodes) % 3}", "file_type": "code",
                      "norm_label": label.lower(), "source_file": sf, "source_location": f"L{line}", **kw})

    for rel in sorted(FILES):
        if not rel.endswith((".php", ".js")) or rel.startswith(("config/", "bin/")):
            continue
        text = textwrap.dedent(FILES[rel])
        fid = file_node_id(rel)
        node(fid, rel.rsplit("/", 1)[-1], rel, 1)
        for cm in CLASS_RE.finditer(text):
            cid = make_id(fid, cm.group(2))
            line = text.count("\n", 0, cm.start()) + 1
            node(cid, cm.group(2), rel, line, _callable=True, _callable_class=True)
            links.append({"source": fid, "target": cid, "relation": "contains", "_origin": "ast",
                          "confidence": "EXTRACTED", "confidence_score": 1.0, "source_file": rel,
                          "source_location": f"L{line}", "weight": 1.0})
            for mm in METHOD_RE.finditer(text, cm.end()):
                mid = make_id(cid, mm.group(1))
                mline = text.count("\n", 0, mm.start()) + 1
                node(mid, f".{mm.group(1)}()", rel, mline, _callable=True)
                links.append({"source": cid, "target": mid, "relation": "method", "_origin": "ast",
                              "confidence": "EXTRACTED", "confidence_score": 1.0, "source_file": rel,
                              "source_location": f"L{mline}", "weight": 1.0})
    # an import stub graphify would mint for a vendor class
    nodes.append({"id": "psr_log_loggerinterface", "label": "Psr\\Log\\LoggerInterface", "community": 0,
                  "community_name": "Community 0", "external": True, "file_type": "concept",
                  "norm_label": "psr\\log\\loggerinterface", "source_file": "", "type": "external"})
    return {"directed": False, "multigraph": False, "graph": {"schema_version": 1},
            "nodes": nodes, "links": links, "hyperedges": []}


@pytest.fixture()
def app(tmp_path: Path) -> Path:
    root = write_app(tmp_path / "acme")
    out = root / "graphify-out"
    out.mkdir()
    (out / "graph.json").write_text(json.dumps(fake_graph(root), separators=(",", ":")), encoding="utf-8")
    return root
