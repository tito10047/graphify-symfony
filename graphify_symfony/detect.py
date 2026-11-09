"""Find Symfony application roots (single app or monorepo) and read the bits of
their configuration the extractors need. Never executes project code."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SKIP_DIRS = {
    "vendor", "node_modules", "var", ".git", "graphify-out", "public", "assets",
    "tests", "translations", "migrations", ".idea", ".vscode", "cache", "build",
    "dist", "docker", "docs", "_docs",
}
SYMFONY_MARKERS = ("symfony/framework-bundle", "symfony/http-kernel")


class _Loader(yaml.SafeLoader):
    """SafeLoader that tolerates Symfony's custom tags (!php/const, !tagged_iterator, ...)."""


def _unknown_tag(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


_Loader.add_multi_constructor("!", _unknown_tag)


def load_yaml(path: Path):
    try:
        with open(path, encoding="utf-8") as fh:
            return yaml.load(fh, Loader=_Loader)  # noqa: S506 - SafeLoader subclass
    except Exception:
        return None


def load_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def rel(path: Path, root: Path) -> str:
    try:
        r = path.relative_to(root).as_posix()
    except ValueError:
        r = os.path.relpath(path, root).replace(os.sep, "/")
    return "" if r == "." else r


def join(*parts: str) -> str:
    return "/".join(p.strip("/") for p in parts if p and p.strip("/"))


@dataclass
class App:
    root: Path            # absolute
    rel: str              # repo-relative ("" for the repo root)
    composer: dict
    name: str = ""
    # (twig namespace or None for main, repo-relative dir)
    template_dirs: list[tuple[str | None, str]] = field(default_factory=list)
    form_themes: list[str] = field(default_factory=list)
    anonymous_dir: str = "components/"
    # namespace prefix (with trailing \) -> {"template_directory", "name_prefix"}
    component_defaults: dict[str, dict] = field(default_factory=dict)
    sdc_dir: str | None = None          # repo-relative
    sdc_namespace: str | None = None    # e.g. App\Component
    psr4: dict[str, list[str]] = field(default_factory=dict)  # ns -> repo-rel dirs

    @property
    def label(self) -> str:
        return self.rel or "."

    def path(self, sub: str) -> str:
        return join(self.rel, sub)

    def to_json(self) -> dict:
        return {
            "root": self.label,
            "name": self.name,
            "template_dirs": [{"namespace": ns, "dir": d} for ns, d in self.template_dirs],
            "anonymous_component_dir": self.anonymous_dir,
            "component_defaults": self.component_defaults,
            "sdc": {"dir": self.sdc_dir, "namespace": self.sdc_namespace} if self.sdc_dir else None,
            "container_dump": [str(p) for p in container_dumps(self)],
        }


@dataclass
class Project:
    root: Path
    apps: list[App]
    packages: dict[str, str]  # composer package name -> repo-relative dir

    @property
    def is_symfony(self) -> bool:
        return bool(self.apps)

    def app_for(self, rel_path: str) -> App | None:
        best = None
        for app in self.apps:
            if not app.rel or rel_path == app.rel or rel_path.startswith(app.rel + "/"):
                if best is None or len(app.rel) > len(best.rel):
                    best = app
        return best

    def to_json(self) -> dict:
        return {
            "root": str(self.root),
            "symfony": self.is_symfony,
            "apps": [a.to_json() for a in self.apps],
        }


def _requires_symfony(composer: dict) -> bool:
    req = {**(composer.get("require") or {}), **(composer.get("require-dev") or {})}
    return any(m in req for m in SYMFONY_MARKERS)


def _is_app_dir(d: Path, composer: dict) -> bool:
    if not _requires_symfony(composer):
        return False
    if composer.get("type") in ("symfony-bundle", "library"):
        return False
    return (d / "bin" / "console").is_file() or (d / "config" / "bundles.php").is_file() \
        or (d / "src" / "Kernel.php").is_file()


def _walk_composer(root: Path, max_depth: int = 3):
    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        cj = d / "composer.json"
        if cj.is_file():
            yield d, load_json(cj)
        if depth >= max_depth:
            continue
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name, reverse=True)
        except OSError:
            continue
        for e in entries:
            if e.is_dir(follow_symlinks=False) and not e.name.startswith(".") and e.name not in SKIP_DIRS:
                stack.append((Path(e.path), depth + 1))


def detect_project(root: Path) -> Project:
    root = root.resolve()
    apps: list[App] = []
    packages: dict[str, str] = {}
    for d, composer in _walk_composer(root):
        name = composer.get("name")
        if isinstance(name, str):
            packages.setdefault(name, rel(d, root))
        if _is_app_dir(d, composer):
            apps.append(App(root=d, rel=rel(d, root), composer=composer, name=name or rel(d, root) or root.name))
    project = Project(root=root, apps=sorted(apps, key=lambda a: a.rel), packages=packages)
    for app in project.apps:
        _configure_app(project, app)
    return project


