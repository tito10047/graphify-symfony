# graphify-symfony

A Symfony post-pass for [Graphify](https://pypi.org/project/graphifyy/) (`graphify` CLI).

Graphify builds a code knowledge graph (`graphify-out/graph.json`) from tree-sitter
ASTs. It knows PHP classes and methods, but it does not parse Twig at all and knows
nothing about how Symfony wires an application together. `graphify-symfony` runs
after `graphify update .` and adds that missing layer to the same `graph.json`:
templates and their inheritance, which controller renders which template, Twig and
Live components, Stimulus controllers, routes, the DI container, Doctrine relations,
Messenger handlers, event listeners and console commands.

Afterwards the usual Graphify commands simply see more of the application:

```console
$ graphify explain "product/show.html.twig"
  --> base.html.twig [extends] [EXTRACTED] templates/product/show.html.twig:L1
  <-- .show() [uses] [EXTRACTED] src/Controller/ProductController.php:L21
  --> Alert [uses] [EXTRACTED] templates/product/show.html.twig:L9
$ graphify affected "PriceCalculator"      # now includes the services it is injected into
$ graphify path "ProductController" "base.html.twig"
```

Nothing is executed by default: PHP, Twig and YAML are read as text, and the DI
container is taken from the XML dump Symfony already writes to `var/cache/dev/`.

## Requirements

* Graphify (`graphifyy` on PyPI) installed with `uv tool install` (tested with 0.9.104).
* Python 3.10+ (the one in Graphify's tool environment).
* A Symfony application: a `composer.json` requiring `symfony/framework-bundle`, plus
  `bin/console`, `config/bundles.php` or `src/Kernel.php`. Monorepos with several
  applications (e.g. `apps/*/`, each with its own `composer.json`) are supported; every
  app gets its own Twig paths, component config and container dump.

## Installation

graphify-symfony must live in the **same environment as Graphify**, because it uses
Graphify's own node-id function and rebuild lock. Install it into the Graphify tool
environment and expose its executable:

```sh
uv tool install --force \
  --with git+https://github.com/tito10047/graphify-symfony.git \
  --with-executables-from graphify-symfony \
  graphifyy
```

From a local checkout:

```sh
uv tool install --force \
  --with /path/to/graphify-symfony \
  --with-executables-from graphify-symfony \
  graphifyy
```

`--with` alone installs the package but does not put `graphify-symfony` on `PATH`;
`--with-executables-from graphify-symfony` does. Add any extras you normally use
(`"graphifyy[mcp]"`, …). `uv tool install --force` replaces the whole Graphify tool
environment: if the build fails, re-run it (or a plain `uv tool install --force
graphifyy`) to get `graphify` back. `--with <path>` installs a copy, so re-run the
command after changing a local checkout.

Check with `graphify-symfony --version`.

## Usage

```sh
graphify update .                           # or: graphify extract . --code-only
graphify-symfony run --root .               # add the Symfony layer (idempotent)
graphify-symfony run --root . -v            # with per-kind counts and timings
graphify-symfony run --root . --json        # machine-readable stats + unresolved samples
graphify-symfony detect --root . [--json]   # Symfony apps, Twig paths, container dumps
graphify-symfony strip --root .             # remove everything graphify-symfony added
```

`run` reads `graphify-out/graph.json` (respects `GRAPHIFY_OUT`), removes the nodes and
edges it added previously (`_origin: "graphify-symfony"`), extracts again and writes
the file back atomically under Graphify's own `.rebuild.lock`. Running it twice gives
a byte-identical file, and `strip` restores the original byte for byte.

Exit codes: `0` on success, also when the directory is not a Symfony project (a note is
printed) or when `graph.json` does not exist yet (a hint is printed); with `--strict` a
missing `graph.json` exits with `2`.

Set `GRAPHIFY_BACKEND=none` if you want to be sure Graphify never calls an LLM; the
post-pass itself never does and makes no network requests.

### Git hooks

```sh
graphify-symfony hook install --root .     # also runs `graphify hook install`
graphify-symfony hook status --root .      # exit 1 if something is missing
graphify-symfony hook uninstall --root .   # removes only our blocks
```

Graphify's hooks (`post-commit`, `post-checkout`, `post-merge`, `post-rewrite`) start a
detached background rebuild. graphify-symfony adds its own marked block
(`# graphify-symfony-hook-start` … `# graphify-symfony-hook-end`) right after
Graphify's block in each hook. That block launches, also detached,
`graphify-symfony run --after-graphify`, which waits until Graphify's rebuild has
taken and released `.rebuild.lock` and then applies the post-pass. Commits are not
slowed down.

* Re-running `graphify hook install` rewrites only the text between Graphify's markers,
  so our block survives; any other content of the hook files is left alone.
* The block honours `GRAPHIFY_SKIP_HOOK=1` and `GRAPHIFY_SYMFONY_SKIP_HOOK=1`, skips
  linked worktrees and in-progress rebases/merges, and does nothing until
  `graphify-out/graph.json` exists.
* Output goes to `~/.cache/graphify-symfony.log` (override with `GRAPHIFY_SYMFONY_LOG`);
  Graphify's own rebuild logs to `~/.cache/graphify-rebuild.log`.

### Do the nodes survive Graphify rebuilds?

Yes. Graphify's incremental and full code rebuilds (`graphify update`, its git hooks,
`graphify watch`) keep every node and edge whose `_origin` is not `"ast"`, as long as
both endpoints still exist. Template nodes are owned by their `.twig` file, so deleting
the file makes Graphify evict them. One caveat: the rebuild stores edges in a simple
graph, so when two edges connect the same pair of nodes (for example a Doctrine
`ManyToOne` and its inverse `OneToMany`), one of them can be collapsed. The hook
re-applies the post-pass after every rebuild, so the final state is complete; after a
manual `graphify update`, run `graphify-symfony run` again.

## What is extracted

| Area | Source | Nodes | Edges (`sf_kind`) |
| --- | --- | --- | --- |
| Twig templates | `*.twig` under each app's Twig paths: `templates/`, `twig.paths`, `templates/bundles/` overrides, `templates/` or `Resources/views` of bundles whose code is in the repo, the ux-sdc component dir | template file, `{% block %}` | `extends`, `include` (tag and function), `embed`, `import` (`import`/`from`), `use`, `form_theme` (tag and `twig.form_themes`), `source`, `block_ref`, `defines_block`, `overrides_block` (child block → nearest ancestor block) |
| PHP → Twig | `render()`, `renderView()`, `renderBlock()`, `stream()`, `htmlTemplate()`, `textTemplate()`, `#[Template]`, `@Template`; any other `'….twig'` literal as INFERRED | — | `render`, `render_block`, `template_ref`, from the enclosing method node (fallback: class, file) |
| Twig components | `#[AsTwigComponent]`, `#[AsLiveComponent]`, `#[AsSdcComponent]` (ux-sdc), `twig_component.defaults`, `anonymous_template_directory` | — | `component_template` (class → template); `component` (template → class or anonymous template) for `<twig:Name>`, `{{ component('Name') }}`, `{% component Name %}` |
| Stimulus | `data-controller`, `stimulus_controller/action/target()`, `{{ controller }}` in ux-sdc components, controllers shipped by in-repo packages (`package.json` → `symfony.controllers`) | — | `stimulus` (template → `*_controller.js` file node), `sdc_controller` |
| Routes | `#[Route]` (class prefix + method, invokable controllers), `config/routes*.yaml`, `var/cache/dev/url_generating_routes.php` (full paths incl. locale prefixes, loader routes) | route (`route <name> <METHODS> <path>`) | `route` (route → controller method) |
| DI container | `var/cache/dev/*DebugContainer.xml` | named app services (`service <id>`) | `injects` (constructor/calls), `injects_iterator`, `di_alias` (interface → implementation), `decorates`, `service_class` |
| Events | `#[AsEventListener]`, `getSubscribedEvents()`, `kernel.event_listener` tags, Doctrine listeners | event (`event kernel.request`) or the event class node | `listens_to`, `subscribes_to`, `doctrine_listener`, `entity_listener` |
| Messenger | `#[AsMessageHandler]` (class or method), `MessageHandlerInterface`, `messenger.message_handler` tags | — | `handles_message` (handler method → message class) |
| Console | `#[AsCommand]`, `console.command` tags | command (`console app:import`) | `console_command` |
| Doctrine | `#[ORM\Entity(repositoryClass)]`, `ManyToOne`/`OneToMany`/`ManyToMany`/`OneToOne`/`Embedded` attributes and annotations | — | `doctrine_repository`, `doctrine_manytoone`, … |

Only application code is modelled: services, routes and templates from `vendor/` are
not added as nodes. An edge to a vendor class is kept only when Graphify already has a
node for it (the stubs it mints for `use` imports), so for example
`ProductController --injects--> Doctrine\ORM\EntityManagerInterface` appears when the
controller imports that interface.

### Graph schema

Nodes and edges follow Graphify's schema (`id`, `label`, `source_file`,
`source_location`, `file_type`, `community`, `relation`, `confidence`, …) and carry
`_origin: "graphify-symfony"`. Node ids are made with Graphify's own
`graphify.ids.make_id`, so edges attach to Graphify's PHP class, method and file nodes;
a template's id is the id Graphify would give that file. Extra fields: `sf_type` on
nodes (`template`, `twig_block`, `route`, `service`, `event`, `console_command`) and
`sf_kind` on edges.