# ---------------------------------------------------------------- config
def resolve_config_path(project: Project, app: App, raw: str) -> str | None:
    """Repo-relative dir for a path from Symfony config, or None if outside."""
    if not isinstance(raw, str) or not raw:
        return None
    p = raw.replace("%kernel.project_dir%", str(app.root)).rstrip("/")
    if "%" in p:
        return None
    path = Path(p)
    if not path.is_absolute():
        path = app.root / path
    path = Path(os.path.normpath(path))
    r = rel(path, project.root)
    parts = r.split("/")
    if "vendor" in parts:
        i = parts.index("vendor")
        # composer path repository / monorepo package checked out in the repo
        if path.exists():
            real = path.resolve()
            rr = rel(real, project.root)
            if not rr.startswith("..") and "vendor" not in rr.split("/"):
                return rr
        if len(parts) >= i + 3:
            pkg = parts[i + 1] + "/" + parts[i + 2]
            target = project.packages.get(pkg)
            if target is not None:
                return join(target, *parts[i + 3:])
        return None
    if r.startswith(".."):
        return None
    return r


def _yaml_sections(data) -> list[dict]:
    """Top-level config plus when@dev, the env we statically model."""
    out = []
    if isinstance(data, dict):
        out.append(data)
        dev = data.get("when@dev")
        if isinstance(dev, dict):
            out.append(dev)
    return out


def _configure_app(project: Project, app: App) -> None:
    pk = app.root / "config" / "packages"
    # PSR-4 (only used to sanity-check component namespaces)
    for ns, dirs in ((app.composer.get("autoload") or {}).get("psr-4") or {}).items():
        dirs = dirs if isinstance(dirs, list) else [dirs]
        app.psr4[ns] = [join(app.rel, d) for d in dirs if isinstance(d, str)]

    main_dirs: list[str] = []
    named: list[tuple[str | None, str]] = []
    default_path = app.path("templates")
    for sec in _yaml_sections(load_yaml(pk / "twig.yaml")):
        twig = sec.get("twig")
        if not isinstance(twig, dict):
            continue
        if isinstance(twig.get("default_path"), str):
            dp = resolve_config_path(project, app, twig["default_path"])
            if dp is not None:
                default_path = dp
        paths = twig.get("paths")
        items: list[tuple[str, str | None]] = []
        if isinstance(paths, dict):
            items = [(k, v) for k, v in paths.items()]
        elif isinstance(paths, list):
            for p in paths:
                if isinstance(p, str):
                    items.append((p, None))
                elif isinstance(p, dict):
                    items.append((p.get("value") or p.get("path"), p.get("namespace")))
        for raw, ns in items:
            d = resolve_config_path(project, app, raw)
            if d is None:
                continue
            ns = (ns or "").lstrip("@") or None
            if ns is None:
                main_dirs.append(d)
            else:
                named.append((ns, d))
        ft = twig.get("form_themes")
        if isinstance(ft, list):
            app.form_themes.extend(x for x in ft if isinstance(x, str))
    for sec in _yaml_sections(load_yaml(pk / "twig_component.yaml")):
        tc = sec.get("twig_component")
        if not isinstance(tc, dict):
            continue
        if isinstance(tc.get("anonymous_template_directory"), str):
            app.anonymous_dir = tc["anonymous_template_directory"]
        defaults = tc.get("defaults")
        if isinstance(defaults, dict):
            for ns, val in defaults.items():
                if isinstance(val, str):
                    val = {"template_directory": val}
                if isinstance(val, dict):
                    app.component_defaults[ns.rstrip("\\") + "\\"] = {
                        "template_directory": val.get("template_directory") or "components/",
                        "name_prefix": val.get("name_prefix") or "",
                    }
    for sec in _yaml_sections(load_yaml(pk / "ux_sdc.yaml")):
        sdc = sec.get("ux_sdc")
        if not isinstance(sdc, dict):
            continue
        d = resolve_config_path(project, app, sdc.get("ux_components_dir") or "%kernel.project_dir%/src_component")
        if d is not None:
            app.sdc_dir = d
        if isinstance(sdc.get("component_namespace"), str):
            app.sdc_namespace = sdc["component_namespace"].strip("\\")
    if app.sdc_dir is None and (pk / "ux_sdc.yaml").is_file():
        app.sdc_dir = app.path("src_component")

    # main namespace: default path first, like Twig's FilesystemLoader
    dirs: list[tuple[str | None, str]] = [(None, default_path)]
    dirs += [(None, d) for d in main_dirs]
    if app.sdc_dir:
        dirs.append((None, app.sdc_dir))
    dirs += named
    seen = set()
    for item in dirs:
        if item not in seen and (project.root / item[1]).is_dir():
            seen.add(item)
            app.template_dirs.append(item)


def container_dumps(app: App) -> list[Path]:
    cache = app.root / "var" / "cache" / "dev"
    if not cache.is_dir():
        return []
    return sorted(cache.glob("*DebugContainer.xml"))