Edge `relation`s are taken from Graphify's default `affected` relation set (`extends`,
`embeds`, `imports`, `mixes_in`, `uses`, `references`, `defines`), so `graphify
affected` and `graphify path` traverse them without `--relation`; `sf_kind` holds the
precise meaning. Confidence is `EXTRACTED` for literal references and container data
and `INFERRED` for conventions (default component templates, Doctrine targets taken
from property types, templates found by path suffix for custom Twig loaders, ambiguous
`extends a ? b : c`).

## The DI container dump

The container part needs `var/cache/dev/<Kernel>DevDebugContainer.xml`, which Symfony
writes whenever the dev cache is built (any `bin/console` call or web request in the
dev environment). Without it the container part is skipped with a note; if the dump is
older than source or config files, a staleness note is printed.

`--boot-container` runs `php bin/console cache:warmup --env=dev` in each app first.
**This executes project code** (bundles, compiler passes, possibly env/DB access), so it
is opt-in, limited to 60 s per app and fails soft.

## Tips

* Large translation catalogues or generated API clients tend to dominate Graphify's
  "god nodes". Consider a `.graphifyignore` listing `translations/` and generated client
  directories. graphify-symfony honours `.graphifyignore` for templates too.
* `graphify-symfony run --json` lists samples of unresolved template, component and
  Stimulus names (usually vendor ones), which helps when something looks missing.

## Limitations

* Regex-based Twig and PHP scanning. Dynamic template names (`'x/' ~ name`, variables)
  are skipped and counted in `twig_dynamic_refs_skipped`; template strings inside
  commented-out PHP code may still produce INFERRED `template_ref` edges.
* Only the `dev` environment is modelled (top level + `when@dev`); PHP-format config
  files (`config/packages/*.php`) are not read.
* Templates of vendor bundles are not in the graph, so references to them are dropped.
* `getSubscribedEvents()` is read statically; when the handler method is not obvious the
  edge starts at the subscriber class.
* `GRAPH_REPORT.md` and `graph.html` are not regenerated by the post-pass.

## Development

```sh
uv venv && uv pip install -e '.[test]' graphifyy
.venv/bin/pytest -q
```

The test suite uses a small synthetic Symfony app (`tests/conftest.py`); the
integration test runs the real Graphify when it is installed.

## License

MIT, see [LICENSE](LICENSE).
